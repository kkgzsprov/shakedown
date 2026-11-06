[English](README.md) | **한국어**

# Shakedown

게임용 오프라인 AI 플레이테스트 파이프라인이다. 게임 쪽 실행기가 일반 입력 경로로 플레이하고, Shakedown은 결정과 입력을 기록해 지원되는 결정론적 실패를 재현한다. LLM 캠페인 계획 연동은 다음 마일스톤이다.

> 상태: 초기 단계. M0~M3를 구현했다. 프로토콜·오프라인 디사이더, tinyraid·재생, 격리 배치·어댑터 검사에 더해 탐색·이상 신호·검증된 분류와 재현·클립·QA CLI 종료 코드는 단계별 의미를 가진다. `explore`는 실행·오라클 발견이나 이상 신호가 있으면 1, 중단은 130이고, `anomalies`는 신호가 있으면 1이다. `triage`는 재현 실패나 미측정 시그니처가 남으면 1이다. `bench`는 합격 게이트를 통과해야 0이며, 실패·미자격은 1, 중단은 130이다. 설정 오류는 2다.

통제된 tinyraid 벤치마크를 제공한다. M4 이후는 설계 단계다. [설계 문서](docs/ko/README.md)와 [로드맵](docs/ko/09-roadmap.md)을 참고한다.

## 동작 방식

전체 설계는 동작 속도가 다른 세 계층으로 구성된다. 플래너 연동은 M4부터이며, 현재 실행에는 오프라인 디사이더를 사용한다.

- **플래너(planner, System 2).** Claude Code나 Codex 같은 LLM이 실행(run) 배치(batch) 사이에 테스트 캠페인을 작성한다. 플레이어 프로필(profile)을 고르고, 가설을 세우고, 탐색할 튜닝(tuning) 범위를 정한다. 플레이 중에는 호출하지 않는다.
- **디사이더(decider, System 1).** 빠른 결정 모델이 게임이 만든 합법적 선택지 목록에서 전술 하나를 고른다. 좌표 계산이나 조준은 하지 않는다. 무작위 기준선, 게임 규칙, 테이프 재생기는 네트워크 없이 동작한다.
- **실행기.** 게임 안의 코드가 선택된 전술을 매 프레임 키보드·마우스 입력으로 바꾼다. 이때 사람 플레이어가 쓰는 것과 같은 입력 경로를 쓴다. 실행기는 게임별 어댑터(adapter)의 일부이며, 동봉된 에이전트 스킬이 어댑터 작성을 돕는다.

디사이더가 생각하는 동안 게임 시간이 멈추므로, 모델 지연 시간은 결과를 바꾸지 않는다. 답은 하나씩 테이프에 기록되므로, 어떤 실행이든 모델을 다시 호출하지 않고 그대로 재생할 수 있다.

코드 수준의 오라클은 끼임·순간이동·수치 범위를 검사하고, 실행기는 크래시와 다른 무효 시도를 기록한다. 분류·재현 단계는 확인된 발견과 실패·누락된 증거를 구분하고, 재생이 일치하면 클립을 만들 수 있다. 최적화기와 밸런스 제안 루프는 앞으로 구현한다.

## 요구 사항

- Shakedown 쪽: Python 3.11 이상, 표준 라이브러리만.
- 시드에서 시작해 고정 시뮬레이션 스텝으로 헤드리스 실행이 가능하고, 자기 시계를 멈출 수 있는 게임. 전체 계약은 설계 문서 3장에 있다.

## 돌려 보기

테스트에는 네트워크도 설치도 필요 없다.

```bash
python -m unittest discover -s tests -v
```

저장소 루트에서 tinyraid의 로컬 규칙으로 실행하고, 기록된 입력을 디사이더 없이 재생한다.

```bash
python -m examples.tinyraid --seed 7 --duration 45 --profile-name looter --decider rule --telemetry runs/tinyraid-rule/telemetry.jsonl --record-inputs runs/tinyraid-rule/inputs.json
python -m examples.tinyraid --replay-inputs runs/tinyraid-rule/inputs.json --telemetry runs/tinyraid-input-replay/telemetry.jsonl
```

패키지를 설치해 브리지를 쓰고, `mock` 실행을 기록한 뒤 브리지의 결정 테이프를 어댑터 안에서 재생한다.

```bash
python -m pip install --no-build-isolation -e .
python -m shakedown bridge --decider mock --run-dir runs/tinyraid-mock -- python -m examples.tinyraid --seed 7 --duration 12 --decider bridge --telemetry runs/tinyraid-mock/telemetry.jsonl --record-inputs runs/tinyraid-mock/inputs.json
python -m examples.tinyraid --seed 7 --duration 12 --decider tape:runs/tinyraid-mock/decision_tape.jsonl --telemetry runs/tinyraid-tape/telemetry.jsonl
```

설치는 기존 빌드 도구를 사용한다. `setuptools`가 없으면 먼저 설치한다. Python의 Scripts 디렉터리가 `PATH`에 있으면 `python -m shakedown` 대신 `shakedown`을 쓸 수 있다. 예제는 소스 작업본에 있으므로 저장소 루트에서 명령을 실행한다.

