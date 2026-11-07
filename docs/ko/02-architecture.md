[English](../02-architecture.md) | **한국어**

# 2. 아키텍처

## 2.1 프로세스 모델

아래 그림은 전체 설계다. M0~M3는 게임·어댑터, 오프라인 디사이더, 브리지, 재생, 격리 배치, 집계, 적합성 검사, QA 경로를 제공한다. M4는 실행 사이의 플래너 초안과 승인·예산 기록, M5는 수치 탐색·보류 시드 선택 고정·제안 패치, M6는 에이전트 스킬·두 번째 얇은 실행기 예제, M7은 선택적 Jev HTTP 백엔드를 제공한다. 실제 공급자, 더 넓은 처음 보는 게임, 사람 플레이의 검증은 별도 과제다. `apply` 명령은 명시적 승인 아래 검토된 기본값을 쓰며 커밋은 운영자 단계로 남는다.

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
| 분류(triage) | Python | 발견을 묶고, 테이프·인자와 독립 입력 재생을 검증하고, 증거만 담은 초안을 쓴다 (6장) |
| 최적화기 | Python | 튜닝(tuning) 키 표 안에서 무작위 탐색과 격자 탐색 (7장) |
| 에이전트 스킬 | Markdown + 스크립트 | 코딩 에이전트가 위의 모든 것을 돌리고 어댑터를 작성하게 한다 (5장) |
| 예제 게임 | Python | 참조 어댑터와, 켜고 끌 수 있는 심어 둔 버그를 갖춘 작은 실시간 게임 (8장) |

Python 쪽은 표준 라이브러리(`json`, `subprocess`, `urllib`, `hashlib`, `concurrent.futures`, `random`, `statistics`)만 쓴다. 프로토타입이 이 규칙을 지켰고, 이 규칙 덕분에 설치는 `pip install` 한 번으로 끝난다. Optuna 같은 선택적 의존성은 extra 뒤에서, 별도의 결정을 거쳐서만 들어온다. 최소 버전은 Python 3.11이다. `shakedown.toml`을 읽는 `tomllib`이 3.11부터 들어 있기 때문이다. CI는 고정된 요청 해시 값을 포함한 테스트를 3.11, 3.12, 3.13에서 돌린다.

## 2.3 저장소 구조 (구현과 계획)

```text
shakedown/
  README.md, README.ko.md
  docs/                 English design docs
  docs/ko/              Korean translations, same file names
  src/shakedown/
    protocol.py         wire format, request hashing, version checks
    config.py           validated TOML and argument templates
    processes.py        bounded child processes and cancellation
    windows_job.py      Windows descendant containment
    bridge.py           one game process ↔ one decider
    deciders/           random.py, mock.py, tape.py, jev.py
    planner/            CLI backends, schemas, approvals, budgets, baseline campaigns
    orchestrate.py      batches → runs → workers
    aggregate.py        metrics.json
    explore.py                    QA sampling
    anomalies.py                anomalies.md
    triage.py                      findings/
    clips.py            verified replay frames and encoding
    bench.py            controlled tinyraid qualification
    optimize.py         bounded random/grid search and measured target distances
    balance_gate.py     holdout improvement, oracle, sample, and policy checks
    campaign.py         approved search/revision loop and frozen holdout evaluation
    proposals.py        reviewable tuning-default patches; never applies them
    conformance.py      adapter conformance checks (section 3.10)
    cli.py              `shakedown bridge | run | check-adapter | explore | anomalies | triage | bench | plan | approve | campaign`
  skill/shakedown/      agent skill, adapter guides, transport skeletons, wrapper
  examples/tinyraid/    reference game and adapter (chapter 8)
  examples/switchyard/  second game with a discrete-button thin executor
  tests/
```

파일 이름 하나가 위 파이프라인의 단계 하나에 대응한다. 프로토타입 도구도 같은 방식으로 나뉘었고, 파일당 26~344줄을 유지했다.

## 2.4 설정

