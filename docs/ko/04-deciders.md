[English](../04-deciders.md) | **한국어**

# 4. 디사이더(decider, System 1)

디사이더는 `decide` 요청 하나를 받아 선택 하나와 확률을 돌려준다. 디사이더는 브리지 프로세스에서 돈다. 게임에 프로세스 내부 `rule` 디사이더(3.2절)를 둘 수도 있으며, 이 디사이더에는 브리지가 필요 없다.

## 4.1 인터페이스

```python
@dataclass(frozen=True)
class Decision:
    choice: str                       # must be a key of request["candidates"]
    probabilities: dict[str, float]   # over the same keys; may be empty for random/mock
    source: str                       # random | mock | tape | jev | cache | fallback
    attempts: int                     # API attempts, 0 for local deciders
    latency_ms: float                 # wall time of the whole call, retries included
    model: str | None                 # exact model ID the provider reported

class Decider(Protocol):
    def decide(self, request: dict, instructions: str) -> Decision: ...
    def close(self) -> None: ...
```

`instructions`는 이번 실행(run)에 쓰는 플래너(planner)의 프로필(profile) 텍스트다(5.2절). 플래너가 플레이에 영향을 주는 경로는 이것 하나뿐이다.

`latency_ms`는 재시도까지 포함해 잰다. 프로토타입 작업 중에 호스팅 결정 모델의 평가 스크립트 하나가 마지막 시도의 시간만 기록하는 것을 발견했다. 그렇게 재면 재시도 비용이 드러나지 않는다.

## 4.2 백엔드

| 백엔드 | 네트워크 | 용도 |
|---|---|---|
| `random` | 없음 | 합법 후보(candidate) 가운데 균등하게 고르며, 시드(seed)를 고정한다. 다른 모든 디사이더가 넘어야 하는 하한선이다(실험 E1). |
| `mock` | 없음 | 요청의 해시로 결정적으로 고른다. 파이프라인을 시험할 때 쓰며, 게임 시험용은 아니다. |
| `tape` | 없음 | 녹화된 테이프(tape)의 답을 순서대로 낸다. 어긋나면 실행이 끝난다. |
| `jev` | 있음 | 선택적 TypeSafe Jev `POST /v1/systemone` 질문, 정확한 모델 고정, 공유 캐시·실행 허용 원장을 구현했다. 실제 공급자 검증은 남아 있다. |
| 로컬 오픈 모델 | 없음 | 계획 단계다(9.3절). 검토 대상으로 Laya(공개 인코더와 결정 헤드)와 SemIf(고정한 LM의 레이블 로짓)가 있다. 둘 다 Shakedown과 함께 시험하지 않았다. |

Jev는 요청 해시·프로필 지시문·모델·엔드포인트를 묶어 성공한 답을 캐시하고 적중은 `source: "cache"`로 집계한다. 선택적 SQLite 캐시는 실행 사이에도 유지된다. 캐시와 원장 경로는 프로젝트 설정 파일을 기준으로 한다.

## 4.3 호스팅 백엔드 예: Jev

구현은 다음 요청 형태를 보낸다. 아래 모델 고정값은 예시이며 현재 그 버전이 공급자에게 제공된다는 주장이 아니다.

```json
{
  "model": "jev-1.14.0",
  "state": { "...": "the game's state object, unchanged" },
  "questions": {
    "tactic": {
      "type": "choice",
      "instructions": "<profile text from the planner>",
      "criteria": { "cover_c1": "Move to cover c1. 6 m, ...", "engage_e2": "...", "...": "..." }
    }
  }
}
```

- 정확한 모델 ID를 고정한다. `latest` 같은 별칭은 쓰지 않는다. 응답마다 모델 ID를 기록하고, 도중에 모델이 바뀐 캠페인(campaign)은 무효로 한다. 버전이 섞이면 비교할 수 없다.
- 선택은 `argmax`이며 동률 최대값은 후보 이름 정렬로 고른다. 시드 고정 호스팅 샘플링은 구현하지 않았다.
- 키는 설정된 환경 변수에서 읽으며 기본 이름은 `TYPESAFE_API_KEY`다. 키 파일 읽기는 미구현이다. 실행기는 게임 환경에서 호스팅 키를 제거한다. 설정·로그·테이프·예외 메시지에 키를 쓰지 않는다.
- `probabilities`는 후보 설명에 대한 모델의 판단으로 읽는다. 행동이 성공할 확률로 읽으면 안 된다. 신뢰도 정의도 모델마다 다르다. Jev 문서는 `(pmax − 1/n) / (1 − 1/n)`을 쓰고, Laya는 `1 − H(p) / log(n)`을 쓴다. 디사이더끼리 임계값을 재사용하지 않는다.

프로토타입은 실행 네 번에서 p50 약 210 ms, p95 약 235 ms를 측정했고 비용은 60 s 실행 한 번에 $0.0005~$0.0025였다(1.5절). 이는 현재 백엔드의 검증이 아닌 과거 측정이다. 현재 로컬 HTTP 픽스처는 요청·응답·타임아웃·재시도·캐시·회계 동작을 검증한다. 실제 추론·운영 지연·실제 청구액은 미측정이다.

