[English](../02-architecture.md) | **한국어**

# 2. 아키텍처

## 2.1 프로세스 모델

```text
 human: approves goals, campaigns, and any default change
   │
 [planner]  campaign.json ── experiments, hypotheses, profile instructions, tuning ranges
   │                              ▲
   ▼                              │ metrics.json, anomalies.md, findings/
 shakedown orchestrator ──────────┤
   │  (tuning × seed × profile × map → N worker processes)
   ▼
 game process + adapter          ← no network code
   │   every step: observe → reflexes → executor → input events → simulation step
   │   decision point: pause clock, write "@@shakedown {request}" to stdout,
   │                   read one answer line from stdin
   ▼
 shakedown bridge ── decider: random | mock | tape | jev | (game rule, in-process)
   │
   ▼
 runs/<run>/ run.json, telemetry.jsonl, decision_tape.jsonl, inputs.json, stdout.log
```

게임 프로세스는 stdin과 stdout으로만 통신한다. API 키, 재시도, 예산, 캐싱은 모두 Python 브리지에 있다. 그래서 게임에는 HTTP 클라이언트도 새 의존성도 필요 없다.

브리지 프로세스 하나가 게임 프로세스 하나를 맡는다. 오케스트레이터는 N쌍을 병렬로 띄우고 실행(run)마다 별도의 작업 디렉터리를 준다. 프로필 파일이나 설정을 디스크에 저장하는 게임은 그렇게 하지 않으면 서로 경쟁 상태에 빠지기 때문이다(프로토타입은 공유된 `profile.json`에서 이 문제를 겪었다).

## 2.2 구성 요소

| 구성 요소 | 언어 | 책임 |
|---|---|---|
| 어댑터(adapter) | 게임의 언어 | 관측, 후보(candidate), 결정 시점(decision point), 실행기(executor), 반사(reflex), 오라클(oracle), 일시정지, 텔레메트리, 입력 기록(input recording) (3장) |
| 브리지 | Python | 요청을 읽고, 디사이더(decider)를 호출하고, 테이프(tape)를 쓰고, 예산을 지키고, 무효 실행을 표시한다 (4장) |
| 디사이더 | Python | `random`, `mock`, `tape`, `jev`. 나중에 로컬 오픈 모델 (4장) |
| 플래너(planner) | Python + LLM CLI | `campaign`, `revise`, `bug`, `report`, `targets` 단계. 출력은 JSON Schema로 검사한다 (5장) |
| 오케스트레이터 | Python | 배치(batch)를 실행들로 펼치고, 워커를 돌리고, 결과를 모은다 |
| 집계 | Python | 셀별 지표. 부트스트랩 95 % 구간과 같은 시드(seed) 쌍 비교를 쓴다 |
| 탐색기 | Python | 덜 방문한 상황 셀을 우선하는 QA 샘플링 (6장) |
| 이상 신호(anomaly) | Python | Wilson 하한이 임계값을 넘는 비율을 표시한다 (6장) |
| 분류(triage) | Python | 발견(finding)을 시그니처로 묶고, 테이프로 재현을 확인하고, 플래너에게 버그 초안을 요청한다 (6장) |
| 최적화기 | Python | 튜닝(tuning) 키 표 안에서 무작위 탐색과 격자 탐색 (7장) |
| 에이전트 스킬 | Markdown + 스크립트 | 코딩 에이전트가 위의 모든 것을 돌리고 어댑터를 작성하게 한다 (5장) |
| 예제 게임 | Python | 참조 어댑터와, 켜고 끌 수 있는 심어 둔 버그를 갖춘 작은 실시간 게임 (8장) |

Python 쪽은 표준 라이브러리(`json`, `subprocess`, `urllib`, `hashlib`, `concurrent.futures`, `random`, `statistics`)만 쓴다. 프로토타입이 이 규칙을 지켰고, 이 규칙 덕분에 설치는 `pip install` 한 번으로 끝난다. Optuna 같은 선택적 의존성은 extra 뒤에서, 별도의 결정을 거쳐서만 들어온다.