브리지는 `decision_tape.jsonl`, `bridge_summary.json`, `stdout.log`를 쓰고, 게임은 지정한 텔레메트리와 입력 기록을 쓴다. 고정된 M1 시드 조합, 버그 픽스처, 선택적 PNG 프레임은 [8장](docs/ko/08-example-game.md)에 있다. 아래 명령은 배치·적합성 검사와 M3 QA를 다룬다. 플래너는 M4에서 구현한다.

## 배치 실행과 어댑터 검사

위의 편집 가능 설치 후 다음 짧은 배치는 시드 하나로 맵 2개와 프로필 3개를 실행한다. 적합성 검사는 별도 증거를 남긴다.

```bash
python -m shakedown run --config examples/tinyraid/shakedown.toml --output runs/tinyraid-batch --seeds 1 --seconds 1 --workers 2
python -m shakedown check-adapter --config examples/tinyraid/shakedown.toml --output runs/tinyraid-checks
```

명령마다 새 출력 디렉터리를 사용한다. 기존 증거는 덮어쓰지 않는다. `--seeds`와 `--seconds`를 생략하면 예제 설정의 60회(시드 10개 × 맵 2개 × 프로필 3개, 각 12초)를 실행한다. `--seconds 45`는 시뮬레이션 제한을 늘리고, `--deciders rule,mock,random`은 오프라인 정책 비교를 추가한다.

배치는 `batch.json`, `metrics.json`, `findings.jsonl`과 실행별 디렉터리를 쓴다. 무효 실행도 제출된 실행 수에 남기되 비교에서는 제외한다. 적합성 검사는 C1~C7 판정과 증거 경로가 담긴 `conformance.json`을 쓴다. 게임별 픽스처가 없으면 통과가 아니라 `unmeasured`로 표시한다.

`run`은 발견이 없으면 0, 발견이 있으면 1, 설정 오류는 2, 중단 후 남은 실행을 기록하면 130으로 종료한다. `check-adapter`는 모든 검사가 통과해야 0이며, 실패나 미측정 검사가 있으면 1, 설정 오류는 2다. 이 CLI 코드는 게임의 오라클 종료 코드 4와 구분한다.

## 탐색·분류와 재현·클립

제한된 탐색은 설정된 맵·프로필·오프라인 디사이더를 사용한다. 다음 명령은 샘플 계획, 측정된 신호, 재현 증거를 구분해 남긴다.

```bash
python -m shakedown explore --config examples/tinyraid/shakedown.toml --output runs/tinyraid-explore --runs 6 --seed 17
python -m shakedown anomalies --config examples/tinyraid/shakedown.toml --batch runs/tinyraid-explore
python -m shakedown triage --config examples/tinyraid/shakedown.toml --batch runs/tinyraid-explore --output runs/tinyraid-triage --clips
```

탐색은 맵·프로필·디사이더·변형 조합 중 덜 방문한 셀을 우선하고, 셀별로 섞은 시드 풀을 모두 쓴 뒤 재사용한다. `--deadline <초>`가 새 실행과 현재 실행의 시간을 제한하도록 순차 실행한다. `exploration.json`에는 전체 계획과 건너뛴 항목이 남고, 배치 분모에는 실제 시도만 들어간다. `--tuning-noise 1`~`3`은 키 표 범위 안의 값을 뽑고, `--no-steer`는 방문 수에 따른 선택을 끈다.

`anomalies.json`과 `anomalies.md`는 조사할 측정 그룹을 가리킨다. 최소 표본 수를 만족하고 Wilson 하한이 설정 임계값과 전체 측정 비율 또는 `--baseline`으로 지정한 정상 배치 비율을 넘어야 표시한다. 기회를 측정하지 못한 항목은 미측정이며, 신호만으로 버그를 확정하지 않는다.

분류·재현 단계는 `findings/index.json`, 요약, 소스 해시, 결정론적 `bug_draft.json`, 재현 시도를 남긴다. 오라클·이상 신호 확인에는 정책 또는 테이프 재현과 독립 입력 재생이 모두 필요하다. 기존 시그니처는 중복 집계하며 조용히 재시도하지 않는다. 초안은 LLM을 호출하거나 원인을 지어내지 않는다. `--clips`를 주면 입력 재생이 일치한 뒤 애니메이션 GIF를 만들 수 있고, 별도 인코더를 설정해 다른 형식으로 출력할 수 있다.

통제된 tinyraid 벤치마크는 개발용 시드와 보류 시드를 분리하고, 정상 게임과 버그 하나만 켠 게임에 같은 입력 워크플로 프로브를 적용한다. 전체 명령, 합격 기준, 시험대의 커버리지와 처음 보는 게임의 자율 탐색 사이 경계는 [8장](docs/ko/08-example-game.md#84-벤치마크)에 있다.

## 라이선스

아직 정하지 않았다([열린 질문](docs/ko/09-roadmap.md#94-열린-질문) 참고).
