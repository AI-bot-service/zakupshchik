"""Сборка zip плагина под адрес стенда: адрес в config.js и host_permissions, токены и шрифты — из стенда."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

EXT_DIR = Path(__file__).resolve().parents[2] / "extension"
STATIC_DIR = Path(__file__).resolve().parent / "static"
GENERATED = {"manifest.json", "config.js"}


def build_zip(base_url: str) -> bytes:
    manifest = json.loads((EXT_DIR / "manifest.json").read_text(encoding="utf-8"))
    manifest["host_permissions"] = [f"{base_url}/*"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        z.writestr("config.js", f"export const BASE = {json.dumps(base_url)};\n")
        for path in sorted(EXT_DIR.rglob("*")):
            rel = path.relative_to(EXT_DIR).as_posix()
            if path.is_file() and rel not in GENERATED:
                z.write(path, rel)
        z.write(STATIC_DIR / "tokens.css", "tokens.css")
        for font in sorted((STATIC_DIR / "fonts").glob("*.woff2")):
            z.write(font, f"fonts/{font.name}")
    return buf.getvalue()
