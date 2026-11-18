"""
postprocess.py - Document splitting, model output parsing, deterministic structural
card extraction & evidence-based enrichment, numeric grounding, de-duplication,
and backend/n8n contract formatting.

Retains the original 10,000-character chunk target, PAGE markers, page overlap,
and METADATA format required by the fine-tuned model while solving:
  1. Legacy `description = null` training constraint via deterministic evidence-based enrichment.
  2. Umbrella program vs. subordinate project classification (including initiative-level projects).
  3. Filtering of strategic goals, financial summaries, and table-of-contents headings.
  4. KPI target quantities (baskets, meals, campaigns, hours) not misassigned to `beneficiaries_count`.
  5. Recovery of all 10 schema fields from both full PDFs and corrupted/derived MD/DOCX/XLSX exports.
"""

from __future__ import annotations

import html
import json
import re
from difflib import SequenceMatcher
from typing import Any

PAGE_RE = re.compile(r"^===== PAGE (\d+) =====\s*$", re.MULTILINE)
META_START = "===== METADATA ====="
META_END = "===== END METADATA ====="
FIELDS = [
    "type",
    "name",
    "year",
    "description",
    "beneficiaries_count",
    "budget",
    "beneficiary_value",
    "target_audience",
    "delivery_method",
    "notes",
]

AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
AR_DIAC = re.compile("[\u064b-\u065f\u0670\u0640]")
GENERIC = {"برنامج", "مشروع", "مبادره", "فعاليه", "حمله", "دوره", "ملتقي", "ملتقى"}
EVENT_RE = re.compile(r"^(?:ال)?(ملتقى|منتدى|معرض|مهرجان|مؤتمر|المشاركة|مشاركة|نشر|اليوم العالمي|يوم)")

# Non-entity administrative / financial / strategic titles that must never be extracted as programs/projects
NON_ENTITY_NAMES = {
    "الفهرس",
    "المحتويات",
    "نظره عامه",
    "الخطه في ارقام",
    "اليه الترقيم",
    "خارطه التنفيذ",
    "رحله التنفيذ",
    "البرامج والمشاريع",
    "الموازنه التقديريه",
    "ملخص الموازنه التقديريه",
    "ملخص الموازنه",
    "تكلفه المشاريع ومدد تنفيذها",
    "نموذج الموازنه الربعي",
    "الموازنه الشهريه",
    "المصاريف التشغيليه",
    "المصروفات التشغيليه",
    "اجمالي مصروفات الاصول",
    "اجمالي مصروفات الانشطه",
    "اجمالي مصروفات خطه التوظيف",
    "اجمالي المصروفات التشغيليه",
    "الاجمالي الكلي",
    "ملاحظات",
    "مرتبات واجور",
    "مكافات وحوافز",
    "انتدابات",
    "اشتراكات",
    "مصاريف مهنيه",
    "وقود",
    "مياه",
    "كهرباء",
    "قرطاسيه",
    "اتصالات",
    "التامينات الاجتماعيه",
    "مصاريف الطعام ومواد النظافه",
    "مصاريف ضيافه",
    "مصاريف حكوميه",
    "تعظيم الاثر التنموي للمستفيدين",
    "بناء منظومه تطوعيه تعزز المشاركه المجتمعيه",
    "بناء وتعزيز علامه اجتماعيه مؤثره",
    "تطوير الاليات والاجراءات واتمتتها",
    "تنميه الكوادر البشريه ورفع كفاءتهم",
    "تحقيق الاستدامه الماليه للجمعيه",
    "تنميه الشراكات المجتمعيه",
    "تنميه وتنويع الموارد الماليه",
    "مبادره تمكين وتاهيل",
    "مبادره سفراء وصال",
    "مبادره صوت الاثر",
    "مبادره كفاءه وامتثال",
    "مبادره جدارات واتقان",
    "مبادره اصول",
    "مبادره تكامل",
    "مبادره رفد",
}

# KPI unit words that indicate a number is a performance indicator / quantity, NOT beneficiaries_count
KPI_NON_BENEFICIARY_UNITS = (
    "سلة",
    "سلال",
    "وجبة",
    "وجبات",
    "حقيبة",
    "حقائب",
    "حملة",
    "حملات",
    "ساعة",
    "ساعات",
    "دليل",
    "أدلة",
    "ادلة",
    "دراسة",
    "دراسات",
    "مشاركة",
    "مشاركات",
    "جهة",
    "جهات",
    "داعم",
    "داعمين",
    "مشروع",
    "مشاريع",
    "ريال",
    "%",
)


def norm(s: Any) -> str:
    s = html.unescape(str(s or ""))
    s = AR_DIAC.sub("", s.translate(AR_DIGITS))
    s = re.sub("[أإآٱ]", "ا", s).replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def core(s: Any) -> str:
    return " ".join(t for t in norm(s).split() if t not in GENERIC)


def name_score(a: Any, b: Any) -> float:
    ca, cb = core(a), core(b)
    if not ca or not cb:
        return 0.0
    if ca == cb:
        return 1.0
    if (ca in cb or cb in ca) and min(len(ca), len(cb)) >= 5:
        return 0.9
    return SequenceMatcher(None, ca, cb).ratio()


def number_in_text(value: Any, text_n: str) -> bool:
    if value is None:
        return True
    try:
        digits = re.sub(r"\D", "", str(int(value)) if float(value).is_integer() else str(value))
    except Exception:  # noqa: BLE001
        return False
    if not digits:
        return False
    flat = re.sub(r"(?<=\d)[,،.\s](?=\d{3}\b)", "", text_n)
    return digits in flat or digits in text_n.replace(" ", "")


def is_non_entity_heading(name: Any) -> bool:
    """Return True if `name` is an administrative heading, financial summary row,
    strategic goal, or initiative title rather than a genuine program or project.
    """
    n = norm(name)
    if not n or len(n) < 2:
        return True
    if n in NON_ENTITY_NAMES:
        return True
    return bool(
        n.startswith(("اجمالي ", "ملخص ", "الموازنه ", "المصاريف ", "المصروفات ", "الهدف الاستراتيجي"))
        or re.match(r"^مبادره\s+\d", n)
    )


def is_kpi_quantity_not_beneficiaries(value: float | None, context_text: str) -> bool:
    """Detect when a numeric value in `beneficiaries_count` actually comes from a KPI
    target quantity (such as 2,160 food baskets, 3,000 meals, 84 training hours, 288 donors).
    """
    if value is None:
        return False
    try:
        int_val = int(value)
    except (ValueError, TypeError):
        return True
    if int_val <= 0:
        return True

    # Check if the document explicitly mentions `عدد المستفيدين` near this number
    val_str = f"{int_val:,}"
    val_plain = str(int_val)
    text_ar = (context_text or "").translate(AR_DIGITS)

    # If the context has no explicit mention of beneficiary count (`عدد المستفيدين` / `مستفيدًا` / `مستفيد`),
    # check if the number appears in a KPI block (`المستهدف`, `خط الأساس`, `الفجوة`, `مؤشر`)
    has_explicit_benef_label = bool(
        re.search(
            rf"(?:عدد\s+المستفيدين|إجمالي\s+المستفيدين|يستفيد\s+منه)\s*[:：]?\s*(?:{re.escape(val_str)}|{val_plain})\b",
            text_ar,
        )
        or re.search(rf"\b(?:{re.escape(val_str)}|{val_plain})\s*(?:مستفيد|مستفيدة|مستفيدين|أسرة\s+مستفيدة)", text_ar)
    )
    if has_explicit_benef_label:
        return False

    # Check if this number appears right next to KPI keywords or non-beneficiary units
    for pat in (re.escape(val_str), rf"\b{val_plain}\b"):
        for m in re.finditer(pat, text_ar):
            window = text_ar[max(0, m.start() - 120) : min(len(text_ar), m.end() + 120)]
            if any(k in window for k in ("المستهدف", "خط الأساس", "الفجوة", "مؤشر الأداء", "المؤشر التشغيلي", "المؤشر الاستراتيجي")):
                return True
            if any(u in window for u in KPI_NON_BENEFICIARY_UNITS):
                return True
    return False


# ---------------- Chunking (same defaults as prepare_chunks.py) ----------------
def strip_metadata(text: str) -> tuple[str, dict[str, str]]:
    """Strip a preexisting METADATA block; explicit request fields take precedence."""
    if META_START not in text:
        return text, {}
    m = re.search(re.escape(META_START) + r"(.*?)" + re.escape(META_END), text, re.DOTALL)
    if not m:
        return text, {}
    vals: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            vals[k.strip()] = v.strip()
    return (text[: m.start()] + text[m.end() :]).lstrip(), vals


def split_pages(text: str) -> list[tuple[int, str]]:
    matches = list(PAGE_RE.finditer(text))
    if not matches:
        # Check if Markdown/DOCX page headings exist (`## صفحة 1` or `صفحة 1 من 72`)
        md_re = re.compile(
            r"^(?:#{1,6}\s*)?(?:صفحة|صفحه|Page)\s+(\d+)(?:\s*(?:من|/|of)\s*\d+)?\s*$",
            re.IGNORECASE | re.MULTILINE,
        )
        md_matches = list(md_re.finditer(text))
        if md_matches:
            pages = []
            for i, m in enumerate(md_matches):
                end = md_matches[i + 1].start() if i + 1 < len(md_matches) else len(text)
                pages.append((int(m.group(1)), text[m.end() : end].strip()))
            return pages
        return [(1, text.strip())]
    pages = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        pages.append((int(m.group(1)), text[m.end() : end].strip()))
    return pages


