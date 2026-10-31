"""Free, local OCR for images and scanned PDF pages, with graceful fallback.

Engines, tried in order (``DEEP_RESEARCH_OCR=auto``, the default):

1. ``rapidocr_onnxruntime`` - pure pip install, bundled ONNX models, no system deps
   (``pip install "multimodal_agentic_deep_research_assistant[ocr]"``).
2. ``pytesseract`` - only when the ``tesseract`` binary is on PATH.

Nothing installed (or ``DEEP_RESEARCH_OCR=off``) -> :func:`ocr_image` returns
``None`` and callers keep their metadata / vision-model behaviour.
"""

from __future__ import annotations

import io
import os
import shutil
import threading
from dataclasses import dataclass
from typing import Any

MAX_OCR_PIXELS = 25_000_000
MIN_CONFIDENCE = 0.5
_lock = threading.Lock()
_engine: Any = None
_engine_name: str | None = None
_probed = False


@dataclass(frozen=True)
class OcrResult:
    text: str
    engine: str
    lines: int
    confidence: float | None


def _probe() -> tuple[Any, str | None]:
    global _engine, _engine_name, _probed
    with _lock:
        if _probed:
            return _engine, _engine_name
        _probed = True
        if os.environ.get("DEEP_RESEARCH_OCR", "auto").strip().lower() in ("off", "0", "false", "none"):
            return None, None
        try:
            from rapidocr_onnxruntime import RapidOCR

            _engine, _engine_name = RapidOCR(), "rapidocr"
            return _engine, _engine_name
        except Exception:  # not installed / model load failure -> try next engine
            _engine = None
        try:
            import pytesseract

            if shutil.which("tesseract"):
                _engine, _engine_name = pytesseract, "tesseract"
        except Exception:
            _engine = None
        return _engine, _engine_name


def engine_name() -> str | None:
    """Name of the available OCR engine, or ``None``."""

    return _probe()[1]


def reset_for_tests() -> None:
    global _engine, _engine_name, _probed
    with _lock:
        _engine, _engine_name, _probed = None, None, False


def _order(boxes: list[Any]) -> list[str]:
    """Reading order: group boxes into lines by vertical centre, then left-to-right."""

    items = []
    for box, text, score in boxes:
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        if float(score) < MIN_CONFIDENCE or not str(text).strip():
            continue
        items.append(((min(ys) + max(ys)) / 2, max(ys) - min(ys), min(xs), str(text).strip()))
    items.sort()
    lines: list[list[tuple[float, str]]] = []
    last_y, last_h = None, 0.0
    for y, h, x, text in items:
        if last_y is not None and abs(y - last_y) <= max(h, last_h) * 0.6:
            lines[-1].append((x, text))
        else:
            lines.append([(x, text)])
            last_y, last_h = y, h
    return [" ".join(t for _, t in sorted(line)) for line in lines]


def ocr_image(data: bytes) -> OcrResult | None:
    """OCR one encoded image (PNG/JPEG/...). ``None`` if no engine or nothing legible."""

    engine, name = _probe()
    if engine is None:
        return None
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.width * img.height > MAX_OCR_PIXELS:
                return None
            rgb = img.convert("RGB")
    except Exception:
        return None
    try:
        if name == "rapidocr":
            import numpy as np

            boxes, _ = engine(np.asarray(rgb))
            lines = _order(list(boxes or []))
            scores = [float(b[2]) for b in boxes or [] if float(b[2]) >= MIN_CONFIDENCE]
            conf = sum(scores) / len(scores) if scores else None
        else:
            text = str(engine.image_to_string(rgb))
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            conf = None
    except Exception:
        return None
    text = "\n".join(lines).strip()
    if not text:
        return None
    return OcrResult(text=text, engine=str(name), lines=len(lines), confidence=round(conf, 3) if conf else None)
