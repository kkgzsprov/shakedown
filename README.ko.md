[English](README.md) | **한국어**

# Shakedown

오프라인 검증 경로를 제공하는 게임용 AI 플레이테스트 파이프라인이다. 게임 쪽 실행기가 일반 입력 경로로 플레이하고, Shakedown은 결정과 입력을 기록해 지원되는 결정론적 실패를 재현한다. 플래너는 실행 사이에 구조화된 초안을 만든다. 승인된 캠페인은 기준선을 측정하거나 튜닝 값을 탐색해 보류 시드에서 변경 제안을 검증한다.

> 상태: 초기 단계. M0~M7의 실행 경로를 구현했다. 프로토콜·오프라인 디사이더, 두 예제 게임, 재생, 격리 배치, QA, 플래너 초안, 승인된 밸런스 탐색, 에이전트 스킬과 선택적 Jev 호스팅 디사이더가 있다. 실제 플래너·공급자 검증, 더 넓은 처음 보는 게임의 어댑터 작성, 사람 플레이 검증은 남은 과제다. 어떤 명령도 제안 기본값을 적용하거나 커밋하지 않는다. [설계 문서](docs/ko/README.md)와 [로드맵](docs/ko/09-roadmap.md)을 참고한다.


## 동작 방식

전체 설계는 동작 속도가 다른 세 계층으로 구성된다. 플래너는 배치 사이에서 동작하며, 게임 플레이는 기본적으로 오프라인 디사이더를 쓰고 설정한 Jev 백엔드를 선택할 수 있다.

- **플래너(planner).** Claude Code나 Codex 같은 LLM이 실행(run) 배치(batch) 사이에 테스트 캠페인을 작성한다. 플레이어 프로필(profile)을 고르고, 가설을 세우고, 탐색할 튜닝(tuning) 범위를 정한다. 플레이 중에는 호출하지 않는다.
- **디사이더(decider).** 빠른 결정 모델이 게임이 만든 합법적 선택지 목록에서 전술 하나를 고른다. 좌표 계산이나 조준은 하지 않는다. 무작위 기준선, 게임 규칙, 테이프 재생기는 네트워크 없이 동작한다.
- **실행기.** 게임 안의 코드가 선택된 전술을 매 프레임 키보드·마우스 입력으로 바꾼다. 이때 사람 플레이어가 쓰는 것과 같은 입력 경로를 쓴다. 실행기는 게임별 어댑터(adapter)의 일부이며, 동봉된 [Shakedown 스킬](skill/shakedown/SKILL.md)이 가이드와 전송 골격을 제공한다.

디사이더가 생각하는 동안 게임 시간이 멈추므로, 모델 지연 시간은 결과를 바꾸지 않는다. 답은 하나씩 테이프에 기록되므로, 어떤 실행이든 모델을 다시 호출하지 않고 그대로 재생할 수 있다.

코드 수준의 오라클은 끼임·순간이동·수치 범위를 검사하고, 실행기는 크래시와 다른 무효 시도를 기록한다. 분류·재현 단계는 확인된 발견과 실패·누락된 증거를 구분하고, 재생이 일치하면 클립을 만들 수 있다. 밸런스 루프는 탐색 시드와 보류 시드를 분리하며 사람 검토용 제안을 만든다.

## 실제 실행 예시

![입력 재생으로 검증한 tinyraid 12초 클립 4개: Jev 조심형, Jev 공격형, Jev 약탈형, rule 디사이더의 탈출](docs/media/live-playtest-codex-jev.gif)

[MP4 버전 (47초, H.264)](docs/media/live-playtest-codex-jev.mp4)

2026-11-06에 탐색 목적으로 한 번 돌린 캠페인의 클립이며 검증용 실행이 아니다. Codex(`gpt-6.1-sol`, 추론 medium)가 `plan`으로 목표와 36회 기준선 캠페인 초안을 만들었고, 운영자가 두 초안을 승인했다. 이어서 Jev(`jev-1.13.0`)와 오프라인 `rule` 디사이더로 시드 3개 × 맵 2개 × 프로필 3개를 각 12초(시뮬레이션 시간) 실행했다. 모든 클립은 입력 재생이 원본 실행의 이벤트를 재현한 뒤에만 만들었다. 클립 순서는 Jev 조심형, Jev 공격형, Jev 약탈형, `rule` 공격형(탈출)이다. 화면에서 하늘색은 플레이어, 빨강은 경비병, 노랑은 상자(약탈 후 회색), 갈색은 문, 초록은 탈출 구역이다.