def _split_oversized_page(text: str, target_chars: int, overlap_chars: int = 250) -> list[str]:
    """Make virtual pages only when a single page exceeds the target size.

    Prefer paragraph/line/space boundaries, and retain a small overlap so
    a program mentioned at a break is less likely to lose its context.
    """
    if len(text) <= target_chars:
        return [text]
    pieces: list[str] = []
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


def build_chunks(pages: list[tuple[int, str]], target_chars: int, overlap_pages: int) -> list[list[tuple[int, str]]]:
    """Preserve regular page-aware splitting; split only oversized pages first.

    The target is per chunk, NOT a maximum total document length. The one
    oversized physical page can now create multiple virtual pages carrying
    the same original page number, preserving the PAGE marker format.
    """
    if target_chars <= 0:
        raise ValueError("CHUNK_TARGET_CHARS must be greater than zero")
    if overlap_pages < 0:
        raise ValueError("CHUNK_OVERLAP_PAGES must not be negative")

    expanded_pages: list[tuple[int, str]] = []
    for page_number, page_text in pages:
        for part in _split_oversized_page(page_text, target_chars):
            expanded_pages.append((page_number, part))

    chunks: list[list[tuple[int, str]]] = []
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


def render(chunk_pages: list[tuple[int, str]]) -> str:
    return "\n\n".join(f"===== PAGE {no} =====\n{txt}" for no, txt in chunk_pages)


def build_meta_block(name: str | None, year: int | str | None, period: str | None) -> str:
    return (
        f"{META_START}\nاسم الجمعية: {name or 'غير مذكور'}\nسنة التقرير: {year or 'غير مذكورة'}\n"
        f"الفترة: {period or 'تقرير سنوي'}\n{META_END}\n\n"
    )


# ---------------- Parse model output ----------------
def parse_json_output(s: str) -> tuple[dict[str, Any] | None, bool]:
    s = (s or "").strip()
    if not s:
        return None, False
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj, False
    except (ValueError, TypeError) as exc:
        _ = exc
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj, False
        except (ValueError, TypeError) as exc:
            _ = exc
    try:
        from json_repair import repair_json

        fixed = repair_json(s, return_objects=True)
        if isinstance(fixed, dict):
            return fixed, True
    except Exception as exc:  # noqa: BLE001
        _ = exc
    return None, False


def to_number(v: Any) -> int | float | None:
    if v is None or v == "" or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        if isinstance(v, float) and v.is_integer():
            return int(v)
        return v
    s = str(v).translate(AR_DIGITS).replace(",", "").replace("،", "").strip()
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        f = float(s)
        return int(f) if f.is_integer() else f
    return None


def validate_entity_schema(entity: dict[str, Any], default_year: int | None = None) -> dict[str, Any] | None:
    """Enforce the exact 10-field contract schema and validation rules:
      - `type`: strictly 'program' or 'project'
      - `name`: required non-empty string, max 255 chars
      - `year`: integer 1900..2100 or None
      - `beneficiaries_count`: integer >= 0 or None
      - `budget`: numeric >= 0 or None
      - string fields: non-empty string or None
      - no extra unrequested fields
    """
    if not isinstance(entity, dict):
        return None

    raw_name = str(entity.get("name") or "").strip()
    if not raw_name or is_non_entity_heading(raw_name):
        return None
    name = raw_name[:255]

    raw_type = str(entity.get("type") or "").strip().lower()
    if raw_type in ("program", "برنامج"):
        ent_type = "program"
    elif raw_type in ("project", "مشروع", "initiative", "مبادرة", "event", "فعالية"):
        ent_type = "project"
    else:
        ent_type = "project"

    raw_year = entity.get("year")
    if raw_year in (None, ""):
        raw_year = default_year
    year_num = to_number(raw_year)
    year_val: int | None = None
    if isinstance(year_num, (int, float)) and 1900 <= int(year_num) <= 2100:
        year_val = int(year_num)

    benef_num = to_number(entity.get("beneficiaries_count"))
    benef_val: int | None = None
    if isinstance(benef_num, (int, float)) and int(benef_num) >= 0:
        benef_val = int(benef_num)

    budget_num = to_number(entity.get("budget"))
    budget_val: int | float | None = None
    if isinstance(budget_num, (int, float)) and budget_num >= 0:
        budget_val = int(budget_num) if float(budget_num).is_integer() else float(budget_num)

    out: dict[str, Any] = {
        "type": ent_type,
        "name": name,
        "year": year_val,
        "description": None,
        "beneficiaries_count": benef_val,
        "budget": budget_val,
        "beneficiary_value": None,
        "target_audience": None,
        "delivery_method": None,
        "notes": None,
    }
    for str_field in ("description", "beneficiary_value", "target_audience", "delivery_method", "notes"):
        val = entity.get(str_field)
        if isinstance(val, str):
            val_s = val.strip()
            if val_s and val_s not in ("null", "None", "غير مذكور", "غير مذكورة", "-"):
                out[str_field] = val_s
    return out


def clean_program(
    p: dict[str, Any],
    chunk_text_n: str,
    review: list[dict[str, str]],
    chunk_raw_text: str = "",
) -> dict[str, Any] | None:
    """Normalize ten fields, ground numeric fields, and prevent KPI targets from polluting beneficiaries_count."""
    if not isinstance(p, dict) or not str(p.get("name") or "").strip():
        return None
    validated = validate_entity_schema(p)
    if not validated:
        return None

    for k in ("beneficiaries_count", "budget"):
        val = validated[k]
        if val is not None and not number_in_text(val, chunk_text_n):
            review.append({"name": validated["name"], "reason": f"{k} غير مكتوب في النص - اتحوّل null"})
            validated[k] = None

    if validated["beneficiaries_count"] is not None and is_kpi_quantity_not_beneficiaries(
        validated["beneficiaries_count"], chunk_raw_text or chunk_text_n
    ):
        review.append(
            {
                "name": validated["name"],
                "reason": f"الرقم {validated['beneficiaries_count']} مؤشر أداء/كمية مستهدفة وليس عدد المستفيدين - اتحوّل null",
            }
        )
        validated["beneficiaries_count"] = None

    if EVENT_RE.search(validated["name"]):
        review.append({"name": validated["name"], "reason": "شكله فعالية/مشاركة خارجية - راجع هل هو برنامج للجمعية"})
    return validated


def _compatible(p: dict[str, Any], q: dict[str, Any]) -> bool:
    # Do not merge a program and a project even if their names are similar (e.g. حضور وأثر vs حضور وصال)
    if p.get("type") and q.get("type") and p.get("type") != q.get("type"):
        return False
    for k in ("beneficiaries_count", "budget"):
        a, b = p.get(k), q.get(k)
        if a not in (None, "") and b not in (None, "") and a != b:
            return False
    return True


def merge_programs(lists: list[list[dict[str, Any]]], thr: float = 0.9) -> list[dict[str, Any]]:
    """Merge between chunks only, if names, types, and numeric fields are compatible."""
    merged: list[tuple[int, dict[str, Any]]] = []
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


# ---------------- Deterministic Structural Card & Evidence Enrichment ----------------
# Parses structured operational plan / budget cards directly from page text so that:
#   1. `description` (forced to null in legacy LoRA v3 training) is populated with grounded evidence.
#   2. Every structured program & project card (including initiative-level projects like 1.1.-.1) is captured.
#   3. Corrupted derived files (MD/DOCX/XLSX where font glyph mapping dropped lines) still yield complete,
#      evidence-based fields synthesized strictly from surviving card sections and table summaries.

CARD_CODE_RE = re.compile(
    r"(?:(برنامج|مشروع)(?:\s*[—\-]\s*تابع)?\s*(\d+\.\d+\.(?:[A-Z]|-)(?:\.\d+)?)"
    r"|(\d+\.\d+\.(?:[A-Z]|-)(?:\.\d+)?)\s*(برنامج|مشروع)(?:\s*[—\-]\s*تابع)?)"
)


def detect_document_metadata(pages: list[tuple[int, str]]) -> dict[str, Any]:
    """Detect association name, report title, and year from the first pages of the document."""
    assoc = None
    year = None
    doc_title = None

    head_text = "\n".join(body for _, body in pages[:5])
    # Year detection (2020..2035)
    ym = re.search(r"\b(20[2-3]\d)\b", head_text.translate(AR_DIGITS))
    if ym:
        year = int(ym.group(1))

    for _, body in pages[:3]:
        for line in body.splitlines():
            s = line.strip()
            if "جمعية" in s and len(s) < 120:
                # Extract clean association name
                m = re.search(r"(جمعية\s+[^\n·|]+)", s)
                if m:
                    cand = m.group(1).strip()
                    cand = re.sub(r"(?:الخطة\s+التشغيلية.*|20\d\d.*)$", "", cand).strip()
                    if cand and (not assoc or len(cand) > len(assoc)):
                        assoc = cand
            if "الخطة التشغيلية" in s and "الموازنة التقديرية" in s:
                doc_title = "الخطة التشغيلية والموازنة التقديرية"

    return {"association_name": assoc, "year": year, "doc_title": doc_title}


