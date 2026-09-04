#!/usr/bin/env python3
"""
논문 수집기 — LLM을 전혀 쓰지 않는 순수 파이썬 단계.

HuggingFace Daily Papers + arXiv(신착/타깃) + DBLP(과거 학회 발굴) 에서
후보를 모아 중복 제거하고 관심 프로필로 점수를 매겨 data/YYYY-MM-DD.json 에 저장.
그 다음 Claude가 그 JSON을 읽고 상위 N편만 요약한다.

의존성 없음 (표준 라이브러리만). Python 3.8+.
"""
import argparse
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
UA = "paper-digest/1.0 (personal research digest)"

# 호스트별 최소 호출 간격(초). arXiv는 공식적으로 3초 이상을 요구한다.
RATE = {"export.arxiv.org": 3.2, "dblp.org": 2.0, "huggingface.co": 0.5,
        "rss.arxiv.org": 0.3}
_last_hit = {}
_rate_lock = threading.Lock()

# 소스 하나가 죽어도 전체가 멈추지 않도록 전역 예산을 둔다.
DEADLINE = [None]


def out_of_time():
    return DEADLINE[0] is not None and time.time() > DEADLINE[0]


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def http_get(url, tries=3):
    host = urllib.parse.urlparse(url).netloc
    delay = RATE.get(host, 1.0)
    for attempt in range(tries):
        if out_of_time():
            log("  시간 예산 초과, 건너뜀: %s" % host)
            return None
        with _rate_lock:
            wait = delay - (time.time() - _last_hit.get(host, 0))
            if wait > 0:
                time.sleep(wait)
            _last_hit[host] = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < tries - 1:
                back = 4 * (attempt + 1)
                log("  rate-limited %s (%s), %ss 대기" % (host, e.code, back))
                time.sleep(back)
                continue
            log("  HTTP %s: %s" % (e.code, url[:110]))
            return None
        except Exception as e:
            if attempt < tries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            log("  실패: %s (%s)" % (type(e).__name__, url[:90]))
            return None
    return None


def get_json(url, tries=4):
    raw = http_get(url, tries)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def clean(text):
    return re.sub(r"\s+", " ", (text or "")).strip()


ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5})")


def arxiv_id_of(*candidates):
    for c in candidates:
        if not c:
            continue
        m = ARXIV_ID_RE.search(str(c))
        if m:
            return m.group(1)
    return None


def norm_title(t):
    return re.sub(r"[^a-z0-9]+", "", (t or "").lower())[:90]


# ---------------------------------------------------------------- sources


def fetch_hf_daily(days_back=2):
    """HF Daily Papers. 오늘 것이 아직 안 올라왔을 수 있어 며칠 거슬러 본다."""
    out = []
    for d in range(days_back):
        day = (date.today() - timedelta(days=d)).isoformat()
        data = get_json("https://huggingface.co/api/daily_papers?date=%s" % day)
        if not isinstance(data, list):
            continue
        for item in data:
            p = item.get("paper") or {}
            title = clean(p.get("title") or item.get("title"))
            if not title:
                continue
            aid = arxiv_id_of(p.get("id"), item.get("paper", {}).get("id"))
            out.append({
                "title": title,
                "abstract": clean(p.get("summary") or item.get("summary")),
                "authors": [a.get("name", "") for a in (p.get("authors") or [])][:8],
                "arxiv_id": aid,
                "url": "https://arxiv.org/abs/%s" % aid if aid else item.get("url", ""),
                "hf_upvotes": p.get("upvotes") or 0,
                "hf_date": day,
                "source": "hf_daily",
                "venue": None,
                "year": None,
            })
        log("  HF %s: %d편" % (day, len(data)))
    return out


ATOM = "{http://www.w3.org/2005/Atom}"


