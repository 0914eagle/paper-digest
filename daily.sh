#!/bin/bash
# 요약이 끝난 뒤 실행. 사이트를 다시 만들고 그날 결과를 커밋한다.
# 커밋 실패가 다이제스트 자체를 망치지 않도록 어떤 경우에도 0으로 끝낸다.
set -u
cd "$(dirname "$0")" || exit 0

python3 build_site.py || echo "사이트 생성 실패 — 마크다운은 그대로 남아 있음" >&2

TODAY=$(date +%F)
git add -A 2>/dev/null
if git diff --cached --quiet 2>/dev/null; then
  echo "변경 없음, 커밋 생략"
  exit 0
fi

N=$(grep -c '^### ' "digest/$TODAY.md" 2>/dev/null || echo 0)
git commit -q -m "다이제스트 $TODAY — 요약 ${N}편" 2>/dev/null \
  && echo "커밋: $(git rev-parse --short HEAD)" \
  || echo "커밋 실패 (무시)" >&2

# 원격이 설정돼 있을 때만 푸시한다. 없으면 조용히 넘어간다.
if git remote get-url origin >/dev/null 2>&1; then
  git push -q origin HEAD 2>/dev/null && echo "푸시 완료" || echo "푸시 실패 (로컬 커밋은 남음)" >&2
fi
exit 0
