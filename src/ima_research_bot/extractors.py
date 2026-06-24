import logging
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