def parse_arxiv_atom(xml_text, source):
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out = []
    for e in root.findall(ATOM + "entry"):
        title = clean((e.findtext(ATOM + "title") or ""))
        if not title:
            continue
        eid = e.findtext(ATOM + "id") or ""
        aid = arxiv_id_of(eid)
        pub = (e.findtext(ATOM + "published") or "")[:10]
        out.append({
            "title": title,
            "abstract": clean(e.findtext(ATOM + "summary") or ""),
            "authors": [clean(a.findtext(ATOM + "name")) for a in e.findall(ATOM + "author")][:8],
            "arxiv_id": aid,
            "url": "https://arxiv.org/abs/%s" % aid if aid else eid,
            "hf_upvotes": 0,
            "published": pub,
            "source": source,
            "venue": None,
            "year": pub[:4] if pub else None,
        })
    return out


RSS_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.S)
RSS_TAG_RE = {
    "title": re.compile(r"<title>(.*?)</title>", re.S),
    "link": re.compile(r"<link>(.*?)</link>", re.S),
    "desc": re.compile(r"<description>(.*?)</description>", re.S),
    "creator": re.compile(r"<dc:creator>(.*?)</dc:creator>", re.S),
}
RSS_ANNOUNCE_RE = re.compile(r"Announce Type:\s*(\S+)", re.I)
CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)


def _unwrap(text):
    m = CDATA_RE.search(text or "")
    if m:
        text = m.group(1)
    text = re.sub(r"<[^>]+>", " ", text or "")
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&#39;", "'"), ("&apos;", "'")):
        text = text.replace(a, b)
    return clean(text)


def fetch_arxiv_rss(cats):
    """arXiv 신착은 RSS로. export API보다 30배 빠르고 초록까지 딸려오며 429가 없다."""
    out = []
    for c in cats:
        raw = http_get("https://rss.arxiv.org/rss/%s" % c, tries=2)
        if not raw:
            log("  arXiv RSS %s: 실패" % c)
            continue
        n = 0
        for item in RSS_ITEM_RE.findall(raw):
            title = _unwrap(RSS_TAG_RE["title"].search(item).group(1)
                            if RSS_TAG_RE["title"].search(item) else "")
            if not title:
                continue
            dm = RSS_TAG_RE["desc"].search(item)
            desc = _unwrap(dm.group(1)) if dm else ""
            ann = RSS_ANNOUNCE_RE.search(desc)
            # replace/replace-cross = 기존 논문 갱신본. 신착만 본다.
            if ann and ann.group(1).lower().startswith("replace"):
                continue
            abstract = desc.split("Abstract:", 1)[1].strip() if "Abstract:" in desc else desc
            abstract = re.sub(r"^arXiv:\S+\s*", "", abstract).strip()
            lm = RSS_TAG_RE["link"].search(item)
            link = _unwrap(lm.group(1)) if lm else ""
            cm = RSS_TAG_RE["creator"].search(item)
            authors = [a.strip() for a in _unwrap(cm.group(1)).split(",")][:8] if cm else []
            aid = arxiv_id_of(link, desc)
            out.append({
                "title": title,
                "abstract": abstract,
                "authors": authors,
                "arxiv_id": aid,
                "url": link or ("https://arxiv.org/abs/%s" % aid if aid else ""),
                "hf_upvotes": 0,
                "source": "arxiv_new",
                "primary_cat": c,
                "venue": None,
                "year": str(date.today().year),
            })
            n += 1
        log("  arXiv RSS %s: %d편(신착)" % (c, n))
    return out


def fetch_arxiv_targeted(queries, per_query):
    """export API 기반. 429가 잦은 엔드포인트라 실패해도 그냥 넘어간다(RSS가 본진)."""
    out = []
    for q in queries:
        if out_of_time():
            break
        url = ("https://export.arxiv.org/api/query?search_query=%s"
               "&start=0&max_results=%d&sortBy=submittedDate&sortOrder=descending"
               % (urllib.parse.quote(q), per_query))
        xml_text = http_get(url, tries=2)
        got = parse_arxiv_atom(xml_text, "arxiv_targeted") if xml_text else []
        log("  arXiv targeted %s: %d편" % (q[:44], len(got)))
        out.extend(got)
    return out