프로젝트는 `shakedown.toml`에 게임을 기술한다. M2는 `tomllib`로 설정을 읽고 형식과 튜닝 범위를 검사한 뒤 셸 없이 인자 배열을 펼친다. 다음은 실행 가능한 `examples/tinyraid/shakedown.toml` 설정이다.

```toml
[game]
cwd = "../.."
command = ["{python}", "-m", "examples.tinyraid", "--headless", "--seed", "{seed}", "--map", "{map}", "--duration", "{seconds}", "--telemetry", "{run_dir}/telemetry.jsonl", "--record-inputs", "{run_dir}/inputs.json", "--decider", "{decider_mode}", "--profile-name", "{profile}", "--tuning", "{tuning_file}"]
replay_command = ["{python}", "-m", "examples.tinyraid", "--replay-inputs", "{inputs_file}", "--telemetry", "{run_dir}/telemetry.jsonl"]
maps = ["yard", "warehouse"]
profiles = ["cautious", "aggressive", "looter"]

[batch]
seeds = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
deciders = ["rule"]
seconds = 12
workers = 2
wall_timeout_s = 30
bootstrap_samples = 1000
bootstrap_seed = 0

[decider]
backend = "rule"
budget_usd = 0.0

[tuning]
keys = "tuning_keys.json"
candidates = [{}]

[checks]
oracle_args = ["--scenario", "stuck"]
oracle_seconds = 14
visibility_command = ["{python}", "-m", "examples.tinyraid.checks"]

[anomalies]
min_runs = 5

[clips]
replay_args = ["--frame-dir", "{frame_dir}", "--frame-every", "6"]
fps = 10
simulation_hz = 60
```

`{decider_mode}`는 `bridge`, `rule`, `tape:<file>` 중 하나로 펼쳐진다(3.2절). 모든 실행은 완전히 펼친 명령을 `run.json`에 쓴다. 그래서 어떤 실행이든 손으로 다시 돌릴 수 있다.

설정 경로는 TOML 파일 기준으로 해석한다. `game.cwd`는 프로젝트 루트를 고르며 자식 프로세스의 작업 디렉터리를 뜻하지 않는다. 자식은 각자의 `runs/<id>/work/`에서 실행하므로 프로젝트에 남겨 두는 에셋에는 `{project_dir}`를 쓴다. 선택적인 `game.fixtures`에는 프로젝트 루트 기준 파일 목록을 적는다. 실행마다 원본의 새 사본을 복사하며, 절대 경로나 루트 밖으로 나가는 경로는 거부한다.

`{python}`은 현재 인터프리터이고 `{exe}`는 선택적인 `game.exe`를 쓴다. 그 밖에 `{project_dir}`, `{seed}`, `{map}`, `{profile}`, `{seconds}`, `{run_dir}`, `{decider_mode}`, `{tuning_file}`, `{inputs_file}`을 지원한다. 속성 접근, 인덱싱, 형식 지정, 변환은 거부한다. 셸 문장 부호가 든 인자도 인자 하나로 전달한다. 지시문과 튜닝 키 파일은 설정 파일 기준으로 해석한다. 비어 있지 않은 튜닝 후보에는 키 표가 필요하며, 알 수 없는 키·잘못된 자료형·범위 밖 값은 거부한다.

M2 배치 백엔드는 `rule`, `mock`, `random`이다. `tape:<file>`은 배치 정책이 아닌 재생 모드다. 플래너 설정은 이후 작업을 기술할 수 있지만 M2에서 플래너를 실행하지는 않는다. `game.exit_codes`로 `ok`, `contract`, `oracle`, `divergence`를 서로 다른 음이 아닌 코드에 매핑할 수 있다.

## 2.5 실행 디렉터리

