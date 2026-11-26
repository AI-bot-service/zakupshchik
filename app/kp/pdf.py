"""КП в PDF: LibreOffice headless рядом с DOCX. Нет LibreOffice (локально, тесты) → None, остаётся DOCX."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import structlog

log = structlog.get_logger()
TIMEOUT_S = 90


def to_pdf(docx: Path) -> Path | None:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if soffice is None:
        return None
    with tempfile.TemporaryDirectory(prefix="lo-") as profile:
        cmd = [
            soffice,
            f"-env:UserInstallation=file://{profile}",
            "--headless",
            "--norestore",
            "--convert-to",
            "pdf",
            "--outdir",
            str(docx.parent),
            str(docx),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=TIMEOUT_S)  # noqa: S603 — свой путь, без shell
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            log.warning("kp_pdf_failed", file=docx.name, error=type(exc).__name__)
            return None
    pdf = docx.with_suffix(".pdf")
    return pdf if pdf.exists() else None