def dblp_search(query, h=20, f=0):
    url = ("https://dblp.org/search/publ/api?q=%s&h=%d&f=%d&format=json"
           % (urllib.parse.quote(query), h, f))
    data = get_json(url)
    if not data:
        return 0, []
    hits = (data.get("result") or {}).get("hits") or {}
    total = int(hits.get("@total", 0))
    raw = hits.get("hit") or []
    if isinstance(raw, dict):
        raw = [raw]
    return total, raw


def fetch_dblp_dive(cfg, rng):
    """랜덤 (학회 x 키워드) 조합으로 과거 학회 논문 발굴. 초록은 arXiv에서 보강."""
    d = cfg["dblp_dive"]
    want = d["n_papers"]
    picks, tried = [], set()
    combos = [(v, k) for v in d["venues"] for k in d["keywords"]]
    rng.shuffle(combos)

    for venue, kw in combos:
        if len(picks) >= want or len(tried) >= 12:
            break
        tried.add((venue, kw))
        total, hits = dblp_search("stream:%s: %s" % (venue, kw), h=30, f=0)
        if total == 0 or not hits:
            continue
        # 결과가 많으면 랜덤 구간에서 다시 뽑는다 (DBLP offset 상한 10000).
        if total > 30:
            off = rng.randrange(0, min(total, 9970))
            t2, h2 = dblp_search("stream:%s: %s" % (venue, kw), h=30, f=off)
            if h2:
                hits = h2
        rng.shuffle(hits)
        for hit in hits:
            if len(picks) >= want:
                break
            info = hit.get("info") or {}
            title = clean(info.get("title", "")).rstrip(".")
            year = info.get("year")
            if not title or not year:
                continue
            try:
                if int(year) < d.get("min_year", 2018):
                    continue
            except (TypeError, ValueError):
                continue
            authors = info.get("authors") or {}
            alist = authors.get("author") or []
            if isinstance(alist, dict):
                alist = [alist]
            picks.append({
                "title": title,
                "abstract": "",
                "authors": [a.get("text", "") for a in alist][:8],
                "arxiv_id": None,
                "url": info.get("ee") or info.get("url") or "",
                "hf_upvotes": 0,
                "source": "dblp_dive",
                "venue": info.get("venue"),
                "year": str(year),
                "dive_query": "%s / %s" % (venue, kw),
            })
        log("  DBLP %s + '%s': total=%d, 누적 %d편" % (venue, kw, total, len(picks)))
    return picks


def enrich_abstract(paper):
    """DBLP 논문은 초록이 없다. arXiv 제목 검색으로 보강 시도."""
    if paper.get("abstract") or not paper.get("title"):
        return paper
    safe = re.sub(r'[^A-Za-z0-9 \-]', ' ', paper["title"])
    safe = re.sub(r"\s+", " ", safe).strip()
    if len(safe) < 12:
        return paper
    url = ("https://export.arxiv.org/api/query?search_query=%s&start=0&max_results=1"
           % urllib.parse.quote('ti:"%s"' % safe))
    xml_text = http_get(url, tries=2)
    got = parse_arxiv_atom(xml_text, "arxiv") if xml_text else []
    if got and norm_title(got[0]["title"])[:45] == norm_title(paper["title"])[:45]:
        paper["abstract"] = got[0]["abstract"]
        paper["arxiv_id"] = got[0]["arxiv_id"]
        if not paper.get("url"):
            paper["url"] = got[0]["url"]
        paper["arxiv_url"] = got[0]["url"]
    return paper


# ------------------------------------------------- 학회 oral / spotlight

CONF_LINK_RE = r'href="/virtual/%s/%s/(\d+)"[^>]*>(.*?)</a>'
CONF_ABS_RE = r'<div class="abstract-text" id="abstract-(\d+)"[^>]*>(.*?)</div>'