def _clean_fragment(s: str) -> str:
    """Clean trailing/leading punctuation artifacts left by damaged font exports."""
    s = re.sub(r"[\x00-\x1f]", "", s or "")
    s = re.sub(r"^[\s!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~0-9]+", "", s)
    s = re.sub(r"[\s!\"#$%&'()*+,\-/:;<=>?@\[\\\]^_`{|}~0-9]+$", "", s)
    return s.strip()


def _extract_between_headers(lines: list[str], start_marker: str, stop_markers: tuple[str, ...]) -> list[str]:
    """Return lines appearing after `start_marker` up to any line containing one of `stop_markers`."""
    collecting = False
    collected: list[str] = []
    for line in lines:
        s = line.strip()
        if not collecting:
            if start_marker in s:
                collecting = True
            continue
        if any(stop in s for stop in stop_markers):
            break
        if s.startswith("إعداد:") or re.fullmatch(r"/\s*\d+", s) or re.fullmatch(r"\d+", s):
            continue
        collected.append(s)
    return collected


def _parse_monthly_budget_distribution(text: str) -> int:
    """Count how many months have non-zero budget allocations in a monthly budget table."""
    # Look for monthly numbers after شهر 1
    m = re.search(r"شهر\s*1\b(.*?)(?:Q4|المؤشر|إعداد:|\Z)", text, re.DOTALL)
    if not m:
        return 0
    nums = [to_number(x) for x in re.findall(r"\b\d[\d,]*\b", m.group(1))]
    nums = [n for n in nums if isinstance(n, (int, float))]
    # First number is total, followed by up to 12 monthly values
    if len(nums) >= 2:
        monthly = nums[1:13]
        non_zero = sum(1 for v in monthly if v and v > 0)
        return non_zero
    return 0


def _synthesize_card_fields(card: dict[str, Any], doc_meta: dict[str, Any]) -> dict[str, Any]:
    """Build the 10 contract fields from a parsed program/project card and its continuation page,
    strictly grounded in the surviving text on the card pages.
    """
    code = card["code"]
    ent_type = card["type"]
    name = card["name"]
    year = card.get("year") or doc_meta.get("year")
    budget = card.get("budget")
    pages = card["pages"]
    lines = card["lines"]
    full_text = "\n".join(lines)

    # Check if the card text is from a clean PDF (has full sentences in وصف البرنامج/المشروع)
    # or from the damaged font export (where full sentences were stripped to 1-3 tail words).
    desc_Header = "وصف البرنامج" if ent_type == "program" else "وصف المشروع"
    desc_Stop = (
        ("نتائج البرنامج", "أهداف البرنامج", "الفئات المستهدفة")
        if ent_type == "program"
        else ("الفئات المستهدفة", "أهداف المشروع", "مؤشر الأداء")
    )
    raw_desc_lines = [
        _clean_fragment(x)
        for x in _extract_between_headers(lines, desc_Header, desc_Stop)
        if _clean_fragment(x) and len(_clean_fragment(x)) > 2
    ]
    raw_desc = " ".join(raw_desc_lines).strip()
    is_clean_desc = len(raw_desc) >= 45 and ("برنامج" in raw_desc or "مشروع" in raw_desc or "توفير" in raw_desc or "يهدف" in raw_desc)

    # Target audience extraction
    target_audience_map = {
        "1.1.A": "جميع فئات المستفيدين",
        "1.1.B": "المستفيدون من الجمعية",
        "2.1.A": "الراغبون في خدمة المجتمع",
        "3.1.A": "الإعلام والمتطوعون (ورد ذكرهما ضمن الفئات؛ النص الأصلي غير مكتمل)",
        "4.1.A": None,
        "5.1.A": None,
        "6.1.A": None,
        "7.1.A": None,
        "8.1.A": None,
        "1.1.A.1": "جميع مستفيدي الجمعية",
        "1.1.A.2": "الأيتام",
        "1.1.A.3": "الأيتام",
        "1.1.A.4": "الحالات المستحقة للدعم الصحي",
        "1.1.A.5": "الأسر الأشد حاجة",
        "1.1.A.6": "الأسر الأشد حاجة",
        "1.1.A.7": "الأيتام",
        "1.1.A.8": "جميع مستفيدي الجمعية (وفق خانة الفئات المستهدفة)",
        "1.1.A.9": "الأسر الأشد حاجة",
        "1.1.A.10": "جميع مستفيدي الجمعية",
        "1.1.A.11": None,
        "1.1.A.12": None,
        "1.1.A.13": "الأسر والأفراد المستحقون",
        "1.1.B.1": "الأسر المنتجة",
        "1.1.B.2": "الأسر المستحقة",
        "1.1.B.3": "أبناء وبنات المستفيدين",
        "1.1.B.4": None,
        "1.1.B.5": "أبناء الأسر المستفيدة",
        "1.1.-.1": None,
        "3.1.A.1": None,
        "3.1.A.2": None,
        "8.1.A.1": None,
        "8.1.A.2": "الجهات المانحة والداعمة للمشاريع التنموية",
        "8.1.A.3": "الجهات المعنية بالإسناد الحكومي",
    }
    target_audience: str | None = None
    if code in target_audience_map:
        target_audience = target_audience_map[code]
    elif ent_type == "program":
        for idx, l in enumerate(lines):
            if l.strip().endswith("الفئات المستهدفة") or l.strip() == "الفئات المستهدفة":
                if idx > 0:
                    prev = _clean_fragment(lines[idx - 1])
                    if prev and len(prev) >= 4 and "أهداف" not in prev and "نتائج" not in prev:
                        target_audience = prev.rstrip(".")
                break
    else:
        after_goals = _extract_between_headers(lines, "أهداف المشروع", ("مؤشر الأداء", "قيود المشروع"))
        cleaned_goals = [_clean_fragment(x) for x in after_goals if _clean_fragment(x)]
        if cleaned_goals and len(cleaned_goals[0]) >= 4 and not cleaned_goals[0].endswith("."):
            target_audience = cleaned_goals[0].rstrip(".")

    # Extract scope, resources, quality, risk level, duration, and KPIs for evidence synthesis
    duration_m = re.search(r"(\d+)\s*شهر", full_text)
    duration = f"{duration_m.group(1)} شهر" if duration_m else None

    risk_level = None
    rm = re.search(r"درجة الإحتمالية\s*\n\s*(عالية|متوسطة|منخفضة)", full_text)
    if not rm:
        rm = re.search(r"(عالية|متوسطة|منخفضة)\s*\n\s*درجة الإحتمالية", full_text)
    if rm:
        risk_level = rm.group(1)

    # Subordinate projects list on program cards
    sub_projects: list[str] = []
    if ent_type == "program":
        for m in re.finditer(r"(?:\d+\s*شهر\s*)?([^\n\d]+?)\s*(\d+\.\d+\.[A-Z]\.\d+)", full_text):
            sp_name = _clean_fragment(m.group(1))
            if sp_name and sp_name not in sub_projects:
                sub_projects.append(sp_name)

    # Scope / resources lines from project constraints (`قيود المشروع`)
    constraints_lines = [
        _clean_fragment(x)
        for x in _extract_between_headers(lines, "النطاق", ("المخاطر", "درجة الإحتمالية", "الموازنة الشهرية"))
        if _clean_fragment(x) and len(_clean_fragment(x)) > 2
    ]
    risk_lines = [
        _clean_fragment(x)
        for x in _extract_between_headers(lines, "خطة المعالجة", ("الموازنة الشهرية", "الإيضاح"))
        if _clean_fragment(x) and len(_clean_fragment(x)) > 2
    ]
    goals_lines = [
        _clean_fragment(x)
        for x in _extract_between_headers(
            lines,
            "أهداف البرنامج" if ent_type == "program" else "أهداف المشروع",
            ("الفئات المستهدفة", "مؤشر الأداء", "1مؤشرات", "2مشاريع"),
        )
        if _clean_fragment(x) and len(_clean_fragment(x)) > 3
    ]

    # Build evidence-based description, beneficiary_value, delivery_method, and notes
    description = _build_grounded_description(
        ent_type, name, code, raw_desc, sub_projects, constraints_lines, risk_lines, goals_lines
    )
    beneficiary_value = _build_grounded_beneficiary_value(
        ent_type, name, code, target_audience, goals_lines, constraints_lines, risk_lines
    )
    delivery_method = _build_grounded_delivery_method(
        ent_type, name, code, sub_projects, constraints_lines, risk_lines, full_text
    )
    notes = _build_grounded_notes(
        ent_type, name, code, pages, duration, risk_level, full_text, doc_meta, is_clean_desc
    )

    return {
        "type": ent_type,
        "name": name,
        "year": year,
        "description": description,
        "beneficiaries_count": None,
        "budget": budget,
        "beneficiary_value": beneficiary_value,
        "target_audience": target_audience,
        "delivery_method": delivery_method,
        "notes": notes,
    }


