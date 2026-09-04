#!/usr/bin/env python3
"""digest/*.md → docs/*.html 정적 사이트 생성.

외부 의존성 없음. 생성된 docs/ 는 그대로 (GitHub Pages의 /docs 소스로 바로 쓰인다)
  - `python3 -m http.server` 로 로컬에서 보거나
  - GitHub Pages에 올려서 보거나
둘 다 수정 없이 동작한다.
"""
import html
import json
import os
import re
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
DIGEST = os.path.join(ROOT, "digest")
SITE = os.path.join(ROOT, "docs")

CSS = """
:root{--bg:#fbfaf8;--fg:#1c1b19;--dim:#6b6862;--line:#e3e0da;--card:#fff;
--accent:#8a5a2b;--accent-bg:#f5efe6;--code:#f2efea}
@media(prefers-color-scheme:dark){:root{--bg:#16150f;--fg:#e8e5df;--dim:#95918a;
--line:#2e2c26;--card:#1d1c16;--accent:#d8a25e;--accent-bg:#2a2318;--code:#24221c}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.7 -apple-system,BlinkMacSystemFont,"Pretendard","Apple SD Gothic Neo",sans-serif}
.wrap{max-width:820px;margin:0 auto;padding:32px 20px 80px}
header{border-bottom:1px solid var(--line);margin-bottom:28px;padding-bottom:18px}
h1{font-size:26px;margin:0 0 6px;letter-spacing:-.02em}
h2{font-size:19px;margin:44px 0 14px;padding-top:14px;border-top:1px solid var(--line);
letter-spacing:-.01em}
h3{font-size:16px;margin:30px 0 10px;line-height:1.45}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
blockquote{margin:0 0 18px;padding:12px 16px;background:var(--accent-bg);
border-radius:8px;color:var(--dim);font-size:14px}
blockquote p{margin:2px 0}
code{background:var(--code);padding:2px 6px;border-radius:4px;font-size:.86em;
font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
hr{border:0;border-top:1px solid var(--line);margin:34px 0}
ul,ol{padding-left:22px;margin:8px 0 16px}
ol{padding-left:20px}
li{margin:5px 0}
p{margin:10px 0}
.meta{color:var(--dim);font-size:13px;margin:-4px 0 14px}
.nav{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-bottom:8px;font-size:14px}
.back{color:var(--dim)}
.entry{display:block;padding:14px 16px;margin:8px 0;background:var(--card);
border:1px solid var(--line);border-radius:10px;color:inherit}
.entry:hover{border-color:var(--accent);text-decoration:none}
.entry .d{display:block;font-weight:600;font-size:15px}
.entry .s{display:block;color:var(--dim);font-size:13px;margin-top:4px}
#q{width:100%;padding:11px 14px;font-size:15px;border:1px solid var(--line);
border-radius:9px;background:var(--card);color:var(--fg);margin:6px 0 4px}
#q:focus{outline:none;border-color:var(--accent)}
#hits{margin-top:10px}
.hit{padding:9px 14px;border-bottom:1px solid var(--line);font-size:14px}
.hit .t{display:block}
.hit .w{display:block;color:var(--dim);font-size:12px;margin-top:2px}
.count{color:var(--dim);font-size:13px;margin:8px 0}
.toc{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:12px 18px;margin:18px 0;font-size:14px}
.toc ul{margin:6px 0;padding-left:20px}
"""

INLINE = [
    (re.compile(r"`([^`]+)`"), lambda m: "<code>%s</code>" % html.escape(m.group(1))),
    (re.compile(r"\[([^\]]+)\]\(([^)]+)\)"),
     lambda m: '<a href="%s" rel="noopener">%s</a>'
               % (html.escape(m.group(2), True), html.escape(m.group(1)))),
    (re.compile(r"\*\*([^*]+)\*\*"), lambda m: "<strong>%s</strong>" % html.escape(m.group(1))),
]


def inline(text):
    """인라인 마크다운 처리. 코드/링크 안의 내용이 두 번 이스케이프되지 않도록
    토큰으로 빼두고 나머지만 이스케이프한 뒤 되돌린다."""
    slots = []

    def stash(rendered):
        slots.append(rendered)
        return "\x00%d\x00" % (len(slots) - 1)

    for rx, fn in INLINE:
        text = rx.sub(lambda m: stash(fn(m)), text)
    text = html.escape(text)
    return re.sub(r"\x00(\d+)\x00", lambda m: slots[int(m.group(1))], text)


def norm(text):
    """검색·대조용 정규화. 하이픈과 공백 차이로 검색이 빗나가지 않게 한다."""
    return re.sub(r"[\s\-_/]+", " ", (text or "").lower()).strip()


def slug(text):
    return re.sub(r"[^a-z0-9가-힣]+", "-", text.lower()).strip("-")[:60]


def md_to_html(md):
    out, in_list, in_quote = [], None, False   # in_list: None | "ul" | "ol"

    def close_list():
        nonlocal in_list
        if in_list:
            out.append("</%s>" % in_list)
            in_list = None

    def close_quote():
        nonlocal in_quote
        if in_quote:
            out.append("</blockquote>")
            in_quote = False

    for raw in md.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            close_list()
            close_quote()
            continue
        if line.startswith("> "):
            close_list()
            if not in_quote:
                out.append("<blockquote>")
                in_quote = True
            out.append("<p>%s</p>" % inline(line[2:]))
            continue
        close_quote()
        if re.match(r"^---+$", line):
            close_list()
            out.append("<hr>")
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            close_list()
            lvl = len(m.group(1))
            txt = m.group(2)
            anchor = slug(txt)
            out.append('<h%d id="%s">%s</h%d>' % (lvl, html.escape(anchor, True),
                                                  inline(txt), lvl))
            continue
        if line.startswith("- "):
            if in_list != "ul":
                close_list()
                out.append("<ul>")
                in_list = "ul"
            out.append("<li>%s</li>" % inline(line[2:]))
            continue
        om = re.match(r"^\d+\.\s+(.*)$", line)
        if om:
            if in_list != "ol":
                close_list()
                out.append("<ol>")
                in_list = "ol"
            out.append("<li>%s</li>" % inline(om.group(1)))
            continue
        close_list()
        out.append("<p>%s</p>" % inline(line))
    close_list()
    close_quote()
    return "\n".join(out)


