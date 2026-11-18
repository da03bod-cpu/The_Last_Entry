"""
postprocess.py - Offline document splitting, model output parsing,
number grounding, de-duplication and review flags.

The original 10,000-character target, PAGE markers, page overlap and
METADATA format are retained for regular documents. Oversized pages and
texts without PAGE markers are split into smaller virtual pages.
"""

import html
import json
import re
from difflib import SequenceMatcher

PAGE_RE = re.compile(r"^===== PAGE (\d+) =====\s*$", re.MULTILINE)
META_START = "===== METADATA ====="
META_END = "===== END METADATA ====="
FIELDS = [
    "type", "name", "year", "description", "beneficiaries_count", "budget",
    "beneficiary_value", "target_audience", "delivery_method", "notes",
]

AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
AR_DIAC = re.compile("[ً-ٰٟـ]")
GENERIC = {"برنامج", "مشروع", "مبادره", "فعاليه", "حمله", "دوره", "ملتقي", "ملتقى"}
EVENT_RE = re.compile(r"^(?:ال)?(ملتقى|منتدى|معرض|مهرجان|مؤتمر|المشاركة|مشاركة|نشر|اليوم العالمي|يوم)")


def norm(s) -> str:
    s = html.unescape(str(s or ""))
    s = AR_DIAC.sub("", s.translate(AR_DIGITS))
    s = re.sub("[أإآٱ]", "ا", s).replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def core(s) -> str:
    return " ".join(t for t in norm(s).split() if t not in GENERIC)


def name_score(a, b) -> float:
    ca, cb = core(a), core(b)
    if not ca or not cb:
        return 0.0
    if ca == cb:
        return 1.0
    if (ca in cb or cb in ca) and min(len(ca), len(cb)) >= 5:
        return 0.9
    return SequenceMatcher(None, ca, cb).ratio()


def number_in_text(value, text_n) -> bool:
    if value is None:
        return True
    try:
        digits = re.sub(r"\D", "", str(int(value)) if float(value).is_integer() else str(value))
    except Exception:  # noqa: BLE001
        return False
    flat = re.sub(r"(?<=\d)[,،.\s](?=\d{3}\b)", "", text_n)
    return digits in flat or digits in text_n.replace(" ", "")


# ---------------- Chunking (same defaults as prepare_chunks.py) ----------------
def strip_metadata(text: str):
    """Strip a preexisting METADATA block; explicit request fields take precedence."""
    if META_START not in text:
        return text, {}
    m = re.search(re.escape(META_START) + r"(.*?)" + re.escape(META_END), text, re.DOTALL)
    if not m:
        return text, {}
    vals = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            vals[k.strip()] = v.strip()
    return (text[:m.start()] + text[m.end():]).lstrip(), vals


def split_pages(text: str):
    matches = list(PAGE_RE.finditer(text))
    if not matches:
        return [(1, text.strip())]
    pages = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        pages.append((int(m.group(1)), text[m.end():end].strip()))
    return pages