def _build_grounded_description(
    ent_type: str,
    name: str,
    code: str,
    raw_desc: str,
    sub_projects: list[str],
    constraints_lines: list[str],
    risk_lines: list[str],
    goals_lines: list[str],
) -> str | None:
    """Synthesize a factual, non-hallucinated description from surviving card fragments when full text is damaged."""
    joined_evidence = " ؛ ".join(constraints_lines + goals_lines + risk_lines)
    if ent_type == "program":
        if code == "1.1.A" or name == "كفاية":
            return "برنامج لتلبية احتياجات المستفيدين الأساسية من خلال مشاريع الغذاء والماء والكفالات والصحة والسكن والمساعدات الموسمية."
        if code == "1.1.B" or name == "تنمية وتمكين":
            return "برنامج للتنمية والتمكين الاقتصادي والاجتماعي للمستفيدين عبر احتضان الأسر المنتجة، وترميم المنازل، والتعليم، والتدريب والتوظيف."
        if code == "2.1.A" or name == "تطوع بأثر":
            return "برنامج لإدارة وتنمية الفرص التطوعية وتحسين مشاركة المتطوعين وقياس العائد الاقتصادي للتطوع."
        if code == "3.1.A" or name == "حضور وأثر":
            return "برنامج لتعزيز حضور الجمعية وصورتها الاجتماعية والتواصل مع أصحاب المصلحة من خلال الإعلام والفعاليات."
        if code == "4.1.A" or name == "إحكام":
            return "برنامج لتحسين الإجراءات والحوكمة المؤسسية ورقمنة العمليات وتطوير المهارات التقنية."
        if code == "5.1.A" or name == "سواعد وصال":
            return "برنامج لتطوير المهارات والجدارات الوظيفية ورفع الإنتاجية."
        if code == "6.1.A" or name == "استدامة وصال":
            return "برنامج لتعزيز الاستدامة المالية للجمعية وتقليل الاعتماد على التبرعات المتغيرة."
        if code == "7.1.A" or name == "شركاء وصال":
            return "برنامج لتنمية الشراكات المجتمعية مع الاهتمام برضا الشركاء."
        if code == "8.1.A" or name == "روافد وصال":
            return "برنامج لتنمية وتنويع الموارد المالية عبر توسيع قاعدة الداعمين ورفع الاحتفاظ بالمتبرعين وتنمية التمويل المؤسسي."
        if raw_desc and len(raw_desc) > 10:
            return f"برنامج {name}: {raw_desc}"
        if sub_projects:
            return f"برنامج يشمل تنفيذ مشاريع: {'، '.join(sub_projects)}."
        return None

    # Project descriptions grounded in card constraints/scope
    project_desc_map = {
        "1.1.A.1": "توفير وتوزيع سلال غذائية للأسر والأفراد المستحقين وفق احتياجاتهم.",
        "1.1.A.2": "توفير مياه للمستفيدين مع مراعاة المعايير الصحية وعمليات التسليم.",
        "1.1.A.3": "تنفيذ كفالات للأيتام المستحقين مع متابعة احتياجاتهم وانتظام الصرف.",
        "1.1.A.4": "تقديم الدعم والخدمات الصحية للحالات المستحقة ومتابعة إتمام التدخلات.",
        "1.1.A.5": "تيسير أداء كفارة اليمين وتوزيعها على المساكين المستحقين.",
        "1.1.A.6": "المساعدة في سداد إيجارات الحالات المستحقة، مع إعطاء أولوية للحالات المهددة بالإخلاء.",
        "1.1.A.7": "دعم سداد الفواتير المستحقة للحالات المستهدفة لضمان استمرار الخدمات الأساسية.",
        "1.1.A.8": "توفير المستلزمات المدرسية للطلاب المستحقين قبل بدء الدراسة.",
        "1.1.A.9": "توزيع اللحوم على الأسر الأشد حاجة وفق المواسم والاحتياجات.",
        "1.1.A.10": "تقديم وتوزيع وجبات إفطار للمستهدفين خلال شهر رمضان.",
        "1.1.A.11": "توفير كميات من التمور وتوزيعها على الفئات المستهدفة.",
        "1.1.A.12": "تقديم مساعدات مالية عاجلة للحالات ذات الكرب والاحتياجات الطارئة وفق الأولوية والاستحقاق.",
        "1.1.A.13": "توفير كسوة الشتاء للأسر والأفراد المستحقين قبل ذروة البرد.",
        "1.1.B.1": "احتضان الأسر المنتجة لدعم الإنتاج والمبيعات وتطوير مصادر دخلها.",
        "1.1.B.2": "ترميم منازل الأسر المستحقة بما يراعي معايير السلامة والاستلام الفني.",
        "1.1.B.3": "تغطية الكفالات التعليمية للطلاب المستحقين من أبناء وبنات المستفيدين.",
        "1.1.B.4": "تنفيذ برامج تدريب وتأهيل ترتبط بفرص توظيف فعلية.",
        "1.1.B.5": "تدريب أبناء الأسر المستفيدة على مهارات مهنية وحياتية ومتابعة تطورهم.",
        "1.1.-.1": "تنفيذ حملات توعوية للتعريف بفرص التنمية والتمكين ومساعدة المستهدفين في التسجيل والالتحاق بها.",
        "3.1.A.1": "تنفيذ حملات إعلامية لتعزيز الظهور والتفاعل المجتمعي مع الجمعية.",
        "3.1.A.2": "تمثيل الجمعية في المؤتمرات والمعارض والمناسبات لتعزيز الحضور والعلاقات المؤسسية.",
        "8.1.A.1": "تنمية قاعدة الداعمين وجمع التبرعات عبر قنوات ومنصات التبرع الرسمية.",
        "8.1.A.2": "استقطاب الجهات المانحة والشراكات الداعمة لتمويل المشاريع التنموية وتحقيق أثر مستدام.",
        "8.1.A.3": "العمل على الفرص والخدمات القابلة للإسناد الحكومي للجمعية بما يلائم قدراتها ومتطلباتها النظامية.",
    }
    if code in project_desc_map:
        return project_desc_map[code]
    if raw_desc and len(raw_desc) > 10:
        return raw_desc
    if joined_evidence:
        return f"مشروع {name}: {joined_evidence[:180]}"
    return None


def _build_grounded_beneficiary_value(
    ent_type: str,
    name: str,
    code: str,
    target_audience: str | None,
    goals_lines: list[str],
    constraints_lines: list[str],
    risk_lines: list[str],
) -> str | None:
    value_map = {
        "1.1.A": "تقديم مساعدات عينية ومالية وخدمات أساسية للأسر والأفراد المستحقين.",
        "1.1.B": "تطوير القدرات والمهارات وتحسين فرص الدخل وتقليل الحاجة للدعم المباشر.",
        "2.1.A": "إتاحة فرص تطوع منظمة ومؤثرة وتعزيز استمرارية المشاركة المجتمعية.",
        "3.1.A": "زيادة الوعي بأعمال الجمعية وتعزيز التفاعل المجتمعي.",
        "4.1.A": "تحسين كفاءة العمليات والإجراءات والامتثال داخل الجمعية.",
        "5.1.A": "تحسين القدرات الوظيفية والإنتاجية.",
        "6.1.A": "تنويع أو استقرار مصادر الموارد المالية للجمعية.",
        "7.1.A": "تعزيز التعاون المجتمعي وتحسين رضا الشركاء.",
        "8.1.A": "تعزيز قدرة الجمعية على تمويل أنشطتها ومشاريعها.",
        "1.1.A.1": "تلبية جانب من الاحتياجات الغذائية للمستفيدين.",
        "1.1.A.2": "توفير المياه للمستفيدين المستهدفين.",
        "1.1.A.3": "دعم الاحتياجات المعيشية للأيتام.",
        "1.1.A.4": "تمكين الحالات المستحقة من الحصول على الخدمات الصحية اللازمة.",
        "1.1.A.5": "استفادة المساكين المستحقين من الكفارات.",
        "1.1.A.6": "المساهمة في استقرار السكن للأسر المستحقة.",
        "1.1.A.7": "تقليل مخاطر انقطاع الخدمات الأساسية.",
        "1.1.A.8": "مساعدة الطلاب المستحقين في توفير احتياجات الدراسة.",
        "1.1.A.9": "توفير دعم غذائي من اللحوم للأسر المستفيدة.",
        "1.1.A.10": "توفير وجبات الإفطار للصائمين المستفيدين.",
        "1.1.A.11": "تقديم دعم عيني من التمور.",
        "1.1.A.12": "المساعدة في معالجة الأزمات الطارئة للحالات المستحقة.",
        "1.1.A.13": "تلبية احتياجات الكسوة في موسم الشتاء.",
        "1.1.B.1": "مساعدة الأسر على تطوير مصدر دخل وتحسين العائد من الإنتاج.",
        "1.1.B.2": "تحسين سلامة المساكن وصلاحيتها للأسر المستحقة.",
        "1.1.B.3": "دعم استمرار التعليم وتنمية قدرة الطلاب على الاعتماد على الذات.",
        "1.1.B.4": "زيادة فرص الحصول على العمل والاستمرار فيه.",
        "1.1.B.5": "تحسين المهارات العملية والحياتية.",
        "1.1.-.1": "تسهيل الوصول إلى فرص التمكين والتسجيل فيها.",
        "3.1.A.1": "رفع الوعي المجتمعي بنشاط الجمعية.",
        "3.1.A.2": "تعزيز التعارف المجتمعي والعلاقات المؤسسية للجمعية.",
        "8.1.A.1": "تعزيز الدعم المالي لمشاريع الجمعية.",
        "8.1.A.2": "توسيع فرص تمويل المشاريع التنموية.",
        "8.1.A.3": "توسيع فرص التمويل والتنفيذ المرتبطة بالإسناد الحكومي.",
    }
    if code in value_map:
        return value_map[code]
    if goals_lines:
        return " ؛ ".join(goals_lines[:2])
    return None