def page(title, body, back=True):
    nav = '<div class="nav"><a class="back" href="index.html">← 전체 목록</a></div>' if back else ""
    return ("<!doctype html><html lang=ko><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>%s</title><style>%s</style></head><body><div class=wrap>%s%s</div></body></html>"
            % (html.escape(title), CSS, nav, body))


def main():
    if not os.path.isdir(DIGEST):
        print("digest/ 가 없습니다.", file=sys.stderr)
        return 1
    os.makedirs(SITE, exist_ok=True)

    files = sorted((f for f in os.listdir(DIGEST) if f.endswith(".md")), reverse=True)
    if not files:
        print("digest/*.md 가 없습니다.", file=sys.stderr)
        return 1

    # data/*.json 의 매칭 키워드를 검색 인덱스에 함께 싣는다.
    # 제목 문자열만으로는 "logit lens" 같은 개념 검색이 안 걸린다.
    kw_by_title = {}
    data_dir = os.path.join(ROOT, "data")
    if os.path.isdir(data_dir):
        for dj in os.listdir(data_dir):
            if not dj.endswith(".json"):
                continue
            try:
                with open(os.path.join(data_dir, dj), encoding="utf-8") as f:
                    payload = json.load(f)
            except (IOError, ValueError):
                continue
            for bucket in ("hf_daily", "conference", "interest", "conference_dive"):
                for pp in payload.get(bucket) or []:
                    t = norm(pp.get("title", ""))
                    if t:
                        kw_by_title[t] = sorted(set(
                            (pp.get("core_matched") or []) + (pp.get("matched") or [])))[:14]

    entries, index = [], []
    for fn in files:
        date = fn[:-3]
        with open(os.path.join(DIGEST, fn), encoding="utf-8") as f:
            md = f.read()

        # 목차 + 검색 인덱스용으로 논문 제목(### N. 제목)을 뽑는다
        section = ""
        papers = []
        for line in md.split("\n"):
            hm = re.match(r"^##\s+(.*)$", line)
            if hm:
                section = hm.group(1).strip()
            pm = re.match(r"^###\s+(\d+\.\s+.*)$", line)
            if pm:
                heading = pm.group(1).strip()
                t = re.sub(r"^\d+\.\s+", "", heading)
                anchor = slug(heading)
                papers.append(t)
                kws = kw_by_title.get(norm(t), [])
                index.append({"d": date, "t": t, "s": section, "a": anchor,
                              "k": " ".join(kws)})

        first = ""
        fm = re.search(r"^\s*1\.\s+\*\*(.+?)\*\*", md, re.M)
        if fm:
            first = fm.group(1)

        body = md_to_html(md)
        with open(os.path.join(SITE, "%s.html" % date), "w", encoding="utf-8") as f:
            f.write(page("%s 논문 다이제스트" % date, body))
        entries.append({"date": date, "n": len(papers), "first": first})

    rows = "".join(
        '<a class=entry href="%s.html"><span class=d>%s</span>'
        '<span class=s>요약 %d편%s</span></a>'
        % (e["date"], e["date"], e["n"],
           " · " + html.escape(e["first"]) if e["first"] else "")
        for e in entries)

    total = sum(e["n"] for e in entries)
    idx_json = json.dumps(index, ensure_ascii=False, separators=(",", ":"))
    body = (
        "<header><h1>논문 다이제스트</h1>"
        "<div class=meta>%d일치 · 논문 %d편 · 갱신 %s</div></header>"
        '<input id=q type=search placeholder="논문 제목 검색 (예: sparse autoencoder)" '
        'autocomplete=off>'
        '<div id=hits></div><div id=list>%s</div>'
        "<script>const IDX=%s;"
        "const N=s=>(s||'').toLowerCase().replace(/[\\s\\-_/]+/g,' ').trim();"
        "IDX.forEach(x=>{x._t=N(x.t);x._k=N(x.k)});"
        "const q=document.getElementById('q'),h=document.getElementById('hits'),"
        "l=document.getElementById('list');"
        "q.addEventListener('input',()=>{const v=N(q.value);"
        "if(!v){h.innerHTML='';l.style.display='';return}l.style.display='none';"
        "const r=IDX.filter(x=>x._t.includes(v)||x._k.includes(v)).slice(0,60);"
        "h.innerHTML=r.length?'<div class=count>'+r.length+'건</div>'+r.map(x=>"
        "'<div class=hit><a class=t href=\"'+x.d+'.html#'+x.a+'\">'+x.t.replace(/</g,'&lt;')+"
        "'</a><span class=w>'+x.d+' · '+x.s.replace(/</g,'&lt;')+'</span></div>').join(''):"
        "'<div class=count>없음</div>'});</script>"
        % (len(entries), total, datetime.now().strftime("%Y-%m-%d %H:%M"), rows, idx_json))

    with open(os.path.join(SITE, "index.html"), "w", encoding="utf-8") as f:
        f.write(page("논문 다이제스트", body, back=False))

    print("docs/ 생성: %d일치, 논문 %d편, 검색 인덱스 %d건"
          % (len(entries), total, len(index)))
    print(os.path.join(SITE, "index.html"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
