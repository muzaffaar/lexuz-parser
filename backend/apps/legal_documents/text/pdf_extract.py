"""Embedded-text extraction from a PDF. Django-free on purpose: it runs in a SUBPROCESS.

PDFs come from the Internet, and parsers hang or explode on hostile files. The caller (`pdf.py`) runs this
module with a timeout so a bad PDF can cost one item, never the ingestion job:

    python -m apps.legal_documents.text.pdf_extract <file> [max_pages]   ->  JSON on stdout (UTF-8)
"""
import json
import sys


def extract_pages(path, max_pages: int = 2000) -> dict:
    from pypdf import PdfReader

    reader = PdfReader(path, strict=False)
    if reader.is_encrypted:
        return {"status": "encrypted", "total_pages": 0, "pages": [], "errors": []}
    total = len(reader.pages)
    pages, errors = [], []
    for i, page in enumerate(reader.pages):
        if i >= max_pages:
            break
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001 - one broken page must not lose the others
            errors.append({"page": i + 1, "error": str(exc)[:200]})
            text = ""
        pages.append({"page": i + 1, "text": text})
    status = "page_limit" if total > max_pages else ("partial_errors" if errors else "ok")
    return {"status": status, "total_pages": total, "pages": pages, "errors": errors}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    path = argv[0]
    max_pages = int(argv[1]) if len(argv) > 1 else 2000
    try:
        result = extract_pages(path, max_pages)
    except Exception as exc:  # noqa: BLE001
        result = {"status": "extraction_failed", "total_pages": 0, "pages": [], "errors": [{"error": f"{type(exc).__name__}: {exc}"[:300]}]}
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