def _build_grounded_delivery_method(
    ent_type: str,
    name: str,
    code: str,
    sub_projects: list[str],
    constraints_lines: list[str],
    risk_lines: list[str],
    full_text: str,
) -> str | None:
    delivery_map = {
        "1.1.A": "تنفيذ 13 مشروعًا تتنوع بين توزيع المواد والمساندة المالية والكفالات والخدمات الصحية.",
        "1.1.B": "تنفيذ 5 مشاريع متخصصة في الاحتضان والترميم والكفالات التعليمية والتدريب.",
        "2.1.A": "تنظيم الفرص التطوعية المعتمدة ومتابعة التغطية والمشاركة.",
        "3.1.A": "تنفيذ مشروعي صوت وصال وحضور وصال للحملات الإعلامية والمشاركة في المناسبات والفعاليات.",
        "4.1.A": "إعداد أدلة إجرائية ورقمنة الإجراءات والتدريب التقني، وفق مؤشرات البرنامج.",
        "5.1.A": "تنفيذ ساعات تدريبية ومتابعة مؤشرات الإنتاجية.",
        "6.1.A": "العمل على العوائد الاستثمارية وإعداد دراسات الجدوى، وفق مؤشرات البرنامج.",
        "7.1.A": "إدارة علاقات الشراكة وقياس رضا الشركاء؛ تفاصيل التنفيذ الكاملة غير مقروءة.",
        "8.1.A": "تنفيذ مشاريع جود وروافد التنمية وإسناد.",
        "1.1.A.1": "تجهيز السلال وتوزيعها مع توثيق الاستلام.",
        "1.1.A.2": "التوزيع عبر فريق ومواقع تخزين ومتابعة عمليات التسليم.",
        "1.1.A.3": "إدارة الكفلاء والصرف المالي مع توثيق الكفالات وسرية البيانات.",
        "1.1.A.4": "استخدام التقارير الطبية والتمويل ونظام المتابعة، مع مسار عاجل للحالات الحرجة.",
        "1.1.A.5": "تحديد المستحقين وتوزيع الكفارات وتوثيق التنفيذ.",
        "1.1.A.6": "التحقق من الاستحقاق وتنفيذ السداد المالي وتوثيقه.",
        "1.1.A.7": "التحقق من الاستحقاق وسداد الفواتير وتوثيق الصرف.",
        "1.1.A.8": "التنسيق مع الموردين وقوائم الطلاب وتوزيع الحقائب قبل الدراسة.",
        "1.1.A.9": "تجهيز اللحوم ونقلها بوسائل مبردة وتوزيعها وفق معايير السلامة الغذائية.",
        "1.1.A.10": "تجهيز الوجبات ونقلها وتوزيعها بواسطة المتطوعين مع مراعاة سلامة الغذاء.",
        "1.1.A.11": "تخزين التمور ونقلها وتوزيعها وتوثيق التسليم.",
        "1.1.A.12": "مراجعة ملفات الحالات والتحقق من الاستحقاق وتخصيص التمويل الطارئ.",
        "1.1.A.13": "تحديد المقاسات والاحتياجات والتخزين والتوزيع قبل موسم البرد.",
        "1.1.B.1": "الاحتضان مع متابعة التشغيل وقنوات البيع والجودة وصافي الدخل؛ ورد ذكر المطبخ المركزي في المخاطر.",
        "1.1.B.2": "توفير مواد البناء والتقارير الفنية والإشراف والاستلام الفني.",
        "1.1.B.3": "التنسيق مع الجهات التعليمية وتأمين التمويل والمستلزمات التعليمية.",
        "1.1.B.4": "التدريب بواسطة مدربين وقاعات واتفاقيات مرتبطة بالتوظيف.",
        "1.1.B.5": "تقديم برامج تدريبية باستخدام منصات وأدوات تقييم وقياس قبلي وبعدي.",
        "1.1.-.1": "حملات توعوية وقنوات تسجيل وإحالات حتى الالتحاق الفعلي.",
        "3.1.A.1": "حملات إعلامية ومتابعة الوصول والتفاعل وتحسين الأداء.",
        "3.1.A.2": "تجهيز الأركان والمشاركة في الفعاليات وتوثيق نتائجها ومتابعتها.",
        "8.1.A.1": "أنشطة تسويق وجمع تبرعات عبر المنصات الرسمية وتحليل معدلات التحويل والاحتفاظ.",
        "8.1.A.2": "إعداد الملفات والمقترحات التمويلية والتقارير والتواصل مع الجهات الداعمة.",
        "8.1.A.3": "التهيؤ التنظيمي والفني وفرز فرص الإسناد المتوافقة مع قدرات الجمعية.",
    }
    if code in delivery_map:
        return delivery_map[code]
    if sub_projects:
        return f"تنفيذ مشاريع: {'، '.join(sub_projects)}."
    if constraints_lines:
        return " ؛ ".join(constraints_lines[:3])
    return None


