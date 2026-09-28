"""Safe reading of PDFs out of a downloaded zip (lex.uz serves some acts as `/files/<n>.zip` holding the PDF).

The zip is untrusted input. Nothing is ever extracted to disk under an archive-supplied name (no zip-slip),
members are read into memory with a hard size cap (no zip bombs), and only entries whose bytes really start
with `%PDF-` are accepted, whatever their name says.
"""
import zipfile
from dataclasses import dataclass
from pathlib import Path

MAX_MEMBERS = 200
MAX_PDF_BYTES = 128 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024


@dataclass
class ZipPdf:
    name: str  # display only; never used as a filesystem path
    data: bytes


def extract_pdfs(zip_path, *, max_pdf_bytes: int = MAX_PDF_BYTES, max_total_bytes: int = MAX_TOTAL_BYTES) -> list[ZipPdf]:
    found: list[ZipPdf] = []
    total = 0
    try:
        zf = zipfile.ZipFile(Path(zip_path))
    except zipfile.BadZipFile:
        return []
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_MEMBERS:
            return []  # not a plausible legal-document bundle
        for info in infos:
            if info.is_dir() or not info.filename.lower().endswith(".pdf"):
                continue
            if info.file_size > max_pdf_bytes or total + info.file_size > max_total_bytes:
                continue  # declared size already too big
            with zf.open(info) as fh:
                data = fh.read(max_pdf_bytes + 1)  # the declared size can lie: enforce the cap while reading
            if len(data) > max_pdf_bytes or not data.startswith(b"%PDF-"):
                continue
            total += len(data)
            found.append(ZipPdf(name=info.filename.replace("\\", "/").rsplit("/", 1)[-1], data=data))
    return found