def _split_oversized_page(text: str, target_chars: int, overlap_chars: int = 250):
    """Make virtual pages only when a single page exceeds the target size.

    Prefer paragraph/line/space boundaries, and retain a small overlap so
    a program mentioned at a break is less likely to lose its context.
    """
    if len(text) <= target_chars:
        return [text]
    pieces = []
    start = 0
    safe_overlap = min(overlap_chars, max(0, target_chars // 10))
    while start < len(text):
        hard_end = min(start + target_chars, len(text))
        end = hard_end
        if hard_end < len(text):
            lower = start + max(1, int(target_chars * 0.7))
            candidates = [
                text.rfind("\n\n", lower, hard_end),
                text.rfind("\n", lower, hard_end),
                text.rfind(" ", lower, hard_end),
            ]
            found = next((p for p in candidates if p > start), -1)
            if found != -1:
                end = found
        part = text[start:end].strip()
        if part:
            pieces.append(part)
        if end >= len(text):
            break
        start = max(start + 1, end - safe_overlap)
    return pieces


def build_chunks(pages, target_chars: int, overlap_pages: int):
    """Preserve regular page-aware splitting; split only oversized pages first.

    The target is per chunk, NOT a maximum total document length. The one
    oversized physical page can now create multiple virtual pages carrying
    the same original page number, preserving the PAGE marker format.
    """
    if target_chars <= 0:
        raise ValueError("CHUNK_TARGET_CHARS must be greater than zero")
    if overlap_pages < 0:
        raise ValueError("CHUNK_OVERLAP_PAGES must not be negative")

    expanded_pages = []
    for page_number, page_text in pages:
        for part in _split_oversized_page(page_text, target_chars):
            expanded_pages.append((page_number, part))

    chunks = []
    i, n = 0, len(expanded_pages)
    while i < n:
        j, size = i, 0
        while j < n and (j == i or size + len(expanded_pages[j][1]) <= target_chars):
            size += len(expanded_pages[j][1])
            j += 1
        chunks.append(expanded_pages[i:j])
        if j >= n:
            break
        i = max(j - overlap_pages, i + 1)
    return chunks


def render(chunk_pages) -> str:
    return "\n\n".join(f"===== PAGE {no} =====\n{txt}" for no, txt in chunk_pages)


def build_meta_block(name, year, period) -> str:
    return (f"{META_START}\nاسم الجمعية: {name or 'غير مذكور'}\nسنة التقرير: {year or 'غير مذكورة'}\n"
            f"الفترة: {period or 'تقرير سنوي'}\n{META_END}\n\n")


# ---------------- Parse model output ----------------
def parse_json_output(s: str):
    s = (s or "").strip()
    try:
        return json.loads(s), False
    except Exception:  # noqa: BLE001
        pass
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0)), False
        except Exception:  # noqa: BLE001
            pass
    try:
        from json_repair import repair_json
        fixed = repair_json(s, return_objects=True)
        if isinstance(fixed, dict):
            return fixed, True
    except Exception:  # noqa: BLE001
        pass
    return None, False


def to_number(v):
    if v is None or v == "" or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    s = str(v).translate(AR_DIGITS).replace(",", "").replace("،", "").strip()
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        f = float(s)
        return int(f) if f.is_integer() else f
    return None


def clean_program(p, chunk_text_n, review):
    """Normalize ten fields, null description, ground two numeric fields."""
    if not isinstance(p, dict) or not str(p.get("name") or "").strip():
        return None
    out = {k: p.get(k) for k in FIELDS}
    out["name"] = str(out["name"]).strip()
    out["description"] = None
    for k in ("beneficiaries_count", "budget"):
        out[k] = to_number(out[k])
        if out[k] is not None and not number_in_text(out[k], chunk_text_n):
            review.append({"name": out["name"], "reason": f"{k} غير مكتوب في النص - اتحوّل null"})
            out[k] = None
    if EVENT_RE.search(out["name"]):
        review.append({"name": out["name"], "reason": "شكله فعالية/مشاركة خارجية - راجع هل هو برنامج للجمعية"})
    return out


def _compatible(p, q):
    for k in ("beneficiaries_count", "budget"):
        a, b = p.get(k), q.get(k)
        if a not in (None, "") and b not in (None, "") and a != b:
            return False
    return True


def merge_programs(lists, thr=0.9):
    """Merge between chunks only, if names and numeric fields are compatible."""
    merged = []
    for ci, lst in enumerate(lists):
        for p in lst:
            for qi, q in merged:
                if qi < ci and name_score(p.get("name"), q.get("name")) >= thr and _compatible(p, q):
                    for k, v in p.items():
                        if q.get(k) in (None, "") and v not in (None, ""):
                            q[k] = v
                    break
            else:
                merged.append((ci, dict(p)))
    return [q for _, q in merged]
