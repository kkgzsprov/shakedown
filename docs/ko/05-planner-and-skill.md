[English](../05-planner-and-skill.md) | **한국어**

# 5. 플래너(planner)와 에이전트 스킬

## 5.1 플래너 백엔드

M4 플래너는 게임 실행 사이에 명령줄 LLM 또는 오프라인 `mock` 백엔드로 구조화된 초안을 만든다. 게임 실행 안에서는 호출하지 않는다. M5는 `campaign --optimize`로 제한된 수치 탐색과 보류 시드 제안 검증을 추가한다. 기본값 자동 적용은 미구현이다.

| 백엔드 | 호출 | 참고 |
|---|---|---|
| `claude-cli` | `claude -p --output-format json --json-schema <JSON 문자열> --max-budget-usd <n>` | 결과 envelope의 `structured_output`을 읽는다. 내장 도구·상속 MCP·사용자 확장을 제한한다. 비용 상한은 응답 후 검사하는 추정치다. |
| `codex-cli` | `codex exec --output-schema <file> -o <file> -s read-only --json` | `codex_mode = "restricted"`를 명시해야 한다. 도구·설정을 추가 제한하지만 읽기 전용은 도구 제거 보장이 아니다. 토큰 사용량만으로 달러 청구액을 확정하지 않는다. |
| `mock` | 입력에서 만든 고정 JSON | 네트워크 없이 파이프라인 전체를 돌린다. |

모든 백엔드의 출력은 단계별 스키마와 의미 제약으로 검사한다. 스키마 오류는 한 번 재시도하고 두 번째도 실패하면 멈춘다. 각 시도는 호출 수와 예산을 사용한다. CLI 내부 재시도가 따로 있을 수 있으므로 이 제한은 공급자 요청 수가 아니라 Shakedown 시도 수다. 오케스트레이터는 입력 파일을 프롬프트에 붙이며 플래너에 Shakedown 실행 도구를 제공하지 않는다.

Claude의 `--tools ""`는 내장 도구를 제거하며 MCP에는 별도의 빈 strict 설정이 필요하다. Codex restricted 모드는 일부 기능을 끄고 JSONL 도구 사용 이벤트를 검사해 해당 결과를 거부한다. 사후 검사는 이미 실행된 도구의 부작용을 되돌리지 못한다. 빈 작업 디렉터리와 사용자 설정 생략도 운영체제 격리나 관리 정책 제거를 보장하지 않는다.

