"""Извлечение записей закупок из ответа площадки: текст ответа → список словарей «как на площадке»."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

from app.adapters.config import ExtractKind, PlatformConfig

Record = dict[str, Any]

_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_RSC_PUSH = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')
_RSC_ROW = re.compile(r"^([0-9a-f]+):(.*)$")
_RSC_REF = re.compile(r"^\$L?([0-9a-f]+)$")
_MAX_DEPTH = 60


class ExtractError(ValueError):
    """Ответ площадки не похож на ожидаемый: вёрстка или API поменялись."""


def get_path(obj: Any, path: str) -> Any:
    """Значение по пути "a.b.0.c"; нет такого пути → None."""
    if not path:
        return obj
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if idx < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


def _items(data: Any, path: str) -> list[Record]:
    items = get_path(data, path)
    if not isinstance(items, list):
        raise ExtractError(f"по пути «{path}» нет списка закупок")
    return [i for i in items if isinstance(i, dict)]


def from_json(text: str, cfg: PlatformConfig) -> list[Record]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExtractError("ответ не JSON") from exc
    return _items(data, cfg.items_path)


def from_next_data(text: str, cfg: PlatformConfig) -> list[Record]:
    m = _NEXT_DATA.search(text)
    if not m:
        raise ExtractError("на странице нет __NEXT_DATA__")
    return _items(json.loads(m.group(1)), cfg.items_path)


# ── Next.js RSC: поток строк "id:json", элементы ["$", tag, key, props], ссылки "$L1f" на другие строки ──


def _rsc_rows(html: str) -> dict[str, Any]:
    stream = "".join(json.loads(chunk) for chunk in _RSC_PUSH.findall(html))
    rows: dict[str, Any] = {}
    for line in stream.split("\n"):
        m = _RSC_ROW.match(line)
        if not m or m.group(2)[:1] not in ("[", "{"):
            continue
        try:
            rows[m.group(1)] = json.loads(m.group(2))
        except json.JSONDecodeError:
            continue  # строки модулей и текстовые блоки нам не нужны
    return rows


def _deref(value: Any, rows: dict[str, Any], depth: int) -> Any:
    while isinstance(value, str) and depth < _MAX_DEPTH:
        m = _RSC_REF.match(value)
        if not m or m.group(1) not in rows:
            return value
        value = rows[m.group(1)]
        depth += 1
    return value


def _is_element(node: Any) -> bool:
    return isinstance(node, list) and len(node) == 4 and node[0] == "$" and isinstance(node[3], dict)


def _children(node: Any) -> list[Any]:
    if _is_element(node):
        ch = node[3].get("children")
        return ch if isinstance(ch, list) and not _is_element(ch) else [ch]
    return node if isinstance(node, list) else []


def _is_text(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    return isinstance(value, int | float) or (isinstance(value, str) and not value.startswith("$"))


def _text_of(node: Any, rows: dict[str, Any], depth: int) -> str:
    node = _deref(node, rows, depth)
    if _is_text(node):
        return str(node)
    if isinstance(node, str | bool) or node is None:
        return ""
    return "".join(_text_of(c, rows, depth + 1) for c in _children(node))


def _blocks(node: Any, rows: dict[str, Any], depth: int, out: list[str], props: list[Record]) -> None:
    """Текстовые блоки карточки по порядку: элемент с прямым текстом — один блок."""
    node = _deref(node, rows, depth)
    if depth > _MAX_DEPTH or node is None or isinstance(node, bool):
        return
    if _is_element(node):
        p = node[3]
        if str(node[1]).startswith("$"):  # клиентский компонент: его данные — в props
            props.append({k: v for k, v in p.items() if k != "children"})
            if isinstance(p.get("name"), str) and "url" not in p:
                out.append(p["name"])
        kids = _children(node)
        if any(_is_text(_deref(k, rows, depth)) for k in kids):
            text = _text_of(node, rows, depth).strip()
            if text:
                out.append(text)
            return
        for k in kids:
            _blocks(k, rows, depth + 1, out, props)
    elif isinstance(node, list):
        for k in node:
            _blocks(k, rows, depth + 1, out, props)


def _walk(node: Any, depth: int = 0) -> Iterator[list[Any]]:
    if depth > _MAX_DEPTH:
        return
    if _is_element(node):
        yield node
        for v in node[3].values():
            yield from _walk(v, depth + 1)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v, depth + 1)
    elif isinstance(node, dict):
        for v in node.values():
            yield from _walk(v, depth + 1)


def from_next_rsc(text: str, cfg: PlatformConfig) -> list[Record]:
    rows = _rsc_rows(text)
    if not rows:
        raise ExtractError("на странице нет потока Next.js RSC")
    records: dict[int, Record] = {}
    for row in rows.values():
        for el in _walk(row):
            p = el[3]
            if p.get("data-slot") != "card" or not isinstance(p.get("data-id"), int):
                continue
            blocks: list[str] = []
            props: list[Record] = []
            _blocks(p.get("children"), rows, 0, blocks, props)
            link = next((x for x in props if isinstance(x.get("url"), str) and "name" in x), None)
            if link is None:
                continue
            rec: Record = {**link, "lotId": p["data-id"], "blocks": blocks}
            for i, b in enumerate(blocks):
                if b in cfg.card_labels and i + 1 < len(blocks):
                    rec.setdefault(cfg.card_labels[b], blocks[i + 1])
                if b in cfg.card_suffix_labels and i > 0:
                    rec.setdefault(cfg.card_suffix_labels[b], blocks[i - 1])
            records.setdefault(p["data-id"], rec)
    return list(records.values())


EXTRACTORS = {
    ExtractKind.JSON: from_json,
    ExtractKind.NEXT_DATA: from_next_data,
    ExtractKind.NEXT_RSC: from_next_rsc,
}


def extract(text: str, cfg: PlatformConfig) -> list[Record]:
    return EXTRACTORS[cfg.kind](text, cfg)
