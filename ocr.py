"""CPU-friendly OCR using RapidOCR (ONNX Runtime) with PP-OCRv5 Latin model."""
from functools import lru_cache
from io import BytesIO
from itertools import combinations, product
from pathlib import Path
import re

import cv2
import numpy as np
from PIL import Image, ImageOps, ImageSequence
from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR

IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"})
MAX_OCR_PAGES = 3
_HUNSPELL_PL_PREFIX = "/usr/share/hunspell/pl_PL"

# Foreign diacritics emitted by the generic Latin model that do not exist in Polish.
_FOREIGN_TO_PL = str.maketrans({
    "š": "ś", "Š": "Ś",
    "ž": "ż", "Ž": "Ż",
    "ň": "ń", "Ň": "Ń",
    "č": "ć", "Č": "Ć",
    "é": "e", "É": "E",
    "ç": "ę", "Ç": "Ę",
    "ř": "r", "Ř": "R",
    "ď": "d", "Ď": "D",
    "ť": "t", "Ť": "T",
})

_DIACRITIC_ALTS = {
    "a": ("ą",),
    "c": ("ć",),
    "e": ("ę",),
    "l": ("ł",),
    "n": ("ń",),
    "o": ("ó",),
    "s": ("ś",),
    "z": ("ż", "ź"),
    "ż": ("ź", "z"),
    "ź": ("ż", "z"),
}

_OCR_SHAPE_ALTS_PRIMARY = {
    "t": ("ł",),
    "i": ("l", "ł"),
    "e": ("c",),
    "c": ("e",),
    "p": ("o",),
    "q": ("ą", "a"),
}

_OCR_SHAPE_ALTS_SECONDARY = {
    "t": ("l",),
    "l": ("ł", "t", "i"),
    "o": ("p",),
}

_WORD_RE = re.compile(r"(?<![A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż])([A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]{3,})(?![A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż])")


@lru_cache(maxsize=1)
def _get_polish_dict():
    if not Path(_HUNSPELL_PL_PREFIX + ".dic").exists():
        return None
    try:
        from spylls.hunspell import Dictionary
        return Dictionary.from_files(_HUNSPELL_PL_PREFIX)
    except Exception:
        return None


def _match_case(src, dst):
    if src.isupper():
        return dst.upper()
    if src[0].isupper():
        return dst[0].upper() + dst[1:]
    return dst


def _dict_has(pl_dict, candidate, allow_proper):
    if pl_dict.lookup(candidate):
        return True
    if allow_proper and pl_dict.lookup(candidate[0].upper() + candidate[1:]):
        return True
    return False


@lru_cache(maxsize=4096)
def _fix_polish_word(word):
    pl_dict = _get_polish_dict()
    if pl_dict is None:
        return word
    if word.isupper() and len(word) <= 4:
        return word

    lower = word.lower()
    allow_proper = word[0].isupper()
    if pl_dict.lookup(word) or pl_dict.lookup(lower) or (word.isupper() and pl_dict.lookup(word.capitalize())):
        return word

    # Tier 1: pure Polish diacritic restoration (1..3 diacritic changes)
    diac_positions = [(i, _DIACRITIC_ALTS[ch]) for i, ch in enumerate(lower) if ch in _DIACRITIC_ALTS]
    if diac_positions:
        chars = list(lower)
        for max_edits in (1, 2, 3):
            if max_edits > len(diac_positions):
                break
            if max_edits == 2 and len(diac_positions) > 12:
                break
            if max_edits == 3 and len(diac_positions) > 8:
                break
            matches = set()
            for combo in combinations(diac_positions, max_edits):
                for repls in product(*(alts for _, alts in combo)):
                    for (idx, _), r in zip(combo, repls):
                        chars[idx] = r
                    cand = "".join(chars)
                    if _dict_has(pl_dict, cand, allow_proper):
                        matches.add(cand)
                        if len(matches) > 1:
                            break
                for idx, _ in combo:
                    chars[idx] = lower[idx]
                if len(matches) > 1:
                    break
            if len(matches) == 1:
                return _match_case(word, next(iter(matches)))
            if len(matches) > 1 and allow_proper:
                proper = [c for c in matches if pl_dict.lookup(c[0].upper() + c[1:]) and not pl_dict.lookup(c)]
                if len(proper) == 1:
                    return _match_case(word, proper[0])

    # Tier 2: 1-character visual OCR shape confusion (primary bar/glyph confusions first, then secondary)
    chars = list(lower)
    for alt_table in (_OCR_SHAPE_ALTS_PRIMARY, _OCR_SHAPE_ALTS_SECONDARY):
        shape_matches = set()
        for i, ch in enumerate(lower):
            alts = alt_table.get(ch)
            if not alts:
                continue
            for r in alts:
                chars[i] = r
                cand = "".join(chars)
                if _dict_has(pl_dict, cand, allow_proper):
                    shape_matches.add(cand)
                    if len(shape_matches) > 1:
                        break
            chars[i] = ch
            if len(shape_matches) > 1:
                break
        if len(shape_matches) == 1:
            return _match_case(word, next(iter(shape_matches)))

    return word