실행에서 확인한 것:
- 36회 모두 유효했고 오라클 발견은 없었다.
- Jev는 결정 247회 중 17회(6.9 %)를 `random`으로 대체했다. 지연은 p50 218 ms, p95 306 ms였다.
- 프로필 지시문에 따라 Jev의 전술 구성이 달라졌다. 조심형은 `hold`/`withdraw`, 공격형은 `hold`/`engage`, 약탈형은 `search`가 가장 많았다.
- 결과 지표는 `rule`이 더 좋았다. `rule` 공격형은 6회 모두 탈출했고, Jev 공격형은 한 번도 움직이지 않았다.
- 시드 3개와 12초 실행으로는 디사이더 가치(로드맵의 E1 실험)를 판단할 수 없다. 이 결과는 예시일 뿐 모델 디사이더가 낫다는 증거가 아니다.

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

브리지는 `decision_tape.jsonl`, `bridge_summary.json`, `stdout.log`를 쓰고, 게임은 지정한 텔레메트리와 입력 기록을 쓴다. 고정된 M1 시드 조합, 버그 픽스처, 선택적 PNG 프레임은 [8장](docs/ko/08-example-game.md)에 있다. 아래 명령은 배치·적합성 검사, QA와 M4 플래너를 다룬다.

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

QA CLI 종료 코드는 단계별 의미를 가진다. `explore`는 실행·오라클 발견이나 이상 신호가 있으면 1, 중단은 130이고, `anomalies`는 신호가 있으면 1이다. `triage`는 재현 실패나 미측정 시그니처가 남으면 1이다. `bench`는 합격 게이트를 통과해야 0이며, 실패·미자격은 1, 중단은 130이다. 설정 오류는 2다.

