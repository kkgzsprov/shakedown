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
| `jev` | 있음 | TypeSafe Jev `POST /v1/systemone`에 `choice` 질문을 보낸다. 선택 사항이다. |
| 로컬 오픈 모델 | 없음 | 계획 단계다(9.3절). 검토 대상으로 Laya(공개 인코더와 결정 헤드)와 SemIf(고정한 LM의 레이블 로짓)가 있다. 둘 다 Shakedown과 함께 시험하지 않았다. |

캐시는 어떤 네트워크 백엔드든 감쌀 수 있다. 해시가 같은 요청은 앞선 답을 재사용하고 `source: "cache"`로 집계된다.

## 4.3 호스팅 백엔드 예: Jev

프로토타입의 요청은 다음과 같다.

```json
{
  "model": "jev-1.14.0",
  "state": { "...": "the game's state object, unchanged" },
  "questions": {
    "tactic": {
      "type": "choice",
      "instructions": "<profile text from the planner> Choose what this player does for the next few seconds.",
      "criteria": { "cover_c1": "Move to cover c1. 6 m, ...", "engage_e2": "...", "...": "..." }
    }
  }
}
```

- 정확한 모델 ID를 고정한다. `latest` 같은 별칭은 쓰지 않는다. 응답마다 모델 ID를 기록하고, 도중에 모델이 바뀐 캠페인(campaign)은 무효로 한다. 버전이 섞이면 비교할 수 없다.
- 기본 선택 규칙은 `argmax`다. 탐색용으로 온도 1의 시드 고정 샘플링을 허용하며, 어떤 규칙을 썼는지 테이프에 기록한다.
- 키는 환경 변수(`TYPESAFE_API_KEY`)나 저장소 밖의 키 파일에서 읽는다. 로그, 테이프, 예외 메시지에 키를 쓰지 않는다.
- `probabilities`는 후보 설명에 대한 모델의 판단으로 읽는다. 행동이 성공할 확률로 읽으면 안 된다. 신뢰도 정의도 모델마다 다르다. Jev 문서는 `(pmax − 1/n) / (1 − 1/n)`을 쓰고, Laya는 `1 − H(p) / log(n)`을 쓴다. 디사이더끼리 임계값을 재사용하지 않는다.

프로토타입은 실행 네 번에서 p50 약 210 ms, p95 약 235 ms를 측정했다. 비용은 60 s 실행 한 번에 $0.0005~$0.0025였다(1.5절). 속도 제한(rate limit)은 측정하지 않았다.

## 4.4 실패 처리와 예산

| 상황 | 브리지 동작 | 기록 |
|---|---|---|
| 타임아웃(기본 3 s), 5xx, 연결 오류 | 지수 백오프로 두 번 재시도한 뒤 대체(fallback)한다 | `source: "fallback"`, 시도 횟수 |
| 429 또는 속도 제한 응답 | 동시 실행 상한을 절반으로 줄이고 재시도한다 | 상한 변경 이벤트 |
| 400 또는 422 | 재시도하지 않는다. 요청을 덤프하고 실행을 멈춘다. | 오류 상세(잘라서 기록) |
| 한 실행에서 대체 비율이 10 %를 넘음 | 실행을 끝까지 마치고 무효로 표시한다 | `bridge_summary.json` |
| 캠페인 도중 모델 ID가 바뀜 | 영향받은 실행을 무효로 표시한다 | `bridge_summary.json` |
| 캠페인 예산 도달 | 새 실행을 시작하지 않는다 | 비용 합계 |

대체 디사이더는 게임이 `rule`을 제공하지 않으면 `random`이다. 집계 단계는 무효 실행을 `invalid_runs`로 보고하고 디사이더 비교에서 뺀다.

## 4.5 디사이더가 하면 안 되는 일

- 좌표, 조준, 경로 계산. 어댑터(adapter)가 이미 처리했다.
- 합법성 판정. 디사이더가 보는 후보는 모두 이미 합법이다.
- 행동 지어내기. 후보 밖의 선택은 계약 오류다.
- 요청 사이에 숨은 상태 유지. 디사이더가 기억해야 할 것은 `state.recent`나 `state.memory`에 넣어 테이프가 재생할 수 있게 한다.
