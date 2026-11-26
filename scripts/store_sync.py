"""Выгрузка карточек ПО ключевых вендоров с store.softline.ru в JSONL.

Источник — публичная карта сайта (https-sitemap.xml). Карточки robots.txt не запрещает.
Выгрузка продолжается с места обрыва: уже скачанные URL берутся из cards.jsonl.
Импорт в Postgres (таблица catalog_item) — отдельным шагом после миграции.

Запуск: python3 scripts/store_sync.py [--vendors kaspersky,drweb] [--workers 2] [--delay 1.0]
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from itertools import zip_longest
from pathlib import Path

BASE = "https://store.softline.ru"
SITEMAP_PARTS = [f"{BASE}/sitemap/softwareProducts-{i}.xml" for i in range(1, 9)]
UA = "zakupshchik-catalog-sync/0.1 (hackathon prototype; 1-2 req/s)"
KEY_VENDORS = [
    "kaspersky", "drweb", "pro32", "astralinux", "kripto-pro", "cyberprotect",
    "redsoft", "yandex", "positive-technologies", "basealt", "contentai",
]
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "store"


def fetch(url: str, retries: int = 4) -> str | None:
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ru"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            wait = 60 if e.code in (429, 503) else 5 * (attempt + 1)
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            wait = 5 * (attempt + 1)
        time.sleep(wait)
    raise RuntimeError(f"не удалось скачать {url}")


def text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def parse_card(url: str, page: str) -> dict[str, object]:
    def first(pattern: str) -> str | None:
        m = re.search(pattern, page, re.S)
        return text(m.group(1)) if m else None

    specs = {
        text(k).rstrip(":"): text(v)
        for k, v in re.findall(
            r'b-product-specs__label">(.*?)</span>\s*<span class="b-product-specs__value">(.*?)</span>', page, re.S
        )
    }
    short = [text(s) for s in re.findall(r'b-product-hardware__short-spec">(.*?)</li>', page, re.S)]
    price_raw = first(r'js-price-with-discount">([^<]*)<')
    price = int(re.sub(r"\D", "", price_raw)) if price_raw and re.search(r"\d", price_raw) else None
    min_qty = max_qty = None
    for s in short:
        m = re.search(r"Минимальная покупка:\s*от\s*(\d+)(?:\s*до\s*(\d+))?", s)
        if m:
            min_qty = int(m.group(1))
            max_qty = int(m.group(2)) if m.group(2) else None
    delivery = specs.get("Особенности доставки") or ""
    dm = re.search(r"Срок доставки:\s*(.+?)\.?\s*$", delivery)
    pid = re.search(r"-(\d+)/?$", url)
    return {
        "url": url,
        "product_id": int(pid.group(1)) if pid else None,
        "vendor_slug": url.split("/")[3],
        "sku": specs.get("Артикул") or first(r"Артикул:</span>\s*<[^>]*>([^<]+)<"),
        "name": first(r"<h1[^>]*>(.*?)</h1>"),
        "manufacturer": first(r'Производитель:\s*<a[^>]*>(.*?)</a>'),
        "price_rub": price,
        "price_on_request": price is None and "js-add-to-request" in page,
        "min_qty": min_qty,
        "max_qty": max_qty,
        "license_type": specs.get("Тип лицензии"),
        "term": specs.get("Срок действия"),
        "version": specs.get("Версия"),
        "client_type": specs.get("Конечный пользователь") or specs.get("Тип организации"),
        "rf_registry_no": specs.get("Номер записи в реестре отечественного ПО"),
        "delivery_days": dm.group(1).strip() if dm else None,
        "categories": [text(c) for c in re.findall(r'b-breadcrumbs__item-link">\s*<span itemprop="name">(.*?)</span>', page, re.S)],
        "short_specs": short,
        "specs": specs,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def collect_urls(vendors: list[str]) -> list[str]:
    by_vendor: dict[str, list[str]] = {v: [] for v in vendors}
    for part in SITEMAP_PARTS:
        xml = fetch(part) or ""
        for loc in re.findall(r"<loc>([^<]+)</loc>", xml):
            slug = loc.split("/")[3] if loc.count("/") > 3 else ""
            if slug in by_vendor:
                by_vendor[slug].append(loc.strip())
        time.sleep(1)
    for v, urls in by_vendor.items():
        print(f"  {v}: {len(urls)}", flush=True)
    # чередуем вендоров, чтобы при обрыве у каждого была часть каталога
    return [u for group in zip_longest(*by_vendor.values()) for u in group if u]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vendors", default=",".join(KEY_VENDORS))
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--delay", type=float, default=1.0, help="пауза между запросами одного потока, с")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cards_path, errors_path = OUT_DIR / "cards.jsonl", OUT_DIR / "errors.jsonl"
    urls_path = OUT_DIR / "urls.txt"

    if urls_path.exists():
        urls = urls_path.read_text().split()
    else:
        print("карта сайта → URL карточек", flush=True)
        urls = collect_urls(args.vendors.split(","))
        urls_path.write_text("\n".join(urls))
    done: set[str] = set()
    if cards_path.exists():
        for line in cards_path.open():
            done.add(json.loads(line)["url"])
    todo = [u for u in urls if u not in done]
    print(f"всего {len(urls)}, уже есть {len(done)}, осталось {len(todo)}", flush=True)

    lock = threading.Lock()
    counter = {"ok": 0, "err": 0, "no_price": 0}
    started = time.time()

    def work(url: str) -> None:
        try:
            page = fetch(url)
            if page is None:
                raise RuntimeError("404")
            card = parse_card(url, page)
            line, path = json.dumps(card, ensure_ascii=False), cards_path
            key = "ok" if card["sku"] and card["name"] else "err"
            if card["price_rub"] is None:
                counter["no_price"] += 1
        except Exception as e:  # noqa: BLE001 — любая ошибка карточки пишется в errors.jsonl, выгрузка идёт дальше
            line, path, key = json.dumps({"url": url, "error": str(e)}, ensure_ascii=False), errors_path, "err"
        with lock:
            with path.open("a") as f:
                f.write(line + "\n")
            counter[key] += 1
            n = counter["ok"] + counter["err"]
            if n % 200 == 0:
                rate = n / (time.time() - started)
                left = (len(todo) - n) / rate / 3600 if rate else 0
                print(f"{n}/{len(todo)} ok={counter['ok']} err={counter['err']} без цены={counter['no_price']} "
                      f"{rate:.2f}/с, осталось ~{left:.1f} ч", flush=True)
        time.sleep(args.delay)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(work, todo))
    print(f"готово: ok={counter['ok']} err={counter['err']} без цены={counter['no_price']}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
