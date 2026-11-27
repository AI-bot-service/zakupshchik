"""Сборка плагина под адрес стенда: адрес в config.js и host_permissions, токены и шрифты — из стенда.

Отпечаток сборки (BUILD) — sha256 файлов пакета. Плагин сверяет его со стендом и сам перезаписывает
свою папку, когда стенд отдаёт новую сборку.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from functools import lru_cache
from pathlib import Path

EXT_DIR = Path(__file__).resolve().parents[2] / "extension"
STATIC_DIR = Path(__file__).resolve().parent / "static"
GENERATED = {"manifest.json", "config.js"}


def _sources(base_url: str) -> dict[str, bytes]:
    manifest = json.loads((EXT_DIR / "manifest.json").read_text(encoding="utf-8"))
    manifest["host_permissions"] = [f"{base_url}/*"]
    files = {"manifest.json": json.dumps(manifest, ensure_ascii=False, indent=2).encode()}
    for path in sorted(EXT_DIR.rglob("*")):
        rel = path.relative_to(EXT_DIR).as_posix()
        if path.is_file() and rel not in GENERATED:
            files[rel] = path.read_bytes()
    files["tokens.css"] = (STATIC_DIR / "tokens.css").read_bytes()
    for font in sorted((STATIC_DIR / "fonts").glob("*.woff2")):
        files[f"fonts/{font.name}"] = font.read_bytes()
    return files


@lru_cache(maxsize=4)
def package(base_url: str) -> tuple[str, dict[str, bytes]]:
    """(отпечаток сборки, файлы плагина). Файлы меняются только с выкаткой образа — кэш на процесс."""
    files = _sources(base_url)
    digest = hashlib.sha256()
    for rel in sorted(files):
        digest.update(rel.encode() + b"\0" + hashlib.sha256(files[rel]).digest())
    build = digest.hexdigest()[:16]
    config = f"export const BASE = {json.dumps(base_url)};\nexport const BUILD = {json.dumps(build)};\n"
    files["config.js"] = config.encode()
    return build, files


def build_zip(base_url: str) -> bytes:
    _, files = package(base_url)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, data in sorted(files.items()):
            z.writestr(rel, data)
    return buf.getvalue()
