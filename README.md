# 논문 다이제스트

매일 아침 논문을 수집해 요약본을 쌓는 개인용 자동화. mechanistic
interpretability와 모델 내부를 관찰하는 연구에 가중치를 둔다.

읽기: **[GitHub Pages 사이트](https://heejae.github.io/paper-digest/)** ·
로컬은 `python3 -m http.server 8731 --directory docs`

## 무엇을 모으나

| 갈래 | 소스 | 분량 |
|---|---|---|
| HuggingFace Daily Papers | `huggingface.co/api/daily_papers` | 전날치 전량 |
| 주요 학회 oral/spotlight | 학회 virtual 사이트 (ICLR·ICML·NeurIPS) | 10편 |
| 관심분야 신착 | arXiv RSS (cs.LG·CL·AI·CV) | 기준 통과분 |

하루 약 1300편을 모아 중복을 걸러 1100여 편으로 줄이고, 그 중
50편 안팎을 요약한다.

## 구조

```
collect.py     수집·중복제거·스코어링 (LLM 미사용, 약 20초)
  → data/YYYY-MM-DD.json
Claude         요약 (구독제로 실행)
  → digest/YYYY-MM-DD.md
build_site.py  정적 사이트 생성 (의존성 없음)
  → docs/
daily.sh       사이트 재생성 + 커밋 + 푸시
mark_read.py   읽은 논문 표시 → 다음날 가중치에 반영
config.json    관심 프로필. 대부분의 조정은 여기서 한다.
```

수집과 요약을 나눈 이유는 비용이다. 1300편을 전부 LLM에 넣을 수는
없으므로 파이썬으로 먼저 추려낸다. 요약이 Claude Code 안에서 도는
것도 의도된 제약이다 — 클라우드나 서버리스로 옮기면 API 키가 필요해
구독제로 돌릴 수 없다.

## 스코어링

단순 키워드 합산은 쓰지 않는다. "language models" 같은 흔한 말이
쌓이면 무관한 논문이 상위로 올라오기 때문이다. 대신 코어 게이팅을
쓴다.

- mech interp 코어 용어(sparse autoencoder, activation patching,
  logit lens 등)를 맞춰야 점수가 열린다
- 맥락 점수는 코어 점수를 넘지 못한다
- 코어 용어를 여러 개 맞출수록 가중된다 — 한 단어가 우연히 스친
  논문과 갈라내기 위해서다

관심 프로필을 바꾸려면 `config.json`의 `keywords`를 고치면 된다.

## 외부 API 메모

2026-09-04 기준 실측이다. 시간이 지나면 달라질 수 있다.

- **OpenReview는 쓰지 않는다.** v1, v2, 공식 `openreview-py`
  클라이언트 모두 봇 챌린지(403)에 막힌다. 발표 등급(oral/spotlight)은
  학회 virtual 사이트에서 가져오는데, 한 페이지에 제목과 초록이 다
  들어 있어 학회당 요청 한 번이면 되므로 결과적으로 더 낫다.
- **arXiv는 `rss.arxiv.org`를 쓴다.** `export.arxiv.org` API는 429가
  잦아 한 번 걸리면 재시도로 몇 분을 잡아먹는다. RSS는 별도 호스트라
  제한이 없고 0.1초에 초록 포함 수백 편이 온다. export API는 기본
  비활성이며 `--targeted`로만 켜진다.
- **DBLP**는 offset 상한이 10000이고 503이 잦다. 현재는 학회
  스크레이퍼가 이 역할을 대신해 기본 비활성이다.

수집은 소스별로 병렬로 돌고 전역 시간 예산이 걸려 있다. 한 소스가
죽어도 이미 받아둔 결과는 버리지 않는다.