`jev`를 선택하기 전에 실제 제공되는 정확한 모델과 승인된 예산을 설정한다. 아래 TOML은 구현된 설정의 예시이며 모델 값의 현재 제공 여부는 주장하지 않는다.

```toml
[decider]
backend = "jev"
model = "jev-1.14.0"
budget_usd = 0.05
call_budget_usd = 0.001
max_calls = 50
timeout_s = 3
retries = 2
backoff_s = 0.1
max_elapsed_s = 12
input_price_per_million = 0.042
api_key_env = "TYPESAFE_API_KEY"
cache = "cache/jev.sqlite3"
ledger = "jev-budget.json"
```

엔드포인트 기본값은 `https://api.typesafe.ai/v1/systemone`이다. 재정의는 HTTPS를 요구하며 평문 HTTP는 루프백 테스트 픽스처에서만 허용한다. 리디렉션은 따라가지 않는다. `cache`와 `ledger`는 선택 사항이다. `input_price_per_million`은 설정한 입력 토큰 요율 추정이며 검증된 공급자 청구액이나 실시간 가격 조회가 아니다. 출력 토큰 수는 별도로 보존한다.

계정 환경과 승인된 설정을 준비한 뒤 아래 문법으로 호스팅 경로를 고른다.

```text
shakedown run --config CONFIG --output NEW_BATCH_DIR --deciders jev
shakedown bridge --decider jev --config CONFIG --run-dir NEW_RUN_DIR -- GAME_COMMAND ARGUMENTS
```

대문자 값은 운영자가 제공할 경로와 인자 배열이며 그대로 복사할 셸 명령이 아니다. 독립 브리지의 Jev는 `--config`가 필요하다. 배치는 호스팅 실행들 사이에 하나의 런타임 캐시·USD 예약·호출 수·동시성 제한을 공유한다. 플래너와 게임 플레이 디사이더의 예산은 별도다.

## 4.4 실패 처리와 예산

| 상황 | 브리지 동작 | 기록 |
|---|---|---|
| 타임아웃(기본 3 s), 5xx, 연결 오류 | `max_elapsed_s` 안에서 지수 백오프로 최대 두 번 재시도한 뒤 대체(fallback)한다 | `source: "fallback"`, 시도 횟수, 전체 호출 지연 |
| HTTP 429 | 공유 런타임의 동시 실행 상한을 절반으로 줄이고 같은 제한 안에서 재시도한다 | 상한 변경 이벤트 |
| 400·422·리디렉션 등 다른 HTTP 오류 | 자동 재시도 없이 중단·무효 처리하며 공급자 본문이나 인증 정보를 덤프하지 않는다 | 정리된 오류 상세 |
| 한 실행에서 대체 비율이 10 %를 넘음 | 실행을 끝까지 마치고 무효로 표시한다 | `bridge_summary.json` |
| 캠페인 도중 모델 ID가 바뀜 | 영향받은 실행을 무효로 표시한다 | `bridge_summary.json` |
| 실행 허용 예산·호출 수 소진 | 새 호스팅 시도를 허용하지 않고 무효 시도·실행 증거를 남긴다 | 공유 원장과 브리지 요약 |

구현된 호스팅 대체는 시드 고정 `random`이며 게임별 `rule` 대체는 연결하지 않았다. 집계 단계는 무효 실행을 `invalid_runs`로 보고하고 디사이더 비교에서 뺀다. 모델 불일치·모르는 선택·잘못된 확률·응답 형식 오류는 조용히 수용하지 않고 실행을 멈춘다.

모든 HTTP 시도는 전송 전에 `call_budget_usd`를 예약한다. 토큰 기준 `estimated_cost_usd`와 실제 `cost_usd`는 별도이며 HTTP가 발생했다면 실제 비용은 미확인이다. 실패 요청이나 사용량 누락은 전체 추정도 미확인으로 만들고 예약은 계속 차감한다. 알려진 부분 토큰 추정은 따로 보존한다. 사용하지 않은 사전 예약은 종료 시 해제한다. 원장은 공급자의 최종 청구액이 아닌 허용 작업량을 제한한다. 브리지·배치 영수증은 API 키 없이 HTTP 시도·캐시 적중·모델·토큰·추정액·대체·지연·상한 변경을 남긴다.

## 4.5 디사이더가 하면 안 되는 일

- 좌표, 조준, 경로 계산. 어댑터(adapter)가 이미 처리했다.
- 합법성 판정. 디사이더가 보는 후보는 모두 이미 합법이다.
- 행동 지어내기. 후보 밖의 선택은 계약 오류다.
- 요청 사이에 숨은 상태 유지. 디사이더가 기억해야 할 것은 `state.recent`나 `state.memory`에 넣어 테이프가 재생할 수 있게 한다.