def fetch_conference_orals(cfg):
    """학회 공식 virtual 사이트에서 oral/spotlight 목록을 통째로 긁는다.

    OpenReview API가 봇 챌린지로 막혀 있어 발표등급(oral/spotlight)을 얻을
    경로가 여기뿐이다. 한 페이지에 제목과 초록이 같이 들어 있어서 학회당
    요청 한 번이면 끝난다.
    """
    spec = cfg.get("conference_orals") or {}
    pool = []
    for src in spec.get("sources", []):
        site, year = src["site"], src["year"]
        for seg in src.get("segments", ["oral"]):
            if out_of_time():
                return pool
            url = "https://%s/virtual/%s/events/%s" % (site, year, seg)
            html = http_get(url, tries=2)
            if not html:
                log("  %s %s %s: 실패" % (site, year, seg))
                continue
            titles = {}
            for pid, raw in re.findall(CONF_LINK_RE % (year, seg), html, re.S):
                t = _unwrap(raw)
                if t and pid not in titles:
                    titles[pid] = t
            abstracts = {}
            for pid, raw in re.findall(CONF_ABS_RE, html, re.S):
                abstracts[pid] = _unwrap(raw)
            venue = site.split(".")[0].upper()
            n = 0
            for pid, t in titles.items():
                pool.append({
                    "title": t,
                    "abstract": abstracts.get(pid, ""),
                    "authors": [],
                    "arxiv_id": None,
                    "url": "https://%s/virtual/%s/%s/%s" % (site, year, seg, pid),
                    "hf_upvotes": 0,
                    "source": "conference",
                    "venue": "%s %s" % (venue, year),
                    "presentation": seg,
                    "year": str(year),
                    "_is_conf": True,
                })
                n += 1
            with_abs = sum(1 for pid in titles if abstracts.get(pid))
            log("  %s %s %s: %d편 (초록 %d)" % (site, year, seg, n, with_abs))
    return pool


def pick_conference(pool, n, bias, rng, matchers):
    """관심도로 기울이되 완전 상위독점은 피한다 — 발굴의 재미를 남기려고."""
    if not pool:
        return []
    for p in pool:
        score_paper(p, matchers)
    scored = sorted(pool, key=lambda x: x["score"], reverse=True)
    n_top = int(round(n * bias))
    picks = scored[:n_top]
    rest = scored[n_top:]
    rng.shuffle(rest)
    picks += rest[:max(0, n - len(picks))]
    rng.shuffle(picks)
    return picks


# ---------------------------------------------------------------- scoring


CORE_GROUPS = ("tier1_core", "acronyms", "learned")


def build_matchers(cfg, learned):
    """(compiled_regex, weight, label, group) 목록."""
    out = []
    for group, spec in cfg["keywords"].items():
        if not isinstance(spec, dict) or "terms" not in spec:
            continue
        w = spec["weight"]
        cs = group == "acronyms"   # 약어는 대소문자를 지켜야 오탐이 준다
        for term in spec["terms"]:
            pat = re.escape(term).replace(r"\ ", r"[\s\-]+")
            rx = re.compile(r"(?<![A-Za-z])%s(?![A-Za-z])" % pat,
                            0 if cs else re.IGNORECASE)
            out.append((rx, w, term, group))
    lw = cfg.get("feedback", {}).get("read_boost_weight", 4)
    for term in learned:
        pat = re.escape(term).replace(r"\ ", r"[\s\-]+")
        out.append((re.compile(r"(?<![A-Za-z])%s(?![A-Za-z])" % pat, re.IGNORECASE),
                    lw, "learned:" + term, "learned"))
    return out