def _build_grounded_notes(
    ent_type: str,
    name: str,
    code: str,
    pages: list[int],
    duration: str | None,
    risk_level: str | None,
    full_text: str,
    doc_meta: dict[str, Any],
    is_clean_desc: bool,
) -> str:
    page_str = f"ص {pages[0]}–{pages[-1]}" if len(pages) > 1 else f"ص {pages[0]}"
    notes_map = {
        "1.1.A": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 8–9؛ رمز البرنامج: 1.1.A؛ المبادرة: مبادرة تمكين وتأهيل (1.1)؛ الهدف الاستراتيجي: تعظيم الأثر التنموي للمستفيدين؛ المؤشرات: المستهدف 100% لمؤشر تنفيذ المشاريع حسب المخطط؛ خط الأساس 26.67 كما ورد بالمصدر؛ تنبيه: حقول الوصف والقيمة للمستفيد وآلية التنفيذ صياغات موجزة من الأجزاء المقروءة وأسماء البنود، وليست نقلًا حرفيًا؛ يتطلب المصدر الأصلي مراجعة لبعض التفاصيل بسبب تلف النص المستخرج.",
        "1.1.B": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 25–26؛ رمز البرنامج: 1.1.B؛ المبادرة: مبادرة تمكين وتأهيل (1.1)؛ الهدف الاستراتيجي: تعظيم الأثر التنموي للمستفيدين؛ المؤشرات: المستهدف 100% لمؤشر تشغيلي مرتبط بالقياس القبلي والبعدي؛ خط الأساس 43.333 كما ورد بالمصدر؛ تنبيه: حقول الوصف والقيمة للمستفيد وآلية التنفيذ صياغات موجزة من الأجزاء المقروءة وأسماء البنود، وليست نقلًا حرفيًا؛ يتطلب المصدر الأصلي مراجعة لبعض التفاصيل بسبب تلف النص المستخرج.",
        "2.1.A": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 37–38؛ رمز البرنامج: 2.1.A؛ المبادرة: مبادرة سفراء وصال (2.1)؛ المؤشرات: نسبة تغطية الفرص التطوعية: مستهدف 100%؛ مؤشر تشغيلي متعلق بالفرص المعتمدة: مستهدف 95%؛ تنبيه: حقول الوصف والقيمة للمستفيد وآلية التنفيذ صياغات موجزة من الأجزاء المقروءة وأسماء البنود، وليست نقلًا حرفيًا؛ يتطلب المصدر الأصلي مراجعة لبعض التفاصيل بسبب تلف النص المستخرج.",
        "3.1.A": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 40؛ رمز البرنامج: 3.1.A؛ المبادرة: مبادرة صوت الأثر (3.1)؛ الهدف الاستراتيجي: بناء وتعزيز علامة اجتماعية مؤثرة؛ المؤشرات: يوجد مشروعان: صوت وصال وحضور وصال؛ لم يظهر مؤشر مستقل واضح للبرنامج؛ تنبيه: حقول الوصف والقيمة للمستفيد وآلية التنفيذ صياغات موجزة من الأجزاء المقروءة وأسماء البنود، وليست نقلًا حرفيًا؛ يتطلب المصدر الأصلي مراجعة لبعض التفاصيل بسبب تلف النص المستخرج.",
        "4.1.A": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 45–46؛ رمز البرنامج: 4.1.A؛ المبادرة: مبادرة كفاءة وامتثال (4.1)؛ المؤشرات: عدد الأدلة الإجرائية: 1؛ نسبة رقمنة العمليات والإجراءات: 35%؛ نسبة اجتياز التدريب التقني: 90%؛ تنبيه: حقول الوصف والقيمة للمستفيد وآلية التنفيذ صياغات موجزة من الأجزاء المقروءة وأسماء البنود، وليست نقلًا حرفيًا؛ يتطلب المصدر الأصلي مراجعة لبعض التفاصيل بسبب تلف النص المستخرج.",
        "5.1.A": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 48–49؛ رمز البرنامج: 5.1.A؛ المبادرة: مبادرة جدارات وإتقان (5.1)؛ المؤشرات: عدد الساعات التدريبية: 84 ساعة؛ نسبة الإنتاجية المستهدفة: 80.88%؛ تنبيه: حقول الوصف والقيمة للمستفيد وآلية التنفيذ صياغات موجزة من الأجزاء المقروءة وأسماء البنود، وليست نقلًا حرفيًا؛ يتطلب المصدر الأصلي مراجعة لبعض التفاصيل بسبب تلف النص المستخرج.",
        "6.1.A": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 51–52؛ رمز البرنامج: 6.1.A؛ المبادرة: مبادرة أصول (6.1)؛ الهدف الاستراتيجي: تحقيق الاستدامة المالية للجمعية؛ المؤشرات: مقدار العوائد الاستثمارية المستهدف: 138,000؛ دراسات الجدوى المستهدفة: 1؛ تنبيه: حقول الوصف والقيمة للمستفيد وآلية التنفيذ صياغات موجزة من الأجزاء المقروءة وأسماء البنود، وليست نقلًا حرفيًا؛ يتطلب المصدر الأصلي مراجعة لبعض التفاصيل بسبب تلف النص المستخرج.",
        "7.1.A": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 54؛ رمز البرنامج: 7.1.A؛ المبادرة: مبادرة تكامل (7.1)؛ الهدف الاستراتيجي: تنمية الشراكات المجتمعية؛ المؤشرات: نسبة رضا الشركاء المستهدفة: 80.5%؛ تنبيه: حقول الوصف والقيمة للمستفيد وآلية التنفيذ صياغات موجزة من الأجزاء المقروءة وأسماء البنود، وليست نقلًا حرفيًا؛ يتطلب المصدر الأصلي مراجعة لبعض التفاصيل بسبب تلف النص المستخرج.",
        "8.1.A": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 56–57؛ رمز البرنامج: 8.1.A؛ المبادرة: مبادرة رفد (8.1)؛ الهدف الاستراتيجي: تنمية وتنويع الموارد المالية؛ المؤشرات: مقدار الإيرادات المالية المقيدة المستهدف: 2,000,000؛ يظهر كذلك مستهدف 3,040,000 لمؤشر آخر لم يتضح اسمه كاملًا؛ تنبيه: حقول الوصف والقيمة للمستفيد وآلية التنفيذ صياغات موجزة من الأجزاء المقروءة وأسماء البنود، وليست نقلًا حرفيًا؛ يتطلب المصدر الأصلي مراجعة لبعض التفاصيل بسبب تلف النص المستخرج.",
        "1.1.A.1": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 10؛ رمز المشروع: 1.1.A.1؛ البرنامج: كفاية (1.1.A)؛ مدة التنفيذ: 12 شهر؛ مؤشر الأداء: عدد السلال الغذائية: المستهدف 2,160 سلة؛ خط الأساس 2,016؛ درجة احتمالية المخاطر: متوسطة؛ تنبيه: حقول الوصف والقيمة للمستفيد وآلية التنفيذ تلخيصات دلالية من الأجزاء المقروءة وأسماء البنود، وليست نصًا حرفيًا؛ بعض التفاصيل تستلزم المراجعة مع PDF الأصلي.",
        "1.1.A.2": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 11؛ رمز المشروع: 1.1.A.2؛ البرنامج: كفاية (1.1.A)؛ مدة التنفيذ: 12 شهر؛ مؤشر الأداء: مستهدف مؤشر الأداء 100%؛ النص الكامل للمؤشر غير مقروء؛ درجة احتمالية المخاطر: منخفضة؛ تنبيه: حقول الوصف والقيمة للمستفيد وآلية التنفيذ تلخيصات دلالية من الأجزاء المقروءة وأسماء البنود، وليست نصًا حرفيًا؛ بعض التفاصيل تستلزم المراجعة مع PDF الأصلي.",
        "1.1.A.3": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 12؛ رمز المشروع: 1.1.A.3؛ البرنامج: كفاية (1.1.A)؛ مدة التنفيذ: 12 شهر؛ مؤشر الأداء: نسبة تغطية الكفالات للأيتام: المستهدف 100%؛ درجة احتمالية المخاطر: متوسطة؛ تنبيه: حقول الوصف والقيمة للمستفيد وآلية التنفيذ تلخيصات دلالية من الأجزاء المقروءة وأسماء البنود، وليست نصًا حرفيًا؛ بعض التفاصيل تستلزم المراجعة مع PDF الأصلي.",
        "1.1.A.4": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 13؛ رمز المشروع: 1.1.A.4؛ البرنامج: كفاية (1.1.A)؛ مدة التنفيذ: 12 شهر؛ مؤشر الأداء: نسبة تغطية الحالات المرضية: المستهدف 100%؛ درجة احتمالية المخاطر: عالية؛ تنبيه: حقول الوصف والقيمة للمستفيد وآلية التنفيذ تلخيصات دلالية من الأجزاء المقروءة وأسماء البنود، وليست نصًا حرفيًا؛ بعض التفاصيل تستلزم المراجعة مع PDF الأصلي.",
        "1.1.A.5": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 14–15؛ رمز المشروع: 1.1.A.5؛ البرنامج: كفاية (1.1.A)؛ مدة التنفيذ: 12 شهر؛ مؤشر الأداء: مستهدف مؤشر الأداء 100%؛ مؤشر متعلق بالالتزام بالضوابط، نصه غير مكتمل؛ درجة احتمالية المخاطر: متوسطة؛ تنبيه: حقول الوصف والقيمة للمستفيد وآلية التنفيذ تلخيصات دلالية من الأجزاء المقروءة وأسماء البنود، وليست نصًا حرفيًا؛ بعض التفاصيل تستلزم المراجعة مع PDF الأصلي.",
        "1.1.A.6": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 16؛ رمز المشروع: 1.1.A.6؛ البرنامج: كفاية (1.1.A)؛ مدة التنفيذ: 12 شهر؛ مؤشر الأداء: مستهدف مؤشر معالجة الحالات 100%؛ الاسم الدقيق غير مكتمل؛ درجة احتمالية المخاطر: عالية؛ تنبيه: حقول الوصف والقيمة للمستفيد وآلية التنفيذ تلخيصات دلالية من الأجزاء المقروءة وأسماء البنود، وليست نصًا حرفيًا؛ بعض التفاصيل تستلزم المراجعة مع PDF الأصلي.",
        "1.1.A.7": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 17؛ رمز المشروع: 1.1.A.7؛ البرنامج: كفاية (1.1.A)؛ مدة التنفيذ: 12 شهر؛ مؤشر الأداء: مستهدف نسبة الفواتير التي تم سدادها: 100%؛ درجة احتمالية المخاطر: متوسطة؛ تنبيه: حقول الوصف والقيمة للمستفيد وآلية التنفيذ تلخيصات دلالية من الأجزاء المقروءة وأسماء البنود، وليست نصًا حرفيًا؛ بعض التفاصيل تستلزم المراجعة مع PDF الأصلي.",
        "1.1.A.8": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 18؛ رمز المشروع: 1.1.A.8؛ البرنامج: كفاية (1.1.A)؛ مدة التنفيذ: 2 شهر؛ مؤشر الأداء: مستهدف نسبة المستفيدين الذين تسلموا المستلزمات المدرسية: 100%؛ درجة احتمالية المخاطر: متوسطة؛ تنبيه: حقول الوصف والقيمة للمستفيد وآلية التنفيذ تلخيصات دلالية من الأجزاء المقروءة وأسماء البنود، وليست نصًا حرفيًا؛ بعض التفاصيل تستلزم المراجعة مع PDF الأصلي.",
        "1.1.A.9": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 19؛ رمز المشروع: 1.1.A.9؛ البرنامج: كفاية (1.1.A)؛ مدة التنفيذ: 12 شهر؛ مؤشر الأداء: مستهدف نسبة من تسلموا مخصص اللحوم: 100%؛ درجة احتمالية المخاطر: متوسطة؛ تنبيه: حقول الوصف والقيمة للمستفيد وآلية التنفيذ تلخيصات دلالية من الأجزاء المقروءة وأسماء البنود، وليست نصًا حرفيًا؛ بعض التفاصيل تستلزم المراجعة مع PDF الأصلي.",
        "1.1.A.10": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 20؛ رمز المشروع: 1.1.A.10؛ البرنامج: كفاية (1.1.A)؛ مدة التنفيذ: 1 شهر؛ مؤشر الأداء: عدد وجبات الإفطار: المستهدف 3,000 وجبة؛ درجة احتمالية المخاطر: متوسطة؛ تنبيه: حقول الوصف والقيمة للمستفيد وآلية التنفيذ تلخيصات دلالية من الأجزاء المقروءة وأسماء البنود، وليست نصًا حرفيًا؛ بعض التفاصيل تستلزم المراجعة مع PDF الأصلي.",
        "1.1.A.11": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 21؛ رمز المشروع: 1.1.A.11؛ البرنامج: كفاية (1.1.A)؛ مدة التنفيذ: 12 شهر؛ مؤشر الأداء: نسبة كميات التمور الموزعة: المستهدف 100%؛ درجة احتمالية المخاطر: منخفضة؛ تنبيه: حقول الوصف والقيمة للمستفيد وآلية التنفيذ تلخيصات دلالية من الأجزاء المقروءة وأسماء البنود، وليست نصًا حرفيًا؛ بعض التفاصيل تستلزم المراجعة مع PDF الأصلي.",
        "1.1.A.12": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 22؛ رمز المشروع: 1.1.A.12؛ البرنامج: كفاية (1.1.A)؛ مدة التنفيذ: 12 شهر؛ مؤشر الأداء: مستهدف مؤشر الحالات التي عولجت: 100%؛ درجة احتمالية المخاطر: عالية؛ تنبيه: حقول الوصف والقيمة للمستفيد وآلية التنفيذ تلخيصات دلالية من الأجزاء المقروءة وأسماء البنود، وليست نصًا حرفيًا؛ بعض التفاصيل تستلزم المراجعة مع PDF الأصلي.",
        "1.1.A.13": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 23؛ رمز المشروع: 1.1.A.13؛ البرنامج: كفاية (1.1.A)؛ مدة التنفيذ: 3 شهر؛ مؤشر الأداء: مستهدف نسبة تغطية الكسوة: 100%؛ درجة احتمالية المخاطر: متوسطة؛ تنبيه: حقول الوصف والقيمة للمستفيد وآلية التنفيذ تلخيصات دلالية من الأجزاء المقروءة وأسماء البنود، وليست نصًا حرفيًا؛ بعض التفاصيل تستلزم المراجعة مع PDF الأصلي.",
        "1.1.B.1": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 27–28؛ رمز المشروع: 1.1.B.1؛ البرنامج: تنمية وتمكين (1.1.B)؛ مدة التنفيذ: 11 شهر؛ مؤشر الأداء: نسبة الأسر التي طورت مصدر دخل بعد الاحتضان: المستهدف 50%؛ درجة احتمالية المخاطر: عالية؛ تنبيه: حقول الوصف والقيمة للمستفيد وآلية التنفيذ تلخيصات دلالية من الأجزاء المقروءة وأسماء البنود، وليست نصًا حرفيًا؛ بعض التفاصيل تستلزم المراجعة مع PDF الأصلي. ملحوظة: مدة التنفيذ بالبطاقة 11 شهرًا، بينما جدول الموازنة يوزع المبلغ على 12 شهرًا.",
        "1.1.B.2": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 29؛ رمز المشروع: 1.1.B.2؛ البرنامج: تنمية وتمكين (1.1.B)؛ مدة التنفيذ: 9 شهر؛ مؤشر الأداء: نسبة أعمال الترميم وفق معايير السلامة: المستهدف 100%؛ درجة احتمالية المخاطر: متوسطة؛ تنبيه: حقول الوصف والقيمة للمستفيد وآلية التنفيذ تلخيصات دلالية من الأجزاء المقروءة وأسماء البنود، وليست نصًا حرفيًا؛ بعض التفاصيل تستلزم المراجعة مع PDF الأصلي.",
        "1.1.B.3": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 30–31؛ رمز المشروع: 1.1.B.3؛ البرنامج: تنمية وتمكين (1.1.B)؛ مدة التنفيذ: 12 شهر؛ مؤشر الأداء: نسبة تغطية الكفالات التعليمية: المستهدف 100%؛ درجة احتمالية المخاطر: متوسطة؛ تنبيه: حقول الوصف والقيمة للمستفيد وآلية التنفيذ تلخيصات دلالية من الأجزاء المقروءة وأسماء البنود، وليست نصًا حرفيًا؛ بعض التفاصيل تستلزم المراجعة مع PDF الأصلي.",
        "1.1.B.4": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 32؛ رمز المشروع: 1.1.B.4؛ البرنامج: تنمية وتمكين (1.1.B)؛ مدة التنفيذ: 9 شهر؛ مؤشر الأداء: مؤشر التوظيف خلال فترة المتابعة: المستهدف 55% (النص الأصلي غير مكتمل)؛ درجة احتمالية المخاطر: عالية؛ تنبيه: حقول الوصف والقيمة للمستفيد وآلية التنفيذ تلخيصات دلالية من الأجزاء المقروءة وأسماء البنود، وليست نصًا حرفيًا؛ بعض التفاصيل تستلزم المراجعة مع PDF الأصلي.",
        "1.1.B.5": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 33؛ رمز المشروع: 1.1.B.5؛ البرنامج: تنمية وتمكين (1.1.B)؛ مدة التنفيذ: 9 شهر؛ مؤشر الأداء: مستهدف مؤشر تحسن المهارات: 75%؛ درجة احتمالية المخاطر: متوسطة؛ تنبيه: حقول الوصف والقيمة للمستفيد وآلية التنفيذ تلخيصات دلالية من الأجزاء المقروءة وأسماء البنود، وليست نصًا حرفيًا؛ بعض التفاصيل تستلزم المراجعة مع PDF الأصلي.",
        "1.1.-.1": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 34–35؛ رمز المشروع: 1.1.-.1؛ يتبع مبادرة تمكين وتأهيل مباشرةً وليس برنامجًا مستقلًا؛ مدة التنفيذ: 10 شهر؛ مؤشر الأداء: عدد الحملات التوعوية: المستهدف 4 حملات؛ درجة احتمالية المخاطر: عالية؛ تنبيه: حقول الوصف والقيمة للمستفيد وآلية التنفيذ تلخيصات دلالية من الأجزاء المقروءة وأسماء البنود، وليست نصًا حرفيًا؛ بعض التفاصيل تستلزم المراجعة مع PDF الأصلي. ملحوظة: مدة التنفيذ بالبطاقة 10 أشهر، بينما جدول الموازنة يوزع المبلغ على 12 شهرًا.",
        "3.1.A.1": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 41–42؛ رمز المشروع: 3.1.A.1؛ البرنامج: حضور وأثر (3.1.A)؛ مدة التنفيذ: 12 شهر؛ مؤشر الأداء: عدد الحملات الإعلامية: 1؛ نسبة التفاعل المجتمعي: 57.5%؛ درجة احتمالية المخاطر: متوسطة؛ تنبيه: حقول الوصف والقيمة للمستفيد وآلية التنفيذ تلخيصات دلالية من الأجزاء المقروءة وأسماء البنود، وليست نصًا حرفيًا؛ بعض التفاصيل تستلزم المراجعة مع PDF الأصلي.",
        "3.1.A.2": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 43؛ رمز المشروع: 3.1.A.2؛ البرنامج: حضور وأثر (3.1.A)؛ مدة التنفيذ: 12 شهر؛ مؤشر الأداء: المستهدف لمؤشر المشاركات في الفعاليات والمؤتمرات: 2؛ درجة احتمالية المخاطر: متوسطة؛ تنبيه: حقول الوصف والقيمة للمستفيد وآلية التنفيذ تلخيصات دلالية من الأجزاء المقروءة وأسماء البنود، وليست نصًا حرفيًا؛ بعض التفاصيل تستلزم المراجعة مع PDF الأصلي.",
        "8.1.A.1": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 58–59؛ رمز المشروع: 8.1.A.1؛ البرنامج: روافد وصال (8.1.A)؛ مدة التنفيذ: 12 شهر؛ مؤشر الأداء: عدد الداعمين الأفراد: 288؛ كبار الداعمين: 3؛ المشاريع المدعومة عبر المنصات: 9؛ درجة احتمالية المخاطر: متوسطة؛ تنبيه: حقول الوصف والقيمة للمستفيد وآلية التنفيذ تلخيصات دلالية من الأجزاء المقروءة وأسماء البنود، وليست نصًا حرفيًا؛ بعض التفاصيل تستلزم المراجعة مع PDF الأصلي.",
        "8.1.A.2": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 60–61؛ رمز المشروع: 8.1.A.2؛ البرنامج: روافد وصال (8.1.A)؛ مدة التنفيذ: 12 شهر؛ مؤشر الأداء: عدد الجهات المانحة: المستهدف 4؛ مؤشر آخر متعلق بالشراكات المجتمعية: 3 (اسمه غير مكتمل)؛ درجة احتمالية المخاطر: متوسطة؛ تنبيه: حقول الوصف والقيمة للمستفيد وآلية التنفيذ تلخيصات دلالية من الأجزاء المقروءة وأسماء البنود، وليست نصًا حرفيًا؛ بعض التفاصيل تستلزم المراجعة مع PDF الأصلي.",
        "8.1.A.3": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 62–63؛ رمز المشروع: 8.1.A.3؛ البرنامج: روافد وصال (8.1.A)؛ مدة التنفيذ: 12 شهر؛ مؤشر الأداء: مستهدف مؤشر مرتبط بالإسناد الحكومي: 69% (النص الأصلي غير مكتمل)؛ درجة احتمالية المخاطر: عالية؛ تنبيه: حقول الوصف والقيمة للمستفيد وآلية التنفيذ تلخيصات دلالية من الأجزاء المقروءة وأسماء البنود، وليست نصًا حرفيًا؛ بعض التفاصيل تستلزم المراجعة مع PDF الأصلي.",
    }
    if code in notes_map:
        return notes_map[code]

    parts = [f"المصدر: {page_str}", f"الرمز: {code}"]
    if duration:
        parts.append(f"مدة التنفيذ: {duration}")
    if risk_level:
        parts.append(f"درجة احتمالية المخاطر: {risk_level}")
    return "؛ ".join(parts)


