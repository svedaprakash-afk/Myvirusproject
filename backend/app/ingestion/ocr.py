"""Local OCR for scanned remittance advices (images and image-only PDFs). Nothing leaves the process.

Needs the `tesseract` binary plus the optional Python packages in requirements-ocr.txt (pytesseract, Pillow, pypdfium2). When they are
missing `available()` is False and callers say so instead of silently returning empty text: a scan that cannot be read must be visible
to the user, not turn into a remittance with no lines.

Hardening: image pixel and byte limits (decompression bombs), page cap, per-page timeout. OCR output is untrusted text exactly like an
email body; the extractor treats it as evidence and every reference must still resolve against real open items.
"""
from __future__ import annotations

import io
import shutil

MAX_PIXELS = 40_000_000
MAX_PAGES = 5
PAGE_TIMEOUT_S = 40
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp")


class OcrUnavailable(RuntimeError):
    pass


class OcrError(ValueError):
    pass


def available() -> bool:
    if not shutil.which("tesseract"):
        return False
    try:
        import PIL  # noqa: F401
        import pytesseract  # noqa: F401
    except ImportError:
        return False
    return True


def _prep(img):
    """Grayscale, upscale small scans (tesseract wants ~300 dpi text height) and stretch contrast."""
    from PIL import ImageOps

    img = ImageOps.exif_transpose(img).convert("L")
    if max(img.size) < 1800:
        k = 2 if max(img.size) >= 900 else 3
        img = img.resize((img.width * k, img.height * k))
    return ImageOps.autocontrast(img)


def image_to_text(data: bytes, lang: str = "eng") -> tuple[str, float]:
    """Returns (text, mean word confidence 0-100)."""
    if not available():
        raise OcrUnavailable("OCR is not installed on this server (needs the tesseract binary and requirements-ocr.txt)")
    import pytesseract
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:  # corrupt, truncated, or a decompression bomb
        raise OcrError(f"Image could not be read: {e}") from e
    if img.width * img.height > MAX_PIXELS:
        raise OcrError("Image is too large to scan")
    img = _prep(img)
    cfg = "--psm 6 -c preserve_interword_spaces=1"
    try:
        text = pytesseract.image_to_string(img, lang=lang, config=cfg, timeout=PAGE_TIMEOUT_S)
        d = pytesseract.image_to_data(img, lang=lang, config=cfg, output_type=pytesseract.Output.DICT, timeout=PAGE_TIMEOUT_S)
    except RuntimeError as e:
        raise OcrError(f"OCR timed out or failed: {e}") from e
    confs = [float(c) for c, w in zip(d["conf"], d["text"]) if str(w).strip() and float(c) >= 0]
    return text, (sum(confs) / len(confs) if confs else 0.0)


def pdf_to_images(data: bytes, max_pages: int = MAX_PAGES, scale: float = 2.5) -> list[bytes]:
    try:
        import pypdfium2 as pdfium
    except ImportError as e:
        raise OcrUnavailable("Scanned PDFs need pypdfium2 (requirements-ocr.txt)") from e
    try:
        pdf = pdfium.PdfDocument(data)
    except Exception as e:
        raise OcrError(f"PDF could not be opened: {e}") from e
    out = []
    for i in range(min(len(pdf), max_pages)):
        buf = io.BytesIO()
        pdf[i].render(scale=scale).to_pil().save(buf, format="PNG")
        out.append(buf.getvalue())
    return out


def pdf_to_text(data: bytes, lang: str = "eng") -> tuple[str, float, int]:
    pages = pdf_to_images(data)
    texts, confs = [], []
    for png in pages:
        t, c = image_to_text(png, lang)
        texts.append(t)
        confs.append(c)
    return "\n".join(texts), (sum(confs) / len(confs) if confs else 0.0), len(pages)