def normalize_ocr_text(text):
    """Fix common Polish diacritic and OCR shape errors using Hunspell PL."""
    if not text:
        return text
    text = text.translate(_FOREIGN_TO_PL)
    # Trailing/medial 'q' misread from 'ą' in Polish words (e.g. budowq -> budową)
    text = re.sub(r"([a-ząćęłńóśźż]{3,})q\b", r"\1ą", text)
    text = re.sub(r"([a-ząćęłńóśźż]{3,})qc([aąeęi])", r"\1ąc\2", text)
    # Common shape confusions in legal entities, punctuation, and identifiers
    text = re.sub(r"([A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż])·([A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż])", r"\1 \2", text)
    text = re.sub(r"([A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+)\s+\.\s+([A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+)", r"\1 \2", text)
    text = re.sub(r"\b([Ss][Pp]\.\s*[Zz]\s*)[0oO]\.[0oO]\.", lambda m: m.group(1) + ("O.O." if m.group(1).isupper() else "o.o."), text)
    text = re.sub(r"\bN1P\b", "NIP", text)
    text = re.sub(r"\bzP/", "ZP/", text)

    # Full Polish dictionary correction (skip email addresses and URLs)
    def _replace_word(match):
        start, end = match.span()
        tok_start = max(text.rfind(" ", 0, start), text.rfind("\n", 0, start)) + 1
        tok_end = len(text)
        for sep in (" ", "\n", "\t"):
            pos = text.find(sep, end)
            if pos != -1 and pos < tok_end:
                tok_end = pos
        token = text[tok_start:tok_end]
        if "@" in token or "://" in token or token.lower().startswith("www."):
            return match.group(0)
        return _fix_polish_word(match.group(0))

    text = _WORD_RE.sub(_replace_word, text)
    # Unambiguous contextual Polish phrases where the ASCII form is a different valid Polish word
    text = re.sub(r"\b(op)l(at[aąey]\s+(?:skarbow|sądow|od\s+odwoł))", lambda m: m.group(1) + ("Ł" if m.group(1).isupper() else "ł") + m.group(2), text, flags=re.IGNORECASE)
    text = re.sub(r"\b(wraz\s+z\s+modernizacj)a\b", r"\1ą", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(wraz\s+z\s+budow)a\b", r"\1ą", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(z\s+ograniczon)a(\s+odpowiedzialnością)\b", r"\1ą\2", text, flags=re.IGNORECASE)
    return text


@lru_cache(maxsize=1)
def get_engine():
    return RapidOCR(
        params={
            "Global.use_det": True,
            "Global.use_cls": False,
            "Global.use_rec": True,
            "Det.ocr_version": OCRVersion.PPOCRV5,
            "Det.model_type": ModelType.MOBILE,
            "Det.limit_side_len": 1280,
            "Det.thresh": 0.25,
            "Det.box_thresh": 0.45,
            "Det.unclip_ratio": 1.7,
            "Rec.ocr_version": OCRVersion.PPOCRV5,
            "Rec.model_type": ModelType.MOBILE,
            "Rec.lang_type": LangRec.LATIN,
        }
    )


