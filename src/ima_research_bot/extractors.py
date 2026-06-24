import logging
import re
import zipfile
from html import unescape
from xml.etree import ElementTree
from pathlib import Path

from pypdf import PdfReader

logger = logging.getLogger(__name__)


class TextExtractor:
    def extract(self, path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            return self._extract_pdf(path)
        if suffix in {".txt", ".md"}:
            return path.read_text(encoding="utf-8", errors="ignore")
        if suffix == ".docx":
            return self._extract_docx(path)
        if suffix == ".pptx":
            return self._extract_pptx(path)
        if suffix == ".xlsx":
            return self._extract_xlsx(path)
        if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
            return self._extract_image(path)
        return ""

    def _extract_pdf(self, path: Path) -> str:
        reader = PdfReader(str(path))
        chunks: list[str] = []
        for idx, page in enumerate(reader.pages, 1):
            try:
                text = page.extract_text() or ""
            except Exception as exc:
                logger.warning("failed to extract page %s from %s: %s", idx, path, exc)
                text = ""
            if text.strip():
                chunks.append(f"\n[Page {idx}]\n{text}")
        return "\n".join(chunks).strip()

    def _extract_image(self, path: Path) -> str:
        try:
            import pytesseract
            from PIL import Image
        except Exception:
            logger.warning("pytesseract/Pillow unavailable; skipping OCR for %s", path)
            return ""
        try:
            return pytesseract.image_to_string(Image.open(path), lang="chi_sim+eng+por")
        except Exception as exc:
            logger.warning("OCR failed for %s: %s", path, exc)
            return ""

    def _extract_docx(self, path: Path) -> str:
        try:
            with zipfile.ZipFile(path) as archive:
                chunks = [
                    _xml_text(archive.read(name))
                    for name in sorted(archive.namelist())
                    if name.startswith("word/") and name.endswith(".xml")
                ]
        except Exception as exc:
            logger.warning("DOCX extraction failed for %s: %s", path, exc)
            return ""
        return _clean_text("\n".join(chunk for chunk in chunks if chunk.strip()))

    def _extract_pptx(self, path: Path) -> str:
        try:
            with zipfile.ZipFile(path) as archive:
                chunks = []
                for name in sorted(archive.namelist()):
                    if not (name.startswith("ppt/slides/slide") and name.endswith(".xml")):
                        continue
                    text = _xml_text(archive.read(name))
                    if text.strip():
                        chunks.append(f"\n[Slide {len(chunks) + 1}]\n{text}")
        except Exception as exc:
            logger.warning("PPTX extraction failed for %s: %s", path, exc)
            return ""
        return _clean_text("\n".join(chunks))

    def _extract_xlsx(self, path: Path) -> str:
        try:
            with zipfile.ZipFile(path) as archive:
                shared_strings = _xlsx_shared_strings(archive)
                chunks = []
                for name in sorted(archive.namelist()):
                    if not (name.startswith("xl/worksheets/sheet") and name.endswith(".xml")):
                        continue
                    rows = _xlsx_sheet_rows(archive.read(name), shared_strings)
                    if rows:
                        chunks.append(f"\n[Sheet {len(chunks) + 1}]\n" + "\n".join(rows))
        except Exception as exc:
            logger.warning("XLSX extraction failed for %s: %s", path, exc)
            return ""
        return _clean_text("\n".join(chunks))


def _xml_text(raw_xml: bytes) -> str:
    try:
        root = ElementTree.fromstring(raw_xml)
    except ElementTree.ParseError:
        return ""
    return "\n".join(item.strip() for item in root.itertext() if item and item.strip())


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        raw = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        return []
    return [_clean_text(" ".join(node.itertext())) for node in root]


def _xlsx_sheet_rows(raw_xml: bytes, shared_strings: list[str]) -> list[str]:
    try:
        root = ElementTree.fromstring(raw_xml)
    except ElementTree.ParseError:
        return []
    rows: list[str] = []
    for row in root.iter():
        if _local_name(row.tag) != "row":
            continue
        cells = []
        for cell in row:
            if _local_name(cell.tag) != "c":
                continue
            cell_type = cell.attrib.get("t")
            value = ""
            for child in cell:
                if _local_name(child.tag) in {"v", "t"}:
                    value = child.text or ""
                    break
                if _local_name(child.tag) == "is":
                    value = " ".join(child.itertext())
                    break
            if cell_type == "s" and value.isdigit():
                idx = int(value)
                value = shared_strings[idx] if idx < len(shared_strings) else value
            if value.strip():
                cells.append(value.strip())
        if cells:
            rows.append(" | ".join(cells))
    return rows


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _clean_text(text: str) -> str:
    text = unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
