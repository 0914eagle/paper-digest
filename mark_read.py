#!/usr/bin/env python3
"""읽은 논문을 표시하면 그 키워드가 다음날 스코어링에 반영된다.

  python3 mark_read.py "논문 제목 일부"     # 오늘 다이제스트에서 찾아 표시
  python3 mark_read.py --list              # 학습된 키워드 보기
  python3 mark_read.py --forget <용어>      # 잘못 학습된 용어 제거
"""
import json
import os
import re
import sys
from datetime import date

ROOT = os.path.dirname(os.path.abspath(__file__))
READ = os.path.join(ROOT, "state", "read.json")


def load(p, d):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (IOError, ValueError):
        return d


def save(p, o):
    with open(p, "w", encoding="utf-8") as f:
        json.dump(o, f, ensure_ascii=False, indent=2)


def main():
    args = sys.argv[1:]
    st = load(READ, {"papers": [], "learned_terms": []})

    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0

    if args[0] == "--list":
        print("읽은 논문 %d편" % len(st["papers"]))
        print("학습된 키워드:", ", ".join(st["learned_terms"]) or "(없음)")
        return 0

    if args[0] == "--forget":
        if len(args) < 2:
            print("제거할 용어를 지정하세요.")
            return 1
        t = args[1].lower()
        st["learned_terms"] = [x for x in st["learned_terms"] if x.lower() != t]
        save(READ, st)
        print("제거:", args[1])
        return 0

    needle = " ".join(args).lower()
    cfg = load(os.path.join(ROOT, "config.json"), {})
    known = set()
    for spec in (cfg.get("keywords") or {}).values():
        if isinstance(spec, dict):
            known.update(t.lower() for t in spec.get("terms", []))

    # 최근 다이제스트 데이터에서 제목으로 찾는다
    files = sorted(os.listdir(os.path.join(ROOT, "data")), reverse=True)[:14]
    found = None
    for fn in files:
        if not fn.endswith(".json"):
            continue
        d = load(os.path.join(ROOT, "data", fn), {})
        for bucket in ("hf_daily", "conference", "interest", "conference_dive"):
            for p in d.get(bucket, []):
                if needle in p.get("title", "").lower():
                    found = p
                    break
            if found:
                break
        if found:
            break

    if not found:
        print("최근 14일 데이터에서 '%s' 를 못 찾았습니다." % needle)
        return 1

    st["papers"].append({"title": found["title"], "date": date.today().isoformat(),
                         "score": found.get("score")})

    # 코어 용어만 학습한다. matched 전체를 넣으면 "language models" 같은
    # 일반 용어가 코어 가중치를 받아 스코어링 게이팅이 통째로 무력화된다.
    added = []
    for t in found.get("core_matched", []):
        tl = t.replace("learned:", "").lower()
        if tl in known and tl not in [x.lower() for x in st["learned_terms"]]:
            st["learned_terms"].append(tl)
            added.append(tl)

    cap = (cfg.get("feedback") or {}).get("max_learned_terms", 40)
    st["learned_terms"] = st["learned_terms"][-cap:]
    save(READ, st)
    print("표시: %s" % found["title"][:70])
    print("가중치 추가: %s" % (", ".join(added) or "(새 용어 없음)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
