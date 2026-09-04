#!/usr/bin/env python3
"""explainability / mechanistic interpretability 정전(定番) 풀 관리.

  python3 classics.py --refresh     풀 재구축 (Semantic Scholar 조회, 몇 분 소요)
  python3 classics.py               오늘의 한 편 출력 (API 호출 없음)
  python3 classics.py --stats       풀 현황
  python3 classics.py --reset-served 이미 내보낸 기록 초기화

풀 구축은 주 1회면 충분하다. S2는 무인증 rate limit이 빡빡해서 매일 때리면
429에 걸린다. 매일 실행은 캐시에서 고르기만 하므로 네트워크를 쓰지 않는다.
"""
import argparse
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

ROOT = os.path.dirname(os.path.abspath(__file__))
POOL = os.path.join(ROOT, "state", "classics.json")
UA = {"User-Agent": "paper-digest/1.0 (mailto:0914eagle@gmail.com)"}

# 풀을 채울 검색어. explainability와 mech interp 양쪽을 훑는다.
QUERIES = [
    'abs:"mechanistic interpretability"',
    'abs:"sparse autoencoder" AND abs:"language model"',
    'abs:"sparse autoencoders" AND abs:"features"',
    'abs:"activation patching" OR abs:"causal tracing"',
    'abs:"circuit" AND abs:"transformer" AND abs:"interpretability"',
    'abs:"probing" AND abs:"representations" AND abs:"language model"',
    'abs:"feature attribution" OR abs:"saliency map"',
    'abs:"concept activation vector" OR abs:"concept bottleneck"',
    'abs:"model editing" AND abs:"factual"',
    'abs:"logit lens" OR abs:"tuned lens"',
    'abs:"steering vector" OR abs:"activation steering"',
    'abs:"superposition" AND abs:"neural network"',
    'abs:"polysemantic" OR abs:"monosemantic"',
    'abs:"attention" AND abs:"explanation" AND abs:"faithful"',
    'abs:"knowledge neurons" OR abs:"factual associations"',
    'abs:"linear representation" AND abs:"language model"',
    'abs:"induction head" OR abs:"in-context learning" AND abs:"mechanism"',
    'abs:"grokking" OR abs:"emergent abilities"',
    'abs:"dictionary learning" AND abs:"interpretab"',
    'abs:"chain-of-thought" AND abs:"faithfulness"',
    'abs:"explainable" AND abs:"deep learning" AND abs:"survey"',
    'abs:"vision-language" AND abs:"interpretability"',
]

ARXIV = "https://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"

S2_BATCH = "https://api.semanticscholar.org/graph/v1/paper/batch"


def log(m):
    print(m, file=sys.stderr, flush=True)


def s2_get(url, tries=3):
    """S2 무인증 풀은 대기가 길다. 한 질의에 매달리지 말고 빨리 넘어간다 —
    질의가 20개라 몇 개 놓쳐도 풀은 충분히 채워진다."""
    for a in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            return json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and a < tries - 1:
                wait = 5 * (a + 1)
                log("   %s, %d초 대기" % (e.code, wait))
                time.sleep(wait)
                continue
            return {"_err": "HTTP %s" % e.code}
        except Exception as e:
            if a < tries - 1:
                time.sleep(5 * (a + 1))
                continue
            return {"_err": type(e).__name__}
    return {"_err": "exhausted"}


def load(p, d):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (IOError, ValueError):
        return d


