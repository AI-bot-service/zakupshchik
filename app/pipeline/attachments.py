"""Вложения закупки (ТЗ, проект договора): скачать один раз, сохранить, отдать агенту текст без ПДн.

Файл и его текст: FILES_DIR/<площадка>/<номер закупки>/<NN>_<имя с площадки> (+ .txt), где NN —
порядок файла в карточке закупки. В deal_file — тот же путь и sha256 (целостность, без дублей).
Повторный запрос того же файла идёт из кэша, площадку второй раз не трогаем.
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx
from docx import Document
from openpyxl import load_workbook
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.config import PlatformConfig
from app.adapters.fetch import Fetcher, FileTooLargeError
from app.adapters.normalize import resolve
from app.core.pii import mask_text
from app.db.models import Deal, DealFile

FILES_DIR = Path(__file__).resolve().parents[2] / "data" / "files"
MAX_FILE_BYTES = 15_000_000
MAX_TEXT_CHARS = 30_000
_MIME = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".pdf": "application/pdf",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".txt": "text/plain",
    ".rtf": "application/rtf",
}
# Текст Word 97–2003 хранится в UTF-16LE: латиница/цифры (xx 00) и кириллица (xx 04) идут подряд
_DOC_RUNS = re.compile(rb"(?:[\x20-\x7e\x09\x0d]\x00|[\x00-\xff]\x04){6,}")
_RTF_CONTROL = re.compile(r"\\[a-z]+-?\d* ?|[{}]")
_UNSAFE = re.compile(r'[\x00-\x1f\\/:*?"<>|]+')


class AttachmentError(ValueError):
    """Файл не открыть: причина словами для агента и журнала."""


@dataclass(frozen=True)
class AttachmentText:
    file_id: str
    name: str
    text: str
    truncated: bool
    cached: bool


def _docx(data: bytes) -> str:
    doc = Document(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = list(dict.fromkeys(c.text.strip() for c in row.cells if c.text.strip()))
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _pdf(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages[:60])


def _xlsx(data: bytes) -> str:
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    rows: list[str] = []
    for ws in wb.worksheets[:5]:
        for row in ws.iter_rows(max_row=500, values_only=True):
            cells = [str(v).strip() for v in row if v is not None and str(v).strip()]
            if cells:
                rows.append(" | ".join(cells))
    wb.close()
    return "\n".join(rows)


def _doc(data: bytes) -> str:
    runs = (r.decode("utf-16le", errors="ignore") for r in _DOC_RUNS.findall(data))
    return "\n".join(r.strip() for r in runs if sum(ch.isalpha() for ch in r) >= 4)


def _rtf(data: bytes) -> str:
    text = data.decode("cp1251", errors="ignore")
    text = re.sub(
        r"\\'([0-9a-f]{2})", lambda m: bytes([int(m.group(1), 16)]).decode("cp1251", "ignore"), text
    )
    return _RTF_CONTROL.sub("", text)


def extract_text(name: str, data: bytes) -> str:
    ext = Path(name).suffix.lower()
    try:
        if ext == ".docx" or (ext == "" and data[:2] == b"PK"):
            text = _docx(data)
        elif ext == ".pdf" or data[:4] == b"%PDF":
            text = _pdf(data)
        elif ext == ".xlsx":
            text = _xlsx(data)
        elif ext == ".doc":
            text = _doc(data)
        elif ext == ".rtf" or data[:5] == b"{\\rtf":
            text = _rtf(data)
        elif ext == ".txt":
            text = data.decode("utf-8", errors="ignore")
        else:
            raise AttachmentError(f"формат «{ext or 'без расширения'}» не читаем — открыть на площадке")
    except (zipfile.BadZipFile, PdfReadError, KeyError, ValueError) as exc:
        if isinstance(exc, AttachmentError):
            raise
        raise AttachmentError(f"файл повреждён или защищён ({type(exc).__name__})") from exc
    text = "\n".join(line for line in (" ".join(x.split()) for x in text.splitlines()) if line)
    if len(text) < 20:
        raise AttachmentError("в файле нет текстового слоя (скан) — открыть на площадке")
    return mask_text(text)


def _file_meta(deal: Deal, file_id: str) -> tuple[int, dict[str, str]]:
    """Номер файла в карточке (с 1) и его id/имя."""
    files = deal.raw_masked.get("details", {}).get("files", [])
    for n, f in enumerate(files if isinstance(files, list) else [], 1):
        if isinstance(f, dict) and str(f.get("id")) == file_id:
            return n, {"id": file_id, "name": str(f.get("name", file_id))}
    raise AttachmentError(f"у закупки нет файла с id {file_id}")


def _safe(name: str, limit: int = 120) -> str:
    name = _UNSAFE.sub("_", name).strip(" .") or "file"
    stem, ext = Path(name).stem, Path(name).suffix[:10]
    return stem[: limit - len(ext)] + ext


def deal_dir(root: Path, platform: str, external_id: str) -> Path:
    """Папка закупки: всё по одной сделке (документы площадки, их текст, наше КП) лежит вместе."""
    return root / _safe(platform, 40) / _safe(external_id, 80)


def file_path(root: Path, platform: str, external_id: str, n: int, name: str) -> Path:
    return deal_dir(root, platform, external_id) / f"{n:02d}_{_safe(name)}"


def get_attachment_text(
    session: Session, fetcher: Fetcher, cfg: PlatformConfig, deal: Deal, file_id: str, root: Path = FILES_DIR
) -> AttachmentText:
    n, meta = _file_meta(deal, file_id)
    spec = cfg.details
    if spec is None or spec.file_url is None:
        raise AttachmentError("площадка не отдаёт файлы без входа — открыть на площадке")
    url = resolve(meta, spec.file_url)
    if not isinstance(url, str):
        raise AttachmentError("не из чего собрать адрес файла")
    cached = session.scalar(select(DealFile).where(DealFile.deal_id == deal.id, DealFile.source_url == url))
    if cached is not None and Path(cached.path + ".txt").exists():
        text = Path(cached.path + ".txt").read_text(encoding="utf-8")
        return AttachmentText(file_id, meta["name"], text[:MAX_TEXT_CHARS], len(text) > MAX_TEXT_CHARS, True)
    try:
        data = fetcher.get_bytes(cfg, url, MAX_FILE_BYTES)
    except FileTooLargeError as exc:
        raise AttachmentError(str(exc)) from exc
    except httpx.HTTPError as exc:
        raise AttachmentError(f"площадка не отдала файл ({type(exc).__name__})") from exc
    text = extract_text(meta["name"], data)
    sha = hashlib.sha256(data).hexdigest()
    ext = Path(meta["name"]).suffix.lower()
    path = file_path(root, cfg.code, deal.external_id, n, meta["name"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    Path(str(path) + ".txt").write_text(text, encoding="utf-8")
    same = cached or session.scalar(
        select(DealFile).where(DealFile.deal_id == deal.id, DealFile.sha256 == sha)
    )
    if same is None:
        session.add(
            DealFile(
                deal_id=deal.id,
                name=meta["name"],
                mime=_MIME.get(ext, "application/octet-stream"),
                size=len(data),
                sha256=sha,
                source_url=url,
                path=str(path),
            )
        )
    return AttachmentText(file_id, meta["name"], text[:MAX_TEXT_CHARS], len(text) > MAX_TEXT_CHARS, False)
