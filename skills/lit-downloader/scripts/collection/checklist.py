"""HTML checklist (and CSV) of the working-list records still without a PDF, for downloading by
hand in the user's own browser or handing to the library's document delivery.

Usage: python checklist.py <project> [--lang en|zh] [--exclude-prefix 10.1007,...]

Rows are grouped by publisher; each carries the best link known (publisher PDF, article page)
and what the automatic rounds saw. Records without a DOI get title-search links.
Writes missing_pdfs.html and missing_pdfs.csv in the project folder.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import sys
import urllib.parse
from collections import Counter

from collection_common import (
    console_utf8,
    crossref_message,
    doi_prefix,
    first,
    list_records,
    load_project,
    oa_urls,
    ok_states,
    read_open_log,
    rec_doi,
    rec_journal,
    rec_title,
)
from open_links import url_for

PUBLISHER = {
    "10.1016": "Elsevier",
    "10.1007": "Springer",
    "10.1080": "Taylor & Francis",
    "10.1063": "AIP",
    "10.1116": "AIP (AVS)",
    "10.1134": "Pleiades",
    "10.3103": "Allerton Press",
    "10.1177": "SAGE",
    "10.1179": "SAGE (Maney)",
    "10.1243": "SAGE (IMechE)",
    "10.1103": "APS",
    "10.1088": "IOP",
    "10.1166": "ASP",
    "10.1142": "World Scientific",
    "10.1515": "De Gruyter",
    "10.1155": "Hindawi",
    "10.3233": "IOS Press",
    "10.1002": "Wiley",
    "10.1111": "Wiley",
    "10.1038": "Nature",
    "10.1557": "Cambridge (MRS)",
    "10.1017": "Cambridge",
    "10.4028": "Trans Tech",
    "10.1021": "ACS",
    "10.1039": "RSC",
    "10.1093": "Oxford",
    "10.1107": "IUCr",
    "10.1051": "EDP Sciences",
    "10.1361": "ASM International",
    "10.3390": "MDPI",
    "10.2320": "J-STAGE (JIM)",
}
TEXT = {
    "en": {
        "title": "PDFs still to download",
        "pdf": "PDF",
        "page": "Article page",
        "nodoi": "No DOI",
        "tried": "opened {n}x, no PDF",
        "untried": "not tried automatically",
        "search": "no DOI, search by title",
        "scholar": "Google Scholar",
        "tip": "Open the first link, pass any check yourself, and "
        "save the PDF. If the link fails, use the article page. Ticks are only a reminder "
        "kept in this browser.",
        "head": ["#", "Publisher", "DOI", "Title", "Journal", "Year", "Status", "Link"],
    },
    "zh": {
        "title": "待下载 PDF",
        "pdf": "PDF",
        "page": "文章页",
        "nodoi": "无 DOI",
        "tried": "自动打开过 {n} 次没下到",
        "untried": "没有自动尝试过",
        "search": "没有 DOI，按标题搜索",  # noqa: RUF001
        "scholar": "谷歌学术",
        "tip": "点第一个链接，遇到验证就自己点一下，然后保存 PDF。打不开就点「文章页」。"  # noqa: RUF001
        "勾选框只是帮你记进度（保存在本浏览器里）。",  # noqa: RUF001
        "head": ["序号", "出版商", "DOI", "标题", "期刊", "年份", "情况", "链接"],
    },
}


def rows_for(project, lang: str, exclude: tuple[str, ...]) -> list[dict]:
    t = TEXT[lang]
    have = ok_states(project)
    tries, last = Counter(), {}
    for e in read_open_log(project):
        tries[e["doi"]] += 1
        last[e["doi"]] = (e.get("title") or "").removesuffix(" - Google Chrome")
    repo = oa_urls(project)
    rows = []
    for rec in list_records(project):
        doi, title = rec_doi(rec), rec_title(rec)
        if doi in have or doi.startswith("arxiv-") or (doi and doi.startswith(exclude)):
            continue
        if doi:
            pdf = url_for(doi, crossref_message(project, doi), repo.get(doi, []), [])
            links = [(t["pdf"], pdf)] if pdf else []
            links.append((t["page"], "https://doi.org/" + doi))
            n = tries[doi]
            note = (
                t["tried"].format(n=n) + (f" ({last[doi][:40]})" if last[doi] else "")
                if n
                else t["untried"]
            )
            pub = PUBLISHER.get(doi_prefix(doi), doi_prefix(doi))
        else:
            q = urllib.parse.quote(title)
            links = [(t["scholar"], f"https://scholar.google.com/scholar?q={q}")]
            if lang == "zh":
                links.append(("百度学术", f"https://xueshu.baidu.com/s?wd={q}"))
            note, pub = t["search"], t["nodoi"]
        rows.append(
            {
                "doi": doi,
                "title": title,
                "journal": rec_journal(rec),
                "year": first(rec, "PY"),
                "pub": pub,
                "note": note,
                "links": links,
            }
        )
    rows.sort(key=lambda r: (not r["doi"], r["pub"], r["doi"] or r["title"]))
    return rows


def render(rows: list[dict], lang: str, key: str) -> str:
    t = TEXT[lang]
    items = []
    for i, r in enumerate(rows, 1):
        a = " · ".join(
            f'<a{" class=go" if j == 0 else ""} href="{html.escape(u)}" '
            f'target="_blank">{html.escape(label)}</a>'
            for j, (label, u) in enumerate(r["links"])
        )
        meta = f"{html.escape(r['pub'])} · {html.escape(r['journal'])} {html.escape(r['year'])}"
        meta += f" · {html.escape(r['doi'])}" if r["doi"] else ""
        items.append(
            f'<tr data-k="{html.escape(r["doi"] or r["title"])}"><td>{i}</td>'
            f'<td><input type="checkbox"></td><td>{a}</td><td>{html.escape(r["title"])}'
            f'<div class="m">{meta}</div><div class="n">{html.escape(r["note"])}</div>'
            "</td></tr>"
        )
    return f"""<!doctype html><meta charset="utf-8"><title>{t["title"]} ({len(rows)})</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{{font:14px system-ui,"Microsoft YaHei",sans-serif;margin:24px auto;max-width:1100px;
padding:0 16px;background:#fff;color:#222}}
table{{border-collapse:collapse;width:100%}} td{{border-bottom:1px solid #ddd;padding:6px;
vertical-align:top}} .m{{color:#777;font-size:12px}} .n{{color:#a60;font-size:12px}}
a.go{{font-weight:600}} a:visited{{color:#999}} tr.done{{opacity:.45}}
.tip{{background:#f5f5f5;padding:10px 14px;border-radius:6px}}
</style>
<h2>{t["title"]}: {len(rows)}</h2><div class="tip">{t["tip"]}</div>
<table>{"".join(items)}</table>
<script>
const k={json.dumps(key)};let s=new Set();try{{s=new Set(JSON.parse(localStorage[k]||'[]'))}}catch(e){{}}
document.querySelectorAll('tr[data-k]').forEach(tr=>{{const cb=tr.querySelector('input'),d=tr.dataset.k;
const set=v=>{{cb.checked=v;tr.classList.toggle('done',v);v?s.add(d):s.delete(d);
try{{localStorage[k]=JSON.stringify([...s])}}catch(e){{}}}};
set(s.has(d));cb.onchange=()=>set(cb.checked);tr.querySelector('a.go').addEventListener('click',()=>set(true));}});
</script>"""


def main(argv=None) -> int:
    console_utf8()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("project")
    ap.add_argument("--lang", choices=sorted(TEXT), default="en")
    ap.add_argument("--exclude-prefix", default="")
    args = ap.parse_args(argv)
    project = load_project(args.project)
    exclude = tuple(p.rstrip("/") + "/" for p in args.exclude_prefix.split(",") if p)
    rows = rows_for(project, args.lang, exclude)
    (project.dir / "missing_pdfs.html").write_text(
        render(rows, args.lang, str(project.dir)), encoding="utf-8"
    )
    with (project.dir / "missing_pdfs.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(TEXT[args.lang]["head"])
        for i, r in enumerate(rows, 1):
            w.writerow(
                [
                    i,
                    r["pub"],
                    r["doi"],
                    r["title"],
                    r["journal"],
                    r["year"],
                    r["note"],
                    r["links"][0][1],
                ]
            )
    print(f"{len(rows)} records -> missing_pdfs.html, missing_pdfs.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