통제된 tinyraid 벤치마크는 개발용 시드와 보류 시드를 분리하고, 정상 게임과 버그 하나만 켠 게임에 같은 입력 워크플로 프로브를 적용한다. 전체 명령, 합격 기준, 시험대의 커버리지와 처음 보는 게임의 자율 탐색 사이 경계는 [8장](docs/ko/08-example-game.md#84-벤치마크)에 있다.

## 플래너 초안과 승인된 캠페인

M4는 `plan`, `approve`, `campaign`을 제공한다. `plan` 단계는 `targets`, `campaign`, `revise`, `bug`, `report`이며, `--backend mock`으로 오프라인 초안을 만들 수 있다. 이름이 붙은 입력 파일은 `--input NAME=PATH`를 반복해 전달한다. 초안은 스키마와 의미 제약을 검사한다. 전체 명령·설정 계약은 [5장](docs/ko/05-planner-and-skill.md)에 있다.

```text
python -m shakedown plan targets --config CONFIG --input design_notes=DESIGN_FILE --output DRAFT_DIR --backend mock
python -m shakedown approve targets --config CONFIG --artifact TARGETS_JSON --receipt TARGETS_RECEIPT --approved
python -m shakedown plan campaign --config CONFIG --input design_notes=DESIGN_FILE --targets TARGETS_JSON --targets-approval TARGETS_RECEIPT --output CAMPAIGN_DIR --backend mock
python -m shakedown approve campaign --config CONFIG --artifact CAMPAIGN_JSON --receipt CAMPAIGN_RECEIPT --targets TARGETS_JSON --targets-approval TARGETS_RECEIPT --approved
python -m shakedown campaign --config CONFIG --campaign CAMPAIGN_JSON --approval CAMPAIGN_RECEIPT --targets TARGETS_JSON --targets-approval TARGETS_RECEIPT --output RUN_DIR --approved
```

대문자 값은 운영자가 지정할 경로다. 첫 초안은 `DRAFT_DIR/targets.json`, 다음 초안은 `CAMPAIGN_DIR/campaign.json`에 저장된다. 내용을 검토한 뒤 승인 영수증을 만든다. 영수증은 결과물과 설정 내용에 승인을 연결하지만, 승인자 신원을 인증하는 서명은 아니다. 승인된 내용을 바꾸면 다시 승인해야 한다.

`--optimize`가 없으면 기준선 한 라운드를 실행한다. 승인된 캠페인과 `[balance]` 설정에 `--optimize`를 추가하면 M5 밸런스 루프를 실행한다. 정책·프로필 지시문을 고정하고 탐색 시드로 후보를 정렬한 뒤, 선택된 후보를 고정해 분리된 보류 시드에서 한 번 평가한다. 모든 실험은 캠페인의 공통 탐색 범위를 써야 한다. 제안하려면 최소 두 개의 검증된 정책이 필요하며, 그 정책 사이의 관측된 일치만 확인한다. 설정·게이트·출력 파일은 [7장](docs/ko/07-balance-loop.md)에 있다.

통과한 후보는 `defaults.patch`, `tuning.json`, `proposal.json`을 만든다. 어떤 명령도 이를 적용하거나 커밋하지 않는다. 패치는 튜닝 키 표의 문서화된 기본값을 바꾼다. tinyraid는 이 표를 실제 런타임 기본값으로 읽지만, 다른 어댑터에서는 사람이 패치를 적용하기 전에 게임 연동을 확인해야 한다.

플래너 호출 전 예산 원장에 금액을 예약하며, 비용을 알 수 없으면 예약액 전부를 사용한 것으로 처리한다. 예약은 Shakedown이 허용할 호출을 제한하며 공급자의 최종 청구액 상한을 보장하지 않는다. Codex는 `codex_mode = "restricted"`를 명시해야 하며 도구 사용이 완전히 제거됐다는 보장은 없다. 실제 CLI 호출 검증에는 별도로 승인된 예산이 필요하고, 오프라인 검증이 이를 대신하지 않는다.

## 두 번째 어댑터와 에이전트 스킬

`examples/switchyard/`는 각 후보를 일반 버튼으로 바꾸는 얇은 실행기를 가진 작은 턴제 경로 퍼즐이다. 별도 설정으로 어댑터 검사와 오프라인 배치를 실행한다.

```bash
python -m shakedown check-adapter --config examples/switchyard/shakedown.toml --output runs/switchyard-checks
python -m shakedown run --config examples/switchyard/shakedown.toml --output runs/switchyard-batch
```

출력은 새 디렉터리를 쓴다. 배포 어댑터는 스킬을 읽기 전에 구현했으므로 그 검증만으로는 블라인드 어댑터 생성을 입증하지 않는다. 별도 새 컨텍스트 실험은 스킬·게임 핵심 규칙·프로토콜·설정만 읽고 기존 어댑터 없이 독립 어댑터를 작성했다. 이 실험은 seed 7·depot·courier에서 C1~C6을 통과해 최소 지원 기준 C1~C3을 만족했으며 C7은 `unmeasured`다. 넓은 게임 일반화나 C7 통과를 뜻하지 않는다. [스킬](skill/shakedown/SKILL.md)은 어댑터·얇은 실행기 가이드, Python/C++17/C#/GDScript 전송 골격, 가시성 픽스처 패턴과 휴대용 명령 래퍼를 제공하며 게임 적합성 검사를 대신하지 않는다.

## 선택적 호스팅 결정

M7의 `jev` 백엔드는 정확한 모델 고정, 공유 캐시·원장, 제한된 재시도와 지연·대체·사용량 영수증을 갖춰 브리지로 고정 선택 질문을 보낸다. API 키는 설정된 환경 변수에만 두며 게임 프로세스 환경에서는 제거한다. 설정과 명령은 [4장](docs/ko/04-deciders.md)에 있다. 로컬 HTTP 픽스처는 전송 동작을 검증하며 실제 호스팅 추론·운영 지연·실제 청구액은 미측정이다. 토큰 비용 추정과 보수적 예약은 공급자의 실제 청구액을 입증하지 않는다.

## 라이선스

[Apache License 2.0](LICENSE).