## 2.3 저장소 구조 (계획)

```text
shakedown/
  README.md, README.ko.md
  docs/                 English design docs
  docs/ko/              Korean translations, same file names
  src/shakedown/
    protocol.py         wire format, request hashing, version checks
    bridge.py           one game process ↔ one decider
    deciders/           random.py, mock.py, tape.py, jev.py
    planner/            claude_cli.py, codex_cli.py, mock.py, schemas/
    orchestrate.py      batches → runs → workers
    aggregate.py        metrics.json
    explore.py          QA sampling
    anomalies.py        anomalies.md
    triage.py           findings/
    optimize.py         tuning search
    campaign.py         the planner ↔ optimizer loop with approval gates
    conformance.py      adapter conformance checks (section 3.10)
    cli.py              `shakedown run | explore | triage | campaign | check-adapter`
  skill/shakedown/      SKILL.md and helper scripts for coding agents
  examples/tinyraid/    reference game and adapter (chapter 8)
  tests/
```

파일 이름 하나가 위 파이프라인의 단계 하나에 대응한다. 프로토타입 도구도 같은 방식으로 나뉘었고, 파일당 26~344줄을 유지했다.

## 2.4 설정

프로젝트는 `shakedown.toml`에 게임을 기술한다. 게임마다 플래그 이름이 다르므로 Shakedown은 플래그 이름을 하드코딩하지 않는다. 대신 명령 템플릿을 채운다.

```toml
[game]
command = ["{exe}", "--headless", "--seed", "{seed}", "--map", "{map}",
           "--duration", "{seconds}", "--telemetry", "{run_dir}/telemetry.jsonl",
           "--record-inputs", "{run_dir}/inputs.json", "--decider", "{decider_mode}",
           "--profile-name", "{profile}", "--tuning", "{tuning_file}"]
replay_command = ["{exe}", "--headless", "--seed", "{seed}", "--map", "{map}",
                  "--replay-inputs", "{inputs_file}", "--telemetry", "{run_dir}/telemetry.jsonl"]
exe = "build/mygame"
maps = ["yard", "warehouse"]
profiles = ["cautious", "aggressive", "looter"]

[decider]
backend = "random"        # random | mock | tape | jev
budget_usd = 0.0

[planner]
backend = "mock"          # mock | claude-cli | codex-cli
budget_usd = 0.0

[tuning]
keys = "tuning_keys.json" # chapter 7
```

`{decider_mode}`는 `bridge`, `rule`, `tape:<file>` 중 하나로 펼쳐진다(3.2절). 모든 실행은 완전히 펼친 명령을 `run.json`에 쓴다. 그래서 어떤 실행이든 손으로 다시 돌릴 수 있다.

## 2.5 실행 디렉터리

| 파일 | 작성 주체 | 내용 |
|---|---|---|
| `run.json` | 오케스트레이터 | 펼친 명령, 시드, 맵, 프로필(profile), 튜닝 해시, 디사이더, 플래너 지시문 해시, Shakedown 버전과 어댑터 버전 |
| `telemetry.jsonl` | 어댑터 | 게임 이벤트와, 결정론적 필드만 담은 `decision` 줄 |
| `decision_tape.jsonl` | 브리지 | 결정별 요청 해시, 후보, 순서 시드, 전체 답, 출처, 시도 횟수, 지연 시간, 모델 ID, 적용된 스텝, 폐기 사유 |
| `inputs.json` | 어댑터 | 실행기가 주입한 모든 입력 이벤트와 정확한 스텝 시각 |
| `bridge_summary.json` | 브리지 | 결정 횟수, 폴백 비율, 비용, 지연 시간 백분위수, `valid` 플래그 |
| `stdout.log` | 오케스트레이터 | 게임의 나머지 출력 |

어댑터가 써야 할 내용은 3.8절에서 정의한다. 실행을 무효로 표시하는 조건은 4.4절에서 정의한다.