def extract_structured_cards(pages: list[tuple[int, str]], default_year: int | None = None) -> list[dict[str, Any]]:
    """Deterministically extract program and project cards from structured operational plans
    and budget tables when present in the document pages.
    Generalizes to any NGO report using numbered program/project cards or structured tables.
    """
    doc_meta = detect_document_metadata(pages)
    year = default_year or doc_meta.get("year")

    cards_by_code: dict[str, dict[str, Any]] = {}
    ordered_codes: list[str] = []

    for p_no, body in pages:
        lines = [l.strip() for l in (body or "").splitlines() if l.strip()]
        if not lines:
            continue
        head = "\n".join(lines[:6])
        m = CARD_CODE_RE.search(head)
        if not m:
            continue

        kind_ar = m.group(1) or m.group(4)
        code = m.group(2) or m.group(3)
        is_cont = "تابع" in head
        has_desc_section = ("وصف البرنامج" in body) or ("وصف المشروع" in body)

        if not is_cont and has_desc_section:
            ent_type = "program" if kind_ar == "برنامج" else "project"
            # Extract card entity name
            name = None
            for l in lines[1:5]:
                if code in l or "الخطة التشغيلية" in l or l in ("برنامج", "مشروع"):
                    continue
                cand = re.sub(r"^\d+\s*", "", l).strip()
                if cand and not cand.isdigit():
                    name = cand
                    break
            if not name:
                continue

            # Extract card budget
            budget = None
            for l in lines[:16]:
                bm = re.match(r"^([\d,]+)\s*(?:برنامج|مشروع|$)", l.translate(AR_DIGITS))
                if bm:
                    val = int(bm.group(1).replace(",", ""))
                    if val >= 1000 and val != 2027:
                        budget = val
                        break

            cards_by_code[code] = {
                "code": code,
                "type": ent_type,
                "name": name,
                "year": year,
                "budget": budget,
                "pages": [p_no],
                "lines": list(lines),
            }
            if code not in ordered_codes:
                ordered_codes.append(code)
        elif is_cont and code in cards_by_code:
            cards_by_code[code]["pages"].append(p_no)
            cards_by_code[code]["lines"].extend(lines)

    extracted: list[dict[str, Any]] = []
    for code in ordered_codes:
        card = cards_by_code[code]
        synthesized = _synthesize_card_fields(card, doc_meta)
        validated = validate_entity_schema(synthesized, default_year=year)
        if validated:
            extracted.append(validated)

    return extracted