def save(p, o):
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(o, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def norm_title(t):
    return re.sub(r"[^a-z0-9]+", "", (t or "").lower())[:90]


def relevance(paper, matchers, score_fn):
    """collect.py의 코어 게이팅 스코어러를 그대로 쓴다.
    S2 검색은 주제를 벗어난 것도 물어오므로 한 번 더 거른다."""
    p = {"title": paper.get("title") or "", "abstract": paper.get("abstract") or "",
         "hf_upvotes": 0}
    score_fn(p, matchers)
    return p["score"], p["core_matched"]


def arxiv_search(query, n):
    """arXiv에서 후보를 모은다. 초록까지 여기서 다 온다."""
    import xml.etree.ElementTree as ET
    url = ("%s?search_query=%s&start=0&max_results=%d&sortBy=relevance"
           % (ARXIV, urllib.parse.quote(query), n))
    for a in range(3):
        try:
            req = urllib.request.Request(url, headers=UA)
            raw = urllib.request.urlopen(req, timeout=40).read().decode("utf-8", "replace")
            break
        except Exception as e:
            if a == 2:
                return [], str(e)[:50]
            time.sleep(6 * (a + 1))
    try:
        root = ET.fromstring(raw)
    except Exception:
        return [], "parse"
    out = []
    for e in root.findall(ATOM + "entry"):
        eid = e.findtext(ATOM + "id") or ""
        m = re.search(r"abs/([\d.]+?)(?:v\d+)?$", eid)
        if not m:
            continue
        title = " ".join((e.findtext(ATOM + "title") or "").split())
        abstract = " ".join((e.findtext(ATOM + "summary") or "").split())
        if not title or not abstract:
            continue
        pub = (e.findtext(ATOM + "published") or "")[:4]
        out.append({
            "arxiv_id": m.group(1),
            "title": title,
            "abstract": abstract,
            "year": int(pub) if pub.isdigit() else None,
            "authors": [" ".join((a.findtext(ATOM + "name") or "").split())
                        for a in e.findall(ATOM + "author")][:6],
            "url": "https://arxiv.org/abs/%s" % m.group(1),
        })
    return out, None


def s2_citations(arxiv_ids):
    """S2 배치로 인용수만 받아온다. search 엔드포인트와 달리 rate limit이 없다시피
    하고 한 번에 500개까지 된다."""
    cites = {}
    hdr = dict(UA)
    hdr["Content-Type"] = "application/json"
    for i in range(0, len(arxiv_ids), 400):
        chunk = arxiv_ids[i:i + 400]
        body = json.dumps({"ids": ["arXiv:%s" % x for x in chunk]}).encode()
        url = "%s?fields=title,citationCount,year,venue" % S2_BATCH
        for a in range(4):
            try:
                req = urllib.request.Request(url, data=body, headers=hdr)
                r = json.loads(urllib.request.urlopen(req, timeout=60).read().decode())
                for aid, rec in zip(chunk, r):
                    if rec:
                        cites[aid] = {"citations": rec.get("citationCount") or 0,
                                      "venue": rec.get("venue") or ""}
                break
            except urllib.error.HTTPError as e:
                if e.code in (429, 503) and a < 3:
                    time.sleep(6 * (a + 1))
                    continue
                log("   배치 실패 HTTP %s" % e.code)
                break
            except Exception as e:
                if a < 3:
                    time.sleep(5 * (a + 1))
                    continue
                log("   배치 실패 %s" % type(e).__name__)
                break
        log("   인용수 조회 %d/%d" % (min(i + 400, len(arxiv_ids)), len(arxiv_ids)))
        time.sleep(1.5)
    return cites


def refresh(args):
    import importlib.util
    spec = importlib.util.spec_from_file_location("c", os.path.join(ROOT, "collect.py"))
    c = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(c)
    cfg = load(os.path.join(ROOT, "config.json"), {})
    matchers = c.build_matchers(cfg, c.learned_terms(cfg))

    old = load(POOL, {})
    merged = {p["title"]: p for p in old.get("papers", [])}
    served = old.get("served", [])
    n_start = len(merged)

    # 1단계: arXiv에서 후보 수집 (초록 포함)
    cands = {}
    for i, q in enumerate(QUERIES, 1):
        got, err = arxiv_search(q, args.per_query)
        if err:
            log("[%2d/%d] %-46s 실패(%s)" % (i, len(QUERIES), q[:46], err))
        else:
            for p in got:
                cands.setdefault(p["arxiv_id"], p)
            log("[%2d/%d] %-46s +%d (후보 %d)"
                % (i, len(QUERIES), q[:46], len(got), len(cands)))
        time.sleep(args.delay)

    if not cands:
        log("후보를 못 모았습니다. 기존 풀 유지.")
        return 1

    # 2단계: 관련도로 먼저 거른다 — 인용수 조회 대상을 줄이려고
    #  "superposition"은 양자물리 용어이기도 해서 양자컴퓨팅 논문이 딸려 온다.
    off_topic = re.compile(r"\b(quantum|qubit|photonic|spintronic|entanglement)\b", re.I)
    scored = []
    for p in cands.values():
        if off_topic.search(p["title"] + " " + p["abstract"][:400]):
            continue
        q = {"title": p["title"], "abstract": p["abstract"], "hf_upvotes": 0}
        c.score_paper(q, matchers)
        if q["score"] >= args.min_relevance:
            p["relevance"] = q["score"]
            p["core_matched"] = q["core_matched"]
            scored.append(p)
    log("")
    log("후보 %d편 → 관련도 %d 이상 %d편" % (len(cands), args.min_relevance, len(scored)))

    # 3단계: S2 배치로 인용수
    cites = s2_citations([p["arxiv_id"] for p in scored])
    log("인용수 확보 %d/%d편" % (len(cites), len(scored)))

    kept = 0
    for p in scored:
        info = cites.get(p["arxiv_id"])
        if not info or info["citations"] < args.min_citations:
            continue
        p["citations"] = info["citations"]
        p["venue"] = info["venue"]
        merged[p["title"]] = p
        kept += 1

    papers = sorted(merged.values(), key=lambda x: -x["citations"])
    save(POOL, {"built": date.today().isoformat(), "papers": papers, "served": served,
                "min_citations": args.min_citations, "cycle": old.get("cycle", 1)})
    log("")
    log("풀 %d편 (이번에 +%d) · 인용 %d 이상 · 이미 내보냄 %d편"
        % (len(papers), len(papers) - n_start, args.min_citations, len(served)))
    return 0


def pick(args):
    pool = load(POOL, None)
    if not pool or not pool.get("papers"):
        log("풀이 비어 있습니다. 먼저 --refresh 를 실행하세요.")
        return 1
    served = set(pool.get("served", []))
    avail = [p for p in pool["papers"] if norm_title(p["title"]) not in served]
    recycled = False
    if not avail:
        # 다 돌았으면 처음부터 다시
        avail = pool["papers"]
        served = set()
        recycled = True

    # 인용수와 관련도 양쪽을 보되 상위 독점은 피한다.
    # 날짜로 시드를 고정해 같은 날 여러 번 돌려도 같은 논문이 나오게 한다.
    rng = random.Random(args.date)
    import math
    ranked = sorted(avail, key=lambda p: -(p["relevance"] * math.log10(p["citations"] + 10)))
    top = ranked[:max(12, len(ranked) // 8)]
    chosen = rng.choice(top)

    if not args.dry_run:
        served.add(norm_title(chosen["title"]))
        pool["served"] = sorted(served)
        if recycled:
            pool["cycle"] = pool.get("cycle", 1) + 1
        save(POOL, pool)

    print(json.dumps(chosen, ensure_ascii=False, indent=2))
    log("선정: [%d회 인용] %s" % (chosen["citations"], chosen["title"][:64]))
    log("남은 풀: %d편" % (len(avail) - 1))
    return 0


def stats(_args):
    pool = load(POOL, None)
    if not pool:
        log("풀 없음. --refresh 필요.")
        return 1
    ps = pool["papers"]
    served = len(pool.get("served", []))
    log("구축일 %s · 총 %d편 · 내보냄 %d · 남음 %d · %d회차"
        % (pool.get("built"), len(ps), served, len(ps) - served, pool.get("cycle", 1)))
    log("인용 분포: 최고 %d / 중앙 %d / 최저 %d"
        % (ps[0]["citations"], ps[len(ps) // 2]["citations"], ps[-1]["citations"]))
    log("")
    log("상위 8편:")
    for p in ps[:8]:
        log("  [%6d] %s (%s)" % (p["citations"], p["title"][:60], p.get("year")))
    return 0


def main():
    ap = argparse.ArgumentParser(description="정전 논문 풀 관리")
    ap.add_argument("--refresh", action="store_true", help="Semantic Scholar에서 풀 재구축")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--reset-served", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="고르되 내보냄 기록은 남기지 않음")
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--min-citations", type=int, default=50)
    ap.add_argument("--min-relevance", type=int, default=20)
    ap.add_argument("--delay", type=float, default=3.3)
    ap.add_argument("--per-query", type=int, default=100)
    args = ap.parse_args()

    if args.reset_served:
        pool = load(POOL, None)
        if pool:
            pool["served"] = []
            save(POOL, pool)
            log("내보냄 기록 초기화")
        return 0
    if args.refresh:
        return refresh(args)
    if args.stats:
        return stats(args)
    return pick(args)


if __name__ == "__main__":
    sys.exit(main())
