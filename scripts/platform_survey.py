"""Разведка площадок: открыт ли список закупок без входа, есть ли капча, SPA, разделы малых закупок.

Ходит через российский SOCKS (curl), по одному запросу в 2 с. Результат — JSONL для таблицы в ploshchadki.md.
Запуск: python3 scripts/platform_survey.py urls.txt out.jsonl [--proxy socks5h://127.0.0.1:1080]
Формат urls.txt: «код<TAB>URL» в строке.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
CAPTCHA = ("smartcaptcha", "g-recaptcha", "hcaptcha", "ddos-guard", "qrator", "cf-chl", "servicepipe", "captcha")
LOGIN = ("войдите", "авторизуйтесь", "необходимо авторизоваться", "вход в личный кабинет", "войти через госуслуги")
SMALL = re.compile(
    r"мал(ые|ых|ая)\s+закуп|магазин|маркет|market|торгов(ый|ая)\s+(портал|площадк)|коммерческ|котировочн|"
    r"быстр(ые|ая)|упрощ|запрос\w*\s+(цен|кп|предложен)|tailspend|shop|витрин|потребност|прямые закупки",
    re.I,
)
PROC_WORDS = re.compile(r"закупк|процедур|лот|нмц|начальная.{0,20}цена|прием заявок|приём заявок|окончани", re.I)


def curl(url: str, proxy: str) -> tuple[int, str, str]:
    marker = "\n@@META@@"
    cmd = ["curl", "-g", "-s", "-L", "-m", "25", "-A", UA, "-H", "Accept-Language: ru", "--compressed",
           "--socks5-hostname", proxy.removeprefix("socks5h://"), "-w", marker + "%{http_code} %{url_effective}", url]
    out = subprocess.run(cmd, capture_output=True, timeout=40).stdout.decode("utf-8", errors="replace")
    body, _, meta = out.rpartition(marker)
    code, _, final = meta.partition(" ")
    return int(code or 0), final, body


def analyze(code: str, url: str, proxy: str) -> dict[str, object]:
    status, final, page = curl(url, proxy)
    low = page.lower()
    visible = re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", page, flags=re.S | re.I)
    visible = re.sub(r"\s+", " ", html.unescape(visible))
    host = urlparse(final or url).netloc
    links: dict[str, str] = {}
    for href, label in re.findall(r'<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>', page, re.S | re.I):
        text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", label))).strip()
        if SMALL.search(text) or SMALL.search(href):
            full = urljoin(final or url, href)
            if full.startswith("http") and len(links) < 25:
                links.setdefault(full, text[:80])
    return {
        "code": code,
        "url": url,
        "http": status,
        "final_url": final,
        "host": host,
        "bytes": len(page),
        "visible_words": len(visible.split()),
        "captcha": [m for m in CAPTCHA if m in low],
        "login_wall": [m for m in LOGIN if m in low],
        "spa": bool(re.search(r'<div id="(root|app|__next|__nuxt)"|ng-version|<app-root', page)) and len(visible.split()) < 400,
        "procurement_words": len(PROC_WORDS.findall(visible)),
        "rub_amounts": len(re.findall(r"\d[\d\s]{3,}(?:[.,]\d{2})?\s*(?:₽|руб)", visible)),
        "small_purchase_links": links,
        "title": (re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I) or [None, ""])[1].strip()[:120],
        "snippet": visible[:300],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("urls")
    ap.add_argument("out")
    ap.add_argument("--proxy", default="socks5h://127.0.0.1:1080")
    ap.add_argument("--delay", type=float, default=2.0)
    args = ap.parse_args()
    out = Path(args.out)
    with out.open("a") as f:
        for line in Path(args.urls).read_text().splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            code, url = line.split("\t", 1)
            try:
                row = analyze(code, url.strip(), args.proxy)
            except Exception as e:  # noqa: BLE001 — сбой одной площадки не останавливает разведку
                row = {"code": code, "url": url, "error": str(e)}
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            print(code, row.get("http"), row.get("bytes"), "captcha" if row.get("captcha") else "", flush=True)
            time.sleep(args.delay)


if __name__ == "__main__":
    main()