def warmup():
    engine = get_engine()
    engine._load_det_model()
    engine._load_rec_model()
    _get_polish_dict()


def _boxes_to_text(result):
    if not result or not result.txts or result.boxes is None:
        return ""

    items = []
    for box, txt, score in zip(result.boxes, result.txts, result.scores):
        text = " ".join(str(txt).split())
        if not text:
            continue
        if score is not None and score < 0.35 and not any(c.isalnum() for c in text):
            continue
        xs = [float(pt[0]) for pt in box]
        ys = [float(pt[1]) for pt in box]
        x_min, x_max = min(xs), max(xs)
        y_min, y_max = min(ys), max(ys)
        height = max(y_max - y_min, 1.0)
        items.append((y_min, y_max, (y_min + y_max) / 2.0, height, x_min, x_max, text))

    if not items:
        return ""

    items.sort(key=lambda item: (item[2], item[4]))

    rows = []
    for item in items:
        y_min, y_max, y_mid, height, x_min, x_max, text = item
        placed = False
        for row in reversed(rows[-5:]):
            r_mid = row["y_mid"]
            r_h = row["height"]
            tol = max(min(height, r_h) * 0.45, 5.0)
            if abs(y_mid - r_mid) > tol:
                continue
            overlaps_x = any(
                min(x_max, other[5]) - max(x_min, other[4]) > min(x_max - x_min, other[5] - other[4]) * 0.15
                for other in row["items"]
            )
            if overlaps_x:
                continue
            row["items"].append(item)
            n = len(row["items"])
            row["y_mid"] = (r_mid * (n - 1) + y_mid) / n
            row["height"] = (r_h * (n - 1) + height) / n
            placed = True
            break
        if not placed:
            rows.append({"y_mid": y_mid, "height": height, "items": [item]})

    rows.sort(key=lambda row: row["y_mid"])
    lines = []
    for row in rows:
        row_items = sorted(row["items"], key=lambda item: item[4])
        parts = [row_items[0][6]]
        for prev, curr in zip(row_items, row_items[1:]):
            gap = curr[4] - prev[5]
            sep = "  " if gap > max(prev[3], curr[3]) * 0.8 else " "
            parts.append(sep + curr[6])
        lines.append("".join(parts).strip())
    return normalize_ocr_text("\n".join(line for line in lines if line))


def _preprocess_rgb(image):
    rgb = np.array(ImageOps.exif_transpose(image).convert("RGB"))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    is_binary = len(np.unique(gray)) <= 4
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    coords = np.column_stack(np.where(bw > 0))
    if len(coords) > 200:
        rect = cv2.minAreaRect(coords[:, ::-1].astype(np.float32))
        angle = rect[-1]
        if angle < -45:
            angle = 90 + angle
        elif angle > 45:
            angle = angle - 90
        if 0.3 <= abs(angle) <= 5.0:
            h, w = rgb.shape[:2]
            matrix = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
            rgb = cv2.warpAffine(rgb, matrix, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    if not is_binary:
        blur = cv2.GaussianBlur(rgb, (0, 0), 1.0)
        rgb = cv2.addWeighted(rgb, 1.3, blur, -0.3, 0)
    return rgb


def ocr_pil_image(image):
    array = _preprocess_rgb(image)
    result = get_engine()(array)
    return _boxes_to_text(result)


def ocr_image(source, max_pages=MAX_OCR_PAGES):
    """Run OCR on an image path or raw image bytes (supports multi-page TIFF)."""
    if isinstance(source, (bytes, bytearray)):
        stream = BytesIO(source)
        opener = lambda: Image.open(stream)
    else:
        path = Path(source)
        opener = lambda: Image.open(path)

    pages = []
    with opener() as img:
        for index, frame in enumerate(ImageSequence.Iterator(img)):
            if index >= max_pages:
                break
            text = ocr_pil_image(frame.copy())
            if text:
                pages.append(text)
    return "\n\n".join(pages).strip()