| 파일 | 작성 주체 | 내용 |
|---|---|---|
| `run.json` | 오케스트레이터 | 펼친 명령, 시드, 맵, 프로필(profile), 튜닝 해시, 디사이더, 플래너 지시문 해시, Shakedown 버전과 어댑터 버전 |
| `telemetry.jsonl` | 어댑터 | 게임 이벤트와 결정론적 `decision` 영수증: `seq`, 선택, 사유, 상태(`selected`, `applied`, `dropped`), 해당하는 경우 `applied_step` 또는 `drop_reason` |
| `decision_tape.jsonl` | 브리지 | 결정별 요청 해시, 후보, 순서 시드, 선택, 확률, 출처, 시도 횟수, 지연 시간, 모델 ID. 게임이 행동하기 전에 선택을 기록한다 |
| `inputs.json` | 어댑터 | 실행기가 주입한 모든 입력 이벤트와 정확한 스텝 시각 |
| `bridge_summary.json` | 브리지 | 결정 횟수, 폴백 비율, 비용, 지연 시간 백분위수, `valid` 플래그 |
| `stdout.log` | 오케스트레이터 | 게임의 나머지 출력 |

어댑터가 써야 할 내용은 3.8절에서 정의한다. 실행을 무효로 표시하는 조건은 4.4절에서 정의한다.

테이프 행과 어댑터의 적용·폐기 영수증은 `seq`로 연결한다. 브리지가 선택을 쓸 때는 지연 후의 적용 결과를 알 수 없다. 이 분리로 와이어 프로토콜 v1을 유지한다. 브리지 또는 직접 실행기가 `stdout.log`를 쓴다. M2는 실행 시작 실패까지 포함해 모든 시도의 `run.json`을 남긴다. 펼친 인자 배열, 개별 작업 디렉터리, 설정 경로·해시, 시드, 맵, 프로필, 튜닝 해시, 지시문 해시, 종료 코드 매핑, 버전을 기록한다. 알 수 없는 어댑터 버전은 추측하지 않고 null로 둔다.

## 2.6 배치 수명 주기

`run`은 시드 × 맵 × 프로필 × 오프라인 백엔드 × 튜닝 후보를 안정적인 실행 ID로 펼치고, 설정한 워커 수 이내에서 실행한다. 각 시도에는 유한한 실제 시간 제한이 있으며 자동으로 재시도하지 않는다. POSIX에서는 프로세스 그룹을 쓴다. Windows에서는 자식을 일시 정지 상태로 만들고 마지막 핸들을 닫으면 종료되는 작업 개체(job)에 배정한 뒤 재개하므로, 게임 부모가 먼저 종료해도 자손 프로세스를 제어한다.

출력 디렉터리는 새 경로여야 한다. `batch.json`은 제출된 `planned_runs` 목록과 시도별 결과를 보존한다. 실행 폴더가 없어져도 원래 분모의 무효 항목으로 남긴다. `findings.jsonl`에는 오라클 발견과 무효 시도를 쓰고, `metrics.json`에는 독립적인 증거 검사와 셀별 집계를 쓴다. Ctrl+C는 대기·실행 중 작업을 취소하고 남은 시도를 기록해 배치를 `interrupted`로 표시한다. 조용히 다시 실행하지 않는다.

## 2.7 QA 산출물과 경계

M3는 `exploration.json`(전체 샘플 계획과 건너뛴 항목), `anomalies.json`·`anomalies.md`(측정된 조사 신호), 별도 출력 디렉터리의 영속 분류 카탈로그를 추가한다. 시그니처 폴더는 초안, 원본 소스 영수증, 정책·테이프 재현, 독립 입력 재생, 선택적인 `clip.json`과 인코딩 클립을 보관한다. 이 단계들은 격리된 M2 실행기를 재사용하며 게임 기본값을 고치지 않는다.

`[explore].variants`는 게임이 정의한 인자 배열을 방문 셀에 추가한다. `[anomalies]`는 최소 실행 수·임계값·그룹을, `[clips]`는 프레임 재생 인자와 선택적인 인코더를 설정한다. 일반 게임 명령 템플릿과 구분하며, 변형 이름이나 심어 둔 버그 이름을 탐지 판정으로 쓰지 않는다. 통제된 벤치마크는 고정 시드 분할과 정규화된 계획 해시를 결과와 함께 남긴다.