2026-11-06 로컬 Codex 0.166.0과 Claude Code 2.1.318의 도움말 및 공식 문서를 대조했으며 모델 호출은 하지 않았다. [Codex 비대화형 실행](https://developers.openai.com/codex/noninteractive), [Codex 설정](https://developers.openai.com/codex/config-reference), [Claude CLI](https://code.claude.com/docs/en/cli-reference), [Claude 구조화 출력](https://code.claude.com/docs/en/headless)을 참고한다. 실제 CLI별 스키마 검증은 별도 수동 게이트로 남는다.

## 5.2 플래너 단계

| 단계 | 입력 | 출력(스키마) |
|---|---|---|
| `targets` | 게임 디자인 노트, 관측된 지표(있으면) | `targets.json` 초안: 지표, 프로필(profile), 허용 범위. 사람이 편집하고 승인한다. |
| `campaign` | 디자인 노트, 튜닝(tuning) 키 표(key table), 승인된 목표, 이전 보고서 | `campaign.json`: 실험, 가설, 프로필별 지시문, 튜닝 탐색 범위, 배치(batch) 크기 |
| `revise` | `metrics.json`, 레이블, 이상 신호(anomaly) | `revision.json`: 갱신한 가설, 키 표 안의 다음 탐색 범위, 중단 또는 계속 |
| `bug` | 실행 인자, 오라클(oracle) 줄, 마지막 텔레메트리 이벤트 | `bug_draft.json`: 제목, 재현 명령, 기대 결과와 실제 결과, 의심 원인 |
| `report` | 지표와 확보한 보조 증거 | `report.json`과 사람이 읽는 `report.md`. 제안으로 기본값을 적용하지 않는다. |

플래너가 플레이에 영향을 주는 수단은 프로필 지시문뿐이다. 지시문은 한 실행의 모든 결정에서 디사이더(decider)의 질문 앞에 붙는다. 길이는 프로필당 600자로 제한하며, 지시문으로 후보(candidate)나 새 행동을 추가할 수 없다. 스키마는 길이만 검사하므로, 승인 전에 사람이 `campaign.json`을 읽는다.

제안 범위는 설정된 키 표의 제약을 만족해야 채택한다. M5 루프는 수정 범위를 사람이 승인한 캠페인의 탐색 범위로도 제한하며, 잘라 낸 내용을 `revision.json`에 기록하고 새로운 튜닝 키를 거부한다. 수정은 프로필 지시문이나 고정 정책을 바꿀 수 없다.

## 5.3 플래너가 정하지 않는 것

- **목표 범위.** 사람이 정한다. 예: "cautious 프로필 생존율 40~60 %".
- **수치 탐색.** 옵티마이저가 맡는다. LLM과 옵티마이저의 역할을 나눈 RuleSmith의 방식을 따른다.
- **기본값.** 승인된 변경만 게임에 반영된다(7.4절).
- **공정성 판정.** "이 죽음은 공정한가"는 사람 레이블과 모델 레이블을 비교해 답한다. 플래너의 의견은 증거가 되지 못한다.

## 5.4 승인 게이트

| 게이트 | 기본값 | 이유 |
|---|---|---|
| 캠페인 실행 전 | 목표·캠페인 승인 영수증과 실행 시 `--approved` | 지시문과 범위가 실행 방향을 정한다 |
| 플래너 비용 지출 전 | 호출 전에 예산 원장에 금액 예약 | 실패한 시도와 미확인 비용을 회계에서 누락하지 않는다 |
| 기본값 변경 전 | M5는 검토용 패치를 쓰며 어떤 명령도 적용하지 않는다 | 밸런스 변경은 사람이 내릴 디자인 결정이다 |

`approve`는 결과물과 설정 내용에 연결된 운영자의 명시적 승인을 기록한다. 캠페인 승인은 승인된 목표에도 연결된다. 영수증은 서명이 없어 승인자 신원을 인증하지 않으며, 내용 변경 시 재승인이 필요하다. 프로토타입의 직접 기본값 변경은 M4에 포함하지 않는다.

### M4 명령과 예산

```text
shakedown plan {targets,campaign,revise,bug,report} --config PATH --input NAME=PATH --output DIR
  [--backend mock|claude-cli|codex-cli] [--targets PATH --targets-approval PATH]
  [--budget-usd N --call-budget-usd N --max-calls N --ledger PATH]

shakedown approve {targets,campaign} --config PATH --artifact PATH --receipt PATH --approved
  [--targets PATH --targets-approval PATH]

shakedown campaign --config PATH --campaign PATH --approval PATH
  --targets PATH --targets-approval PATH --output DIR --approved [--max-runs N] [--optimize]
```

줄바꿈은 명령 문법을 설명하기 위한 것이며 셸의 줄 연결 표시는 아니다. `--input NAME=PATH`는 반복할 수 있다. 필수 입력 이름은 `targets`·`campaign`의 `design_notes`, `revise`·`report`의 `metrics`, `bug`의 `run`이다. 출력은 차례로 `targets.json`, `campaign.json`, `revision.json`, `bug_draft.json`, `report.json`과 `report.md`다. 캠페인 작성과 승인은 목표 및 목표 승인 영수증을 사용한다. `--optimize`가 없으면 기준선 한 라운드를 실행하며, 이 옵션을 켜면 `[balance]` 설정으로 M5 루프를 실행한다. `--max-runs`는 설정된 실행 상한을 더 낮출 수 있다. 7장을 참고한다.

`[planner]` 설정 키는 `backend`, `budget_usd`, `call_budget_usd`, `max_calls`, `timeout_s`, `model`, `command`, `codex_mode`, `ledger`다. `codex_mode`는 `disabled` 또는 `restricted`다. 실제 호출의 모델은 명시적으로 선택하며 예제에서 임의의 최신 모델이나 fallback을 지정하지 않는다. `plan`의 예산·원장 옵션은 해당 설정을 호출 단위로 덮어쓴다.

원장은 각 시도 전에 `call_budget_usd`를 예약하고 총 예산과 호출 수를 검사한다. 알려진 비용은 기록하며 알 수 없는 비용에는 예약액 전부를 부과한다. 이것은 공급자 청구액의 엄격한 상한이 아니다. 응답이 비용 상한을 넘긴 뒤 종료될 수 있고, Codex에는 검증된 USD 제한이 없다. 실패·중단에도 미보고 비용이 생길 수 있으며 스키마 재시도에도 같은 회계를 적용한다. [Claude 비용 상한의 여유분](https://code.claude.com/docs/en/agent-sdk/agent-loop)과 [실패 시 비용 기록](https://code.claude.com/docs/en/agent-sdk/cost-tracking)을 참고한다.

## 5.5 에이전트 스킬

M6 [`skill/shakedown/`](../../skill/shakedown/SKILL.md)은 Claude Code나 Codex 같은 코딩 에이전트의 Shakedown 작업 흐름을 제공한다. 스킬을 실행하는 에이전트가 플래너이며, 패키지는 다음을 제공한다.

| 구성 | 내용 |
|---|---|
| `SKILL.md` | Shakedown을 쓸 때, 3계층 모델, 명령 목록, 승인 게이트, 절대 하면 안 되는 일(밸런스 변경 커밋, 파일에 키 저장, 공정성 판정) |
| 어댑터(adapter) 가이드 | 3장의 체크리스트를 에이전트가 만들 순서대로 정렬한 것: 헤드리스 모드 → 텔레메트리 → 입력 기록(input recording)과 재생 → 관측 → 후보 → 실행기(executor) → 결정 시점(decision point) → 오라클 |
| 어댑터 템플릿 | C++17, C#, GDScript, Python 전송 골격과 게임 소유 C7 가시성 픽스처 패턴. 게임 입력·재생 연동과 적합성 검사는 여전히 필요하다. |
| 스크립트 | `shakedown check-adapter`, `run`, `explore`, `triage`, `campaign` |

스킬에는 실행기가 들어 있지 않다. 실행기는 게임마다 다른 코드이기 때문이다(3장). 실행기 작성은 스킬이 에이전트에게 맡기는 작업이다. 에이전트는 `check-adapter`로 결과가 동작함을 증명한다. 어댑터는 검사 C1~C3이 통과해야 완료로 친다(3.10절).

`examples/switchyard/`는 두 번째 이산 버튼 어댑터와 스킬 검증 흐름을 제공한다. 배포 어댑터는 스킬을 읽기 전에 구현했으므로 그 검증과 블라인드 생성은 구분한다. 별도 새 컨텍스트 실험은 기존 어댑터를 읽지 않고 스킬·게임 규칙·프로토콜·설정만으로 독립 어댑터를 작성했다. seed 7·depot·courier 범위에서 C1~C6이 통과해 지원 기준 C1~C3을 만족했지만 C7은 `unmeasured`였다. 템플릿 컴파일, 전송 동작, 게임 적합성, 더 넓은 블라인드 일반화는 별도 주장이다. 언어별 검증 범위는 스킬의 안내를 확인한다.

결정마다 이산 행동을 고르는 게임에는 실행기가 거의 필요 없다. 턴제, 카드, 퍼즐, 메뉴 중심 게임이 여기에 속하며, 후보 하나가 버튼 하나나 API 호출 하나에 대응한다. 이런 게임을 위해 템플릿에는 `candidate → input` 표 하나로 처리하는 "얇은 실행기(thin executor)" 모드가 있다.

## 5.6 M6 이후의 에이전트 세션 설계

1. 사용자가 에이전트에게 자기 게임의 플레이테스트를 요청한다.
2. 에이전트는 게임을 읽고 어댑터를 작성하거나 갱신한다. 그다음 C1~C3이 통과할 때까지 `shakedown check-adapter`를 실행한다.
3. 에이전트가 `targets.json`과 `campaign.json` 초안을 쓰고, 사용자에게 둘 다 승인해 달라고 요청한다.
4. 에이전트가 승인된 예산 안에서 `shakedown explore`(QA)나 `shakedown campaign`(밸런스)을 실행한다.
5. 에이전트가 `findings/README.md`, `anomalies.md`, `report.md`를 읽고, 재현된 버그와 제안된 변경을 사용자에게 보여 준다. 각 항목에는 그것을 재생하는 입력 기록이 붙는다.
