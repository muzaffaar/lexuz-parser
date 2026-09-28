"""Bounded PDF text extraction (subprocess + timeout). See pdf_extract.py for why."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from django.conf import settings

DEFAULT_TIMEOUT = 90  # seconds, same bound the crawler uses


def extract_pdf_text(data: bytes, *, timeout: int = DEFAULT_TIMEOUT, max_pages: int = 2000) -> dict:
    """-> {'status', 'total_pages', 'pages': [{'page', 'text'}], 'errors'}. Never raises.

    status is one of: ok, partial_errors, page_limit, encrypted, extraction_failed, extraction_timeout.
    Password-protected PDFs are reported, never unlocked."""
    fd, name = tempfile.mkstemp(suffix=".pdf", prefix="yurist-pdf-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "apps.legal_documents.text.pdf_extract", name, str(max_pages)],
                capture_output=True, timeout=timeout, cwd=str(settings.BASE_DIR),
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
        except subprocess.TimeoutExpired:
            return {"status": "extraction_timeout", "total_pages": 0, "pages": [], "errors": [{"error": f"exceeded {timeout}s"}]}
        try:
            return json.loads(proc.stdout.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            err = proc.stderr.decode("utf-8", "replace")[-300:]
            return {"status": "extraction_failed", "total_pages": 0, "pages": [], "errors": [{"error": err or "no output"}]}
    finally:
        Path(name).unlink(missing_ok=True)