def score_paper(paper, matchers):
    """코어 게이팅 방식.

    단순 합산은 "language models"+"activations"+"interpretable" 같은 흔한 말이
    쌓여서 의료 응용 논문을 1등으로 올려버린다. 그래서 mech-interp 코어 용어가
    실제로 잡혔을 때만 점수를 열어주고, 맥락 점수는 코어 점수를 넘지 못하게 막는다.
    """
    title = paper.get("title", "")
    abstract = paper.get("abstract", "")
    core = 0.0
    ctx = 0.0
    hits, core_hits = [], []
    for rx, w, label, group in matchers:
        in_t = bool(rx.search(title))
        if not in_t and not rx.search(abstract):
            continue
        gained = w * 2 if in_t else w
        if group in CORE_GROUPS:
            core += gained
            if w > 0:
                core_hits.append(label)
        else:
            ctx += gained
        if w > 0:
            hits.append(label)

    if core <= 0:
        total = ctx * 0.25          # 코어 용어 0개 = 관심분야가 아닐 확률이 높다
    else:
        # 코어 용어를 여러 개 맞출수록 진짜 그 분야 논문일 확률이 높다.
        # 한 단어가 우연히 스친 논문과 확실히 갈라주는 부분.
        breadth = 1.0 + 0.35 * (len(set(core_hits)) - 1)
        total = core * breadth + min(ctx, core)

    up = paper.get("hf_upvotes") or 0
    if up:
        total += min(up, 60) * 0.15

    paper["score"] = round(total, 1)
    paper["core_score"] = round(core, 1)
    paper["matched"] = sorted(set(hits))[:12]
    paper["core_matched"] = sorted(set(core_hits))[:8]
    return paper


# ---------------------------------------------------------------- state


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (IOError, ValueError):
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def learned_terms(cfg):
    """읽은 논문에서 뽑아둔 키워드 (mark_read.py 가 갱신)."""
    read = load_json(os.path.join(ROOT, "state", "read.json"), {})
    terms = read.get("learned_terms") or []
    cap = cfg.get("feedback", {}).get("max_learned_terms", 40)
    return terms[:cap]


