[English](README.md) | **한국어**

# Shakedown

게임용 AI 플레이테스터다. LLM이 테스트 캠페인(campaign)을 계획하고, 빠른 결정 모델이 전술을 고르고, 게임 쪽 실행기(executor)가 실제 입력 경로로 게임을 플레이한다. 모든 결정을 테이프(tape)에 기록하므로 어떤 버그든 결정론적으로 재생된다.

> 상태: 초기 단계. 마일스톤 M0(와이어 프로토콜, 브리지, `random`·`mock`·`tape` 디사이더)을 구현했고 가짜 게임으로 시험했다. 나머지는 아직 설계다. [설계 문서](docs/ko/README.md)와 [로드맵](docs/ko/09-roadmap.md)을 참고한다.

## 동작 방식

Shakedown은 봇 플레이어를 세 계층으로 돌리며, 계층마다 동작 속도가 다르다.

- **플래너(planner, System 2).** Claude Code나 Codex 같은 LLM이 실행(run) 배치(batch) 사이에 테스트 캠페인을 작성한다. 플레이어 프로필(profile)을 고르고, 가설을 세우고, 탐색할 튜닝(tuning) 범위를 정한다. 플레이 중에는 호출하지 않는다.
- **디사이더(decider, System 1).** 빠른 결정 모델이 게임이 만든 합법적 선택지 목록에서 전술 하나를 고른다. 좌표 계산이나 조준은 하지 않는다. 무작위 기준선, 게임 규칙, 테이프 재생기는 네트워크 없이 동작한다.
- **실행기.** 게임 안의 코드가 선택된 전술을 매 프레임 키보드·마우스 입력으로 바꾼다. 이때 사람 플레이어가 쓰는 것과 같은 입력 경로를 쓴다. 실행기는 게임별 어댑터(adapter)의 일부이며, 동봉된 에이전트 스킬이 어댑터 작성을 돕는다.

디사이더가 생각하는 동안 게임 시간이 멈추므로, 모델 지연 시간은 결과를 바꾸지 않는다. 답은 하나씩 테이프에 기록되므로, 어떤 실행이든 모델을 다시 호출하지 않고 그대로 재생할 수 있다.

코드 수준의 오라클(oracle)이 끼인 플레이어, 순간이동, 범위를 벗어난 스탯, 크래시를 검사한다. 모든 발견(finding)에는 시드(seed), 리플레이 파일, 그리고 선택적으로 짧은 영상이 붙는다. 배치 사이에는 최적화기가 밸런스 변경을 제안한다. 제안은 보류 시드(holdout seed)에서 검증한 뒤 패치로 작성하며, 사람 없이는 적용하지 않는다.

## 요구 사항

- Shakedown 쪽: Python 3.11 이상, 표준 라이브러리만.
- 시드에서 시작해 고정 시뮬레이션 스텝으로 헤드리스 실행이 가능하고, 자기 시계를 멈출 수 있는 게임. 전체 계약은 설계 문서 3장에 있다.

## 돌려 보기

테스트에는 네트워크도 설치도 필요 없다.

```bash
python -m unittest discover -s tests -v
```

`mock` 디사이더로 가짜 게임에 브리지를 붙여 돌린 뒤, 그 실행을 테이프로 재생한다.

```bash
PYTHONPATH=src python -m shakedown bridge --decider mock --run-dir runs/a -- python tests/fake_game.py --seed 3
PYTHONPATH=src python -m shakedown bridge --decider tape --tape runs/a/decision_tape.jsonl --run-dir runs/b -- python tests/fake_game.py --seed 3
```

실행 디렉터리마다 `decision_tape.jsonl`, `bridge_summary.json`, `stdout.log`가 남는다.

## 라이선스

아직 정하지 않았다([열린 질문](docs/ko/09-roadmap.md#94-열린-질문) 참고).