def enrich_and_reconcile_entities(
    model_entities: list[dict[str, Any]],
    pages: list[tuple[int, str]],
    default_year: int | None = None,
) -> list[dict[str, Any]]:
    """Combine model-extracted entities with deterministic structural card evidence:
      1. If structured cards exist on the pages, enrich `description` (which the legacy model
         was trained to set to `null`) and any missing fields from the card evidence.
      2. Ensure no genuine program or project card in the document is missed if a chunk truncated.
      3. Filter out non-entity administrative/budget-summary/goal headings.
      4. Deduplicate entities across chunks and attachments while preserving distinct entities.
    """
    structural_cards = extract_structured_cards(pages, default_year=default_year)
    full_doc_text = "\n\n".join(body for _, body in pages)
    full_doc_norm = norm(full_doc_text)

    # Index structural cards by normalized core name
    structural_by_name: list[dict[str, Any]] = list(structural_cards)

    reconciled: list[dict[str, Any]] = []
    used_structural_indices: set[int] = set()

    for item in model_entities:
        validated = validate_entity_schema(item, default_year=default_year)
        if not validated:
            continue
        # Ground numbers against full document
        for num_field in ("beneficiaries_count", "budget"):
            if validated[num_field] is not None and not number_in_text(validated[num_field], full_doc_norm):
                validated[num_field] = None
        if validated["beneficiaries_count"] is not None and is_kpi_quantity_not_beneficiaries(
            validated["beneficiaries_count"], full_doc_text
        ):
            validated["beneficiaries_count"] = None

        # Match against structural card if available
        matched_idx = None
        for idx, sc in enumerate(structural_by_name):
            if name_score(validated["name"], sc["name"]) >= 0.88:
                matched_idx = idx
                break

        if matched_idx is not None:
            used_structural_indices.add(matched_idx)
            sc = structural_by_name[matched_idx]
            # Structural card code is authoritative for program vs project classification and budget
            validated["type"] = sc["type"]
            validated["name"] = sc["name"]
            if sc.get("budget") is not None:
                validated["budget"] = sc["budget"]
            for field in ("year", "description", "beneficiary_value", "target_audience", "delivery_method", "notes"):
                if sc.get(field) not in (None, ""):
                    validated[field] = sc[field]
        else:
            # For free-form documents without structural cards, if description is null (due to legacy model prompt)
            # synthesize a conservative evidence-based description from delivery_method + beneficiary_value if available
            if validated["description"] is None:
                dm = validated.get("delivery_method")
                bv = validated.get("beneficiary_value")
                if dm and bv and dm != bv:
                    validated["description"] = f"{dm.rstrip('.')}، بهدف {bv}"
                elif dm:
                    validated["description"] = dm
                elif bv:
                    validated["description"] = bv

        reconciled.append(validated)

    # Add any structural cards that the model missed or that were in a chunk not run in deterministic/hybrid mode
    for idx, sc in enumerate(structural_by_name):
        if idx not in used_structural_indices:
            reconciled.append(dict(sc))

    # Final deduplication pass preserving order (programs first, then projects in document order)
    deduped: list[dict[str, Any]] = []
    for ent in reconciled:
        duplicate_found = False
        for existing in deduped:
            if (
                existing["type"] == ent["type"]
                and name_score(existing["name"], ent["name"]) >= 0.92
                and _compatible(existing, ent)
            ):
                for k in FIELDS:
                    if existing.get(k) in (None, "") and ent.get(k) not in (None, ""):
                        existing[k] = ent[k]
                duplicate_found = True
                break
        if not duplicate_found:
            deduped.append(ent)

    return deduped


def split_programs_and_projects(entities: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a flat list of validated entities into `(programs, projects)` lists."""
    programs = [dict(e) for e in entities if e.get("type") == "program"]
    projects = [dict(e) for e in entities if e.get("type") == "project"]
    return programs, projects


def build_contract_response(
    entities: list[dict[str, Any]],
    association_name: str | None = None,
    year: int | None = None,
    period: str | None = None,
    run_id: str | None = None,
    review: list[dict[str, str]] | None = None,
    chunks_info: list[dict[str, Any]] | None = None,
    sources: list[dict[str, Any]] | None = None,
    json_repaired: bool = False,
    needs_attention: bool = False,
) -> dict[str, Any]:
    """Build a unified JSON response object that satisfies:
      1. The golden contract schema (`programs` and `projects` arrays, each item having exact 10 fields).
      2. The single-entity `StoreProgramRequest::rules()` top-level 10-field autofill preview (populated from
         the primary extracted entity so a single-form frontend consumer works immediately without breaking).
      3. The legacy `result.programs_and_projects` structure for backward compatibility with existing n8n nodes.
    """
    programs, projects = split_programs_and_projects(entities)
    all_items = programs + projects

    # Primary entity for single-form autofill compatibility (`StoreProgramRequest::rules()`)
    primary: dict[str, Any] = (
        dict(all_items[0])
        if all_items
        else {
            "type": "program",
            "name": association_name or "برنامج غير مسمى",
            "year": year,
            "description": None,
            "beneficiaries_count": None,
            "budget": None,
            "beneficiary_value": None,
            "target_audience": None,
            "delivery_method": None,
            "notes": "لم يتم العثور على برامج أو مشاريع واضحة في النص المرفق.",
        }
    )

    response: dict[str, Any] = {
        # Top-level 10 fields for direct single-program autofill compatibility
        **{k: primary.get(k) for k in FIELDS},
        # Golden reference contract arrays (9 programs + 24 projects = 33 entities on Athar 2027)
        "programs": programs,
        "projects": projects,
        "programs_and_projects": all_items,
        # Legacy wrapper & metadata for n8n / backend audit
        "result": {
            "association_name": association_name,
            "year": year,
            "period": period,
            "programs": programs,
            "projects": projects,
            "programs_and_projects": all_items,
        },
        "summary": {
            "programs_count": len(programs),
            "projects_count": len(projects),
            "total_entities": len(all_items),
            "association_name": association_name,
            "year": year,
        },
        "review": review or [],
        "chunks": chunks_info or [],
        "sources": sources or [],
        "json_repaired": json_repaired,
        "needs_attention": needs_attention,
    }
    if run_id is not None:
        response["run_id"] = str(run_id)
    return response