# ---------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser(description="논문 후보 수집 및 스코어링")
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--no-dblp", action="store_true", help="DBLP 발굴 건너뛰기")
    ap.add_argument("--quick", action="store_true", help="타깃검색/DBLP 생략 (빠른 테스트)")
    ap.add_argument("--targeted", action="store_true",
                    help="arXiv export API 타깃검색 추가. RSS로 이미 신착은 다 덮으므로 기본 off.")
    ap.add_argument("--budget", type=int, default=240,
                    help="전체 수집 시간 상한(초). 넘으면 모은 것까지만 쓰고 종료.")
    args = ap.parse_args()

    cfg = load_json(os.path.join(ROOT, "config.json"), None)
    if cfg is None:
        log("config.json 을 읽을 수 없습니다.")
        return 1

    rng = random.Random()
    DEADLINE[0] = time.time() + args.budget
    papers, dive = [], []

    # 소스마다 호스트가 다르므로 병렬로 돈다. 레이트리밋은 호스트별로 유지된다.
    # 이미 받아온 결과는 어떤 실패에도 버리지 않는다. 각 잡이 공유 버킷에 즉시 넣는다.
    bucket = []
    bucket_lock = threading.Lock()

    def deposit(items, tag):
        if not items:
            return
        with bucket_lock:
            bucket.extend(items)
        log("  [%s] %d편 확보" % (tag, len(items)))

    def job_hf():
        log("[HF] Daily Papers")
        deposit(fetch_hf_daily(days_back=2), "HF")

    def job_arxiv_rss():
        log("[arXiv] RSS 신착")
        deposit(fetch_arxiv_rss(cfg["arxiv"]["categories"]), "arXiv RSS")

    def job_arxiv_targeted():
        log("[arXiv] 타깃 검색 (best-effort, 429 잦음)")
        deposit(fetch_arxiv_targeted(cfg["arxiv"]["targeted_queries"],
                                     cfg["arxiv"]["targeted_per_query"]), "arXiv 타깃")

    def job_conf():
        log("[학회] oral/spotlight 목록")
        deposit(fetch_conference_orals(cfg), "학회 oral")

    def job_dblp():
        log("[DBLP] 학회 발굴")
        picks = fetch_dblp_dive(cfg, rng)
        for pk in picks:
            if out_of_time():
                break
            enrich_abstract(pk)
        for pk in picks:
            pk["_is_dive"] = True
        deposit(picks, "DBLP 발굴")

    jobs = [job_hf, job_arxiv_rss]
    if not args.quick:
        jobs.append(job_conf)
    if not args.quick and not args.no_dblp and cfg.get("dblp_dive", {}).get("n_papers", 0) > 0:
        jobs.append(job_dblp)
    if args.targeted:
        jobs.append(job_arxiv_targeted)

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = [(fn.__name__, pool.submit(fn)) for fn in jobs]
        for name, fut in futures:
            try:
                fut.result(timeout=max(15, args.budget))
            except Exception as e:
                log("  %s 중단: %s — 이미 확보한 결과는 유지" % (name, type(e).__name__))

    with bucket_lock:
        papers = list(bucket)
    dive = [x for x in papers if x.get("_is_dive")]

    log("[정리] 중복 제거 + 스코어링")
    seen_path = os.path.join(ROOT, "state", "seen.json")
    seen = load_json(seen_path, {})

    unique, keys_today = {}, []
    for p in papers:
        key = p.get("arxiv_id") or norm_title(p.get("title"))
        if not key:
            continue
        if key in unique:
            old = unique[key]
            # 어느 소스가 먼저 도착하든 결과가 같아야 한다. 스레드 순서에
            # 따라 HF 날짜/upvote가 사라져서 그날 다이제스트가 통째로 비는
            # 버그가 있었다.
            if (p.get("hf_upvotes") or 0) > (old.get("hf_upvotes") or 0):
                old["hf_upvotes"] = p["hf_upvotes"]
            if p.get("hf_date") and not old.get("hf_date"):
                old["hf_date"] = p["hf_date"]
            if p.get("_is_conf"):
                old["_is_conf"] = True
                for f in ("venue", "presentation", "url"):
                    if p.get(f):
                        old.setdefault("conf_" + f, p[f])
                old["venue"] = old.get("venue") or p.get("venue")
                old["presentation"] = old.get("presentation") or p.get("presentation")
            if p.get("_is_dive"):
                old["_is_dive"] = True
            if len(p.get("abstract") or "") > len(old.get("abstract") or ""):
                old["abstract"] = p["abstract"]
            srcs = set(str(old.get("source", "")).split("+")) | {p.get("source", "")}
            old["source"] = "+".join(sorted(x for x in srcs if x))
            continue
        p["key"] = key
        unique[key] = p
        keys_today.append(key)

    matchers = build_matchers(cfg, learned_terms(cfg))
    for p in unique.values():
        score_paper(p, matchers)

    out_cfg = cfg["output"]
    seen_before = set(seen)

    # ── 갈래 1. HF Daily Papers: 전날 것 전량 (중복 제거만, 점수 필터 없음)
    target_day = (date.today() - timedelta(days=1)).isoformat()
    if out_cfg.get("hf_daily_target_day") == "today":
        target_day = date.today().isoformat()
    hf_all = [p for p in unique.values()
              if p.get("hf_date") == target_day and p["key"] not in seen_before]
    if not hf_all:   # 전날 것이 아직 없으면 있는 날짜 중 가장 최근으로
        hf_days = sorted({p["hf_date"] for p in unique.values() if p.get("hf_date")}, reverse=True)
        if hf_days:
            target_day = hf_days[0]
            hf_all = [p for p in unique.values()
                      if p.get("hf_date") == target_day and p["key"] not in seen_before]
    hf_all.sort(key=lambda x: (x.get("hf_upvotes") or 0), reverse=True)
    hf_keys = {p["key"] for p in hf_all}

    # ── 갈래 2. 학회 oral/spotlight
    conf_pool = [p for p in unique.values() if p.get("_is_conf") and p["key"] not in seen_before]
    conf_picks = pick_conference(conf_pool, out_cfg["conference_n"],
                                 cfg["conference_orals"].get("interest_bias", 0.6),
                                 rng, matchers)
    conf_keys = {p["key"] for p in conf_picks}

    # ── 갈래 3. 관심분야 (신착 arXiv 등) — 위 두 갈래와 겹치지 않게
    thr = out_cfg["min_score_to_include"]
    interest_pool = [p for p in unique.values()
                     if p["key"] not in seen_before
                     and p["key"] not in hf_keys and p["key"] not in conf_keys
                     and not p.get("_is_conf")
                     and p["score"] >= thr]
    interest_pool.sort(key=lambda x: x["score"], reverse=True)
    # 정원을 억지로 채우면 우연히 한 단어 스친 논문이 끼어든다.
    # 그날 기준을 넘는 게 4편뿐이면 4편만 낸다.
    strong = out_cfg.get("interest_strong_score", 18)
    interest_picks = [x for x in interest_pool[:out_cfg["interest_n"]]
                      if x["score"] >= strong]

    dive_fresh = [p for p in unique.values()
                  if p.get("_is_dive") and p["key"] not in seen_before]

    # ── 갈래 4. 오늘의 정전 한 편 (state/classics.json 캐시에서, 네트워크 미사용)
    classic = None
    try:
        import subprocess
        r = subprocess.run([sys.executable, os.path.join(ROOT, "classics.py"),
                            "--date", args.date],
                           capture_output=True, timeout=60)
        if r.returncode == 0 and r.stdout.strip():
            classic = json.loads(r.stdout.decode())
            log("  오늘의 정전: [%d회 인용] %s"
                % (classic["citations"], classic["title"][:56]))
    except Exception as e:
        log("  정전 선정 건너뜀 (%s) — classics.py --refresh 가 필요할 수 있음"
            % type(e).__name__)

    total_to_summarize = (len(hf_all) + len(conf_picks) + len(interest_picks)
                          + (1 if classic else 0))

    payload = {
        "date": args.date,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "stats": {
            "collected_raw": len(papers),
            "unique": len(unique),
            "already_seen": sum(1 for k in unique if k in seen_before),
            "hf_target_day": target_day,
            "conference_pool": len(conf_pool),
            "interest_pool": len(interest_pool),
            "total_to_summarize": total_to_summarize,
        },
        "classic": classic,
        "hf_daily": hf_all,
        "conference": conf_picks,
        "interest": interest_picks,
        "conference_dive": dive_fresh,
        "also_ran": [
            {"title": p["title"], "score": p["score"], "url": p.get("url", ""),
             "matched": p.get("matched", [])}
            for p in interest_pool[out_cfg["interest_n"]:
                                   out_cfg["interest_n"] + out_cfg["listing_extra"]]
        ],
    }

    out_path = os.path.join(ROOT, "data", "%s.json" % args.date)
    save_json(out_path, payload)

    for key in keys_today:
        seen.setdefault(key, args.date)
    # seen 기록은 180일치만 유지
    cutoff = (date.today() - timedelta(days=180)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    save_json(seen_path, seen)

    st = payload["stats"]
    log("")
    log("수집 %d → 고유 %d (기존 %d편 스킵)"
        % (st["collected_raw"], st["unique"], st["already_seen"]))
    log("요약 대상 %d편 = 정전 %d + HF %s 전량 %d + 학회 oral %d(풀 %d) + 관심분야 %d(풀 %d)"
        % (st["total_to_summarize"], 1 if payload.get("classic") else 0,
           st["hf_target_day"], len(payload["hf_daily"]),
           len(payload["conference"]), st["conference_pool"],
           len(payload["interest"]), st["interest_pool"]))
    log("→ %s" % out_path)
    print(out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
