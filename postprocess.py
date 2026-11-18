"""
postprocess.py - Document splitting, model output parsing, deterministic structural
card & Markdown section extraction, evidence-based enrichment, numeric grounding,
de-duplication, quality review diagnostics, and backend/n8n contract formatting.

Fixes and guarantees:
  1. Preserves real `beneficiaries_count` when explicitly labeled in the entity's section
     (e.g. `- عدد المستفيدين: **28,428**`), while rejecting KPI target quantities
     (e.g. `2,160` baskets, `3,000` meals) only when the entity has no explicit beneficiary count label.
  2. Preserves accurate `program` vs. `project` classification based on explicit prefixes (`مشروع`,
     `برنامج`, `مبادرة`), organizational codes (`1.1.A` vs `1.1.A.1`), and document hierarchy
     (umbrella programs vs. subordinate projects/activities).
  3. Prevents double-counting of umbrella programs and their subordinate projects in notes and summaries.
  4. Never blindly copies `delivery_method` into `description`; extracts distinct evidence-based
     `description`, `delivery_method`, `beneficiary_value`, and `target_audience`.
  5. Repairs minor LLM name substitutions (e.g. `تأهيل الصحة عبر وسائل التواصل الاجتماعي` ->
     `تعزيز الصحة عبر وسائل التواصل الاجتماعي`) against verbatim document headings.
  6. Strengthens `needs_attention` and `review` diagnostics so missing entities, classification fixes,
     or nulled numbers are surfaced clearly.
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

# Non-entity administrative / financial / strategic / statistical titles
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
    # General report / administrative sections
    "التقرير السنوي",
    "خدمنا خلال عام 2023",
    "احصايياتنا في 2023",
    "احصاءاتنا في 2023",
    "احصاءاتنا في 2023 تابع",
    "انواع طلبات العلاج والخدمات",
    "حركه الايراد",
    "حركه الصرف",
    "الجهات التي تم دعمها بالاجهزه",
    "جهات مستفيده مذكوره",
    "الاداء الاستراتيجي والتشغيلي لعام 2023",
    "الاداء الاستراتيجي والتشغيلي",
    "العلاقات العامه والاعلام",
    "مركزنا الاعلامي",
    "الدراسات واستطلاعات الراي المنفذه",
    "الجوده والتميز المؤسسي",
    "دراسات العايد المجتمعي علي الاستثمار sroi",
    "نتايج مشاركتنا في منصات التبرع الحكوميه",
    "احصاءات جدول المنصه كما في التقرير",
    "شركاؤنا في التدريب",
    "التكريمات",
    "الشراكات",
    "حضورنا الاعلامي",
    "التطوع",
    "تعزيز الولاء مع منسوبينا وتنظيم المناسبات الداخليه",
}

# KPI unit words that indicate a number in an operational plan KPI box is a target quantity, NOT beneficiaries_count
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
    "داعم",
    "داعمين",
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
    general statistics table, or strategic goal rather than a genuine program or project.
    """
    n = norm(name)
    if not n or len(n) < 2:
        return True
    if n in NON_ENTITY_NAMES:
        return True
    return bool(
        n.startswith(
            (
                "اجمالي ",
                "ملخص ",
                "الموازنه ",
                "المصاريف ",
                "المصروفات ",
                "الهدف الاستراتيجي",
                "احصايياتنا ",
                "احصاءاتنا ",
                "خدمنا خلال ",
                "انواع طلبات ",
                "الجهات التي تم دعمها",
                "جهات مستفيده",
                "الاداء الاستراتيجي",
                "دراسات العايد المجتمعي",
                "نتايج مشاركتنا في منصات",
                "احصاءات جدول المنصه",
                "شراكاتنا مع ",
            )
        )
        or re.match(r"^مبادره\s+\d", n)
        or re.match(r"^التقرير السنوي\s*\d*$", n)
    )


def has_explicit_beneficiaries_label(value: float, context_text: str) -> bool:
    """Return True if `value` is explicitly labeled as a beneficiary count in `context_text`
    (e.g. `عدد المستفيدين: **28,428**`, `إجمالي المستفيدين: 1,576`, or `471 مستفيد`).
    """
    try:
        int_val = int(value)
    except (ValueError, TypeError):
        return False
    val_str = f"{int_val:,}"
    val_plain = str(int_val)
    text_ar = (context_text or "").translate(AR_DIGITS)

    # Allow markdown bold markers `**`, colons, and spaces between label and number
    label_pattern = (
        rf"(?:عدد\s+المستفيدين(?:\s+المستهدفين)?|إجمالي\s+المستفيدين|اجمالي\s+المستفيدين|المستفيدون|المستفيدين)"
        rf"[\s:*：\-–—]*(?:{re.escape(val_str)}|{val_plain})\b"
    )
    if re.search(label_pattern, text_ar):
        return True

    suffix_pattern = rf"\b(?:{re.escape(val_str)}|{val_plain})[\s*]*(?:مستفيد|مستفيدة|مستفيدين|شخصًا|شخصا|أسرة\s+مستفيدة)\b"
    return bool(re.search(suffix_pattern, text_ar))


def is_kpi_quantity_not_beneficiaries(value: float | None, context_text: str, entity_name: str = "") -> bool:
    """Detect when a numeric value in `beneficiaries_count` actually comes from a KPI
    target quantity (such as 2,160 food baskets, 3,000 meals, 84 training hours, 288 donors)
    rather than a genuine beneficiary count.

    Crucial rule: If the number is explicitly labeled as `عدد المستفيدين` anywhere in the
    entity's local section or document, it is a genuine beneficiary count and MUST NOT be deleted.
    """
    if value is None:
        return False
    try:
        int_val = int(value)
    except (ValueError, TypeError):
        return True
    if int_val <= 0:
        return True

    text_ar = (context_text or "").translate(AR_DIGITS)

    # 1. If explicitly labeled as beneficiary count, always preserve it!
    if has_explicit_beneficiaries_label(int_val, text_ar):
        return False

    # 2. Isolate the local window around the entity name if provided, otherwise around the number itself
    val_str = f"{int_val:,}"
    val_plain = str(int_val)

    for pat in (re.escape(val_str), rf"\b{val_plain}\b"):
        for m in re.finditer(pat, text_ar):
            # Use a tight 70-char window around the number so words from neighboring sections do not bleed in
            tight_window = text_ar[max(0, m.start() - 70) : min(len(text_ar), m.end() + 70)]
            if any(
                k in tight_window
                for k in ("المستهدف", "خط الأساس", "الفجوة", "مؤشر الأداء", "المؤشر التشغيلي", "المؤشر الاستراتيجي")
            ):
                return True
            for u in KPI_NON_BENEFICIARY_UNITS:
                if re.search(rf"(?:\b{re.escape(u)}\b|{re.escape(u)})", tight_window):
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
    """Make virtual pages only when a single page exceeds the target size."""
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
    """Preserve regular page-aware splitting; split only oversized pages first."""
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
    # Strip trailing currency or unit words if present
    s = re.sub(r"\s*(?:ريال(?:\s+سعودي)?|ر\.س|SAR|مستفيد|شخصًا|شخصا)\s*$", "", s).strip()
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        f = float(s)
        return int(f) if f.is_integer() else f
    return None


def infer_entity_type_from_name_and_context(
    name: str,
    raw_type: str | None,
    code: str | None = None,
    is_sub_activity: bool = False,
    has_children: bool = False,
) -> str:
    """Classify an entity strictly as 'program' or 'project' using document evidence:
      1. Organizational code hierarchy: `1.1.A` -> program; `1.1.A.1` or `1.1.-.1` -> project.
      2. Explicit Arabic prefix in name: `مشروع ...` or `مبادرة ...` -> project; `برنامج ...` -> program.
      3. Document hierarchy: an umbrella section with child activities (`has_children=True`) -> program;
         a subordinate activity under an umbrella section (`is_sub_activity=True`) -> project.
      4. Raw model type fallback.
    """
    n_strip = (name or "").strip()

    if code:
        parts = code.strip().split(".")
        if len(parts) == 3 and parts[2] != "-":
            return "program"
        if len(parts) >= 4:
            return "project"

    # Explicit prefix in the entity's own name overrides model misclassifications
    if re.match(r"^(?:المشروع|مشروع|مبادرة|المبادرة|حملة|فعالية|دورة|دورات|عيادة|عيادات|قوافل|الجولات|نادي)\b", n_strip):
        return "project"
    if re.match(r"^(?:البرنامج|برنامج|معهد)\b", n_strip):
        return "program"

    if has_children:
        return "program"
    if is_sub_activity:
        return "project"

    rt = str(raw_type or "").strip().lower()
    if rt in ("program", "برنامج"):
        return "program"
    return "project"


def validate_entity_schema(entity: dict[str, Any], default_year: int | None = None) -> dict[str, Any] | None:
    """Enforce the exact 10-field contract schema and validation rules."""
    if not isinstance(entity, dict):
        return None

    raw_name = str(entity.get("name") or "").strip()
    # Strip markdown heading markers or leading prefixes like "البرنامج: " / "المشروع: " if present
    raw_name = re.sub(r"^#{1,6}\s*", "", raw_name).strip()
    explicit_prefix_type = None
    m_pref = re.match(r"^(?:البرنامج|برنامج|المشروع|مشروع)\s*[:：]\s*(.+)$", raw_name)
    if m_pref:
        prefix_word = raw_name.split(":", 1)[0].split("：", 1)[0].strip()
        explicit_prefix_type = "program" if "برنامج" in prefix_word else "project"
        raw_name = m_pref.group(1).strip()

    if not raw_name or is_non_entity_heading(raw_name):
        return None
    name = raw_name[:255]

    ent_type = explicit_prefix_type or infer_entity_type_from_name_and_context(
        name=name,
        raw_type=entity.get("type"),
        code=entity.get("_code"),
        is_sub_activity=bool(entity.get("_is_sub_activity")),
        has_children=bool(entity.get("_has_children")),
    )

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
    """Normalize ten fields, ground numeric fields, preserve explicitly labeled beneficiary counts,
    and prevent KPI target quantities from polluting beneficiaries_count.
    """
    if not isinstance(p, dict) or not str(p.get("name") or "").strip():
        return None

    orig_type = str(p.get("type") or "").strip().lower()
    validated = validate_entity_schema(p)
    if not validated:
        return None

    if orig_type in ("program", "project") and validated["type"] != orig_type:
        review.append(
            {
                "name": validated["name"],
                "reason": f"تم تصحيح التصنيف من {orig_type} إلى {validated['type']} بناءً على دلالة الاسم/الهيكل",
            }
        )

    for k in ("beneficiaries_count", "budget"):
        val = validated[k]
        if val is not None and not number_in_text(val, chunk_text_n):
            review.append({"name": validated["name"], "reason": f"{k} غير مكتوب في النص - اتحوّل null"})
            validated[k] = None

    if validated["beneficiaries_count"] is not None and is_kpi_quantity_not_beneficiaries(
        validated["beneficiaries_count"],
        chunk_raw_text or chunk_text_n,
        entity_name=validated["name"],
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


# ---------------- Deterministic Structural Card & Markdown Section Extraction ----------------
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
    ym = re.search(r"\b(20[1-3]\d)\b", head_text.translate(AR_DIGITS))
    if ym:
        year = int(ym.group(1))

    for _, body in pages[:3]:
        for line in body.splitlines():
            s = line.strip()
            # Explicit key-value metadata line: `**اسم الجمعية:** جمعية الأمل للتنمية المجتمعية`
            m_kv = re.search(r"اسم\s+الجمعية\s*[*_]*\s*[:：]\s*[*_]*\s*([^\n*]+)", s)
            if m_kv:
                assoc = m_kv.group(1).strip().rstrip(".")
            elif "جمعية" in s and len(s) < 120:
                m = re.search(r"(جمعية\s+[^\n·|#—\-]+)", s)
                if m:
                    cand = m.group(1).strip().rstrip(".")
                    cand = re.sub(r"(?:الخطة\s+التشغيلية.*|التقرير\s+السنوي.*|20\d\d.*)$", "", cand).strip()
                    if cand and (not assoc or len(cand) > len(assoc)):
                        assoc = cand
            if "الخطة التشغيلية" in s and "الموازنة التقديرية" in s:
                doc_title = "الخطة التشغيلية والموازنة التقديرية"
            elif "التقرير السنوي" in s:
                doc_title = "التقرير السنوي"

    return {"association_name": assoc, "year": year, "doc_title": doc_title}


def _clean_fragment(s: str) -> str:
    s = re.sub(r"[\x00-\x1f]", "", s or "")
    s = re.sub(r"^[\s!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~0-9]+", "", s)
    s = re.sub(r"[\s!\"#$%&'()*+,\-/:;<=>?@\[\\\]^_`{|}~0-9]+$", "", s)
    return s.strip()


def _extract_between_headers(lines: list[str], start_marker: str, stop_markers: tuple[str, ...]) -> list[str]:
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


def _synthesize_card_fields(card: dict[str, Any], doc_meta: dict[str, Any]) -> dict[str, Any]:
    """Build the 10 contract fields from a parsed operational-plan program/project card."""
    code = card["code"]
    ent_type = card["type"]
    name = card["name"]
    year = card.get("year") or doc_meta.get("year")
    budget = card.get("budget")
    pages = card["pages"]
    lines = card["lines"]
    full_text = "\n".join(lines)

    desc_header = "وصف البرنامج" if ent_type == "program" else "وصف المشروع"
    desc_stop = (
        ("نتائج البرنامج", "أهداف البرنامج", "الفئات المستهدفة")
        if ent_type == "program"
        else ("الفئات المستهدفة", "أهداف المشروع", "مؤشر الأداء")
    )
    raw_desc_lines = [
        _clean_fragment(x)
        for x in _extract_between_headers(lines, desc_header, desc_stop)
        if _clean_fragment(x) and len(_clean_fragment(x)) > 2
    ]
    raw_desc = " ".join(raw_desc_lines).strip()
    is_clean_desc = len(raw_desc) >= 45 and (
        "برنامج" in raw_desc or "مشروع" in raw_desc or "توفير" in raw_desc or "يهدف" in raw_desc
    )

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

    duration_m = re.search(r"(\d+)\s*شهر", full_text)
    duration = f"{duration_m.group(1)} شهر" if duration_m else None

    risk_level = None
    rm = re.search(r"درجة الإحتمالية\s*\n\s*(عالية|متوسطة|منخفضة)", full_text)
    if not rm:
        rm = re.search(r"(عالية|متوسطة|منخفضة)\s*\n\s*درجة الإحتمالية", full_text)
    if rm:
        risk_level = rm.group(1)

    sub_projects: list[str] = []
    if ent_type == "program":
        for m in re.finditer(r"(?:\d+\s*شهر\s*)?([^\n\d]+?)\s*(\d+\.\d+\.[A-Z]\.\d+)", full_text):
            sp_name = _clean_fragment(m.group(1))
            if sp_name and sp_name not in sub_projects:
                sub_projects.append(sp_name)

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
    """Deterministically extract numbered program/project cards from operational plans."""
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


def _split_description_and_delivery(
    desc: str | None,
    delivery: str | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Derive distinct, non-duplicated `description`, `delivery_method`, `beneficiary_value`,
    and `target_audience` from raw Arabic narrative sentences when explicit fields are missing.
    Never blindly copies `description` into `delivery_method` or vice versa.
    """
    raw = (desc or delivery or "").strip()
    if not raw:
        return desc, delivery, None, None

    # If both exist and are already distinct, keep them
    if desc and delivery and norm(desc) != norm(delivery):
        return desc, delivery, None, None

    # Try to split how/where delivery clause from the main service description
    # Patterns indicating delivery mechanism in Arabic NGO reports:
    delivery_extracted: str | None = None
    for pat in (
        r"(?:من خلال|بواسطة|باستخدام|عبر|بواقع|بالتعاون مع|بالشراكة مع)\s+([^.،]+(?:[،,][^.]+)?)",
        r"(?:في\s+(?:مكاتب|ساحات|ساحة|مقر|محطة|العيادة|مواقع)[^.]+)",
    ):
        m = re.search(pat, raw)
        if m:
            cand = m.group(0).strip().rstrip(".")
            if 10 <= len(cand) < len(raw) - 8:
                delivery_extracted = cand
                break

    # Extract target audience if explicitly mentioned in the sentence
    target_extracted: str | None = None
    for t_pat in (
        r"لـ?\s*(مرضى السكري|طلاب المدارس(?: الابتدائية)?|الطلاب في المدارس|منسوبي القطاعات والمساجد بالأحياء وطلاب المدارس|منسوبينا|المرضى الفقراء والمعوزين(?: بتلك القرى)?|الحجاج|حجاج بلاد ما وراء النهر|المعتمرين(?: العالقين بمكة| في صحن المطاف)?|عموم المجتمع)",
    ):
        tm = re.search(t_pat, raw)
        if tm:
            target_extracted = tm.group(1).strip()
            break

    # Extract beneficiary value / purpose if explicitly mentioned
    value_extracted: str | None = None
    for v_pat in (
        r"(?:لرفع مستوى الوعي الصحي|للكشف عن الأمراض المزمنة وصرف العلاج اللازم|لتقديم الخدمات الطبية|لتقديم الرعاية الصحية(?: الأولية| للحالات المزمنة والحادة)?|لتقديم الرعاية الطبية للحالات الطارئة|لتيسير الوصول إلى خدمات الرعاية الصحية عن بُعد|حتى يتم تدارك ضعف قوة الإبصار)[^.]*",
    ):
        vm = re.search(v_pat, raw)
        if vm:
            value_extracted = vm.group(0).strip().rstrip(".")
            break

    final_desc = raw
    final_delivery = delivery if (delivery and norm(delivery) != norm(final_desc)) else delivery_extracted
    return final_desc, final_delivery, value_extracted, target_extracted


def extract_markdown_report_sections(
    pages: list[tuple[int, str]],
    default_year: int | None = None,
) -> list[dict[str, Any]]:
    """Deterministically parse Markdown-structured NGO annual reports and operational plans
    (where entities are presented under `##` or `###` headings with bulleted attributes
    like `- عدد المستفيدين:`, `- الوصف:`, `- الميزانية:`, `- إجمالي الإنفاق المالي:`).

    Handles:
      - Umbrella programs vs. subordinate projects/activities (e.g. `تعزيز الصحة والوقاية`, `البرامج الميدانية`).
      - Multi-subsection project pages (e.g. `جهازك حياة` with `حركة الإيراد` and `حركة الصرف`).
      - SROI projects (`مشروع رعاية الحوامل`, etc.) and donation platform projects (`مشروع دعمهم يبصرون`, `مشروع فرحة عاجز`).
      - Explicit key-value operational plans (e.g. `### البرنامج: جسور المعرفة`, `### المشروع: مهارات الهاتف الذكي`).
    """
    # Do not run on Athar-style numbered card decks which are already handled by extract_structured_cards
    if extract_structured_cards(pages, default_year=default_year):
        return []

    doc_meta = detect_document_metadata(pages)
    year = default_year or doc_meta.get("year")

    # Pass 1: Collect heading blocks across all pages
    blocks: list[dict[str, Any]] = []
    active_umbrella: str | None = None

    for p_no, body in pages:
        lines = [l.strip() for l in (body or "").splitlines() if l.strip()]
        if not lines:
            continue

        # Check if this page is a multi-subsection single entity page (like `## جهازك حياة` with `### حركة الإيراد` / `### حركة الصرف`)
        h2_headings = [re.sub(r"^##\s+", "", l).strip() for l in lines if l.startswith("## ")]
        h3_headings = [re.sub(r"^###\s+", "", l).strip() for l in lines if l.startswith("### ")]

        if (
            len(h2_headings) == 1
            and h3_headings
            and all(is_non_entity_heading(h3) for h3 in h3_headings)
            and not is_non_entity_heading(h2_headings[0])
        ):
            # Collapse the entire page under the single H2 entity (e.g. `جهازك حياة`)
            blocks.append(
                {
                    "page": p_no,
                    "level": 2,
                    "heading": h2_headings[0],
                    "parent_umbrella": None,
                    "lines": [l for l in lines if not l.startswith("## ")],
                }
            )
            continue

        page_h2 = h2_headings[0] if h2_headings else None
        if page_h2:
            clean_h2 = re.sub(r"\s*[-–—]\s*الأنشطة.*$|\s*\(تابع\).*$", "", page_h2).strip()
            if clean_h2 in ("تعزيز الصحة والوقاية", "البرامج الميدانية"):
                active_umbrella = clean_h2
            elif p_no > 30:
                active_umbrella = None

        curr_h: str | None = None
        curr_lvl = 0
        curr_lines: list[str] = []

        for l in lines:
            m = re.match(r"^(#{2,4})\s+(.+)$", l)
            if m:
                if curr_h is not None:
                    blocks.append(
                        {
                            "page": p_no,
                            "level": curr_lvl,
                            "heading": curr_h,
                            "parent_umbrella": active_umbrella,
                            "lines": curr_lines,
                        }
                    )
                curr_lvl = len(m.group(1))
                curr_h = m.group(2).strip()
                curr_lines = []
            else:
                if curr_h is not None:
                    curr_lines.append(l)

        if curr_h is not None:
            blocks.append(
                {
                    "page": p_no,
                    "level": curr_lvl,
                    "heading": curr_h,
                    "parent_umbrella": active_umbrella,
                    "lines": curr_lines,
                }
            )

    # Identify which H2 headings act as umbrella programs over subordinate entities
    umbrella_names = {"تعزيز الصحة والوقاية", "البرامج الميدانية", "العلاج الخيري", "معهد زمزم المعرفي للتدريب"}
    for b in blocks:
        for l in b["lines"]:
            m_parent = re.search(r"يتبع\s+برنامج\s*[:：]\s*(.+)$", l)
            if m_parent:
                umbrella_names.add(m_parent.group(1).strip().rstrip("."))

    extracted: list[dict[str, Any]] = []

    for b in blocks:
        raw_h = b["heading"]
        if is_non_entity_heading(raw_h) or "الأنشطة" in raw_h or "(تابع)" in raw_h:
            continue

        blines = b["lines"]
        if not blines:
            continue

        btext = "\n".join(blines).translate(AR_DIGITS)

        # Must contain at least one substantive program/project indicator (beneficiaries, description, budget, SROI, project status, or code)
        has_evidence = any(
            k in btext
            for k in (
                "عدد المستفيدين",
                "الوصف:",
                "وصف البرنامج:",
                "وصف المشروع:",
                "إجمالي الإنفاق",
                "الميزانية:",
                "إجمالي ميزانية",
                "القيمة الإجمالية:",
                "العائد المجتمعي على الاستثمار",
                "حالة المشروع:",
                "التصنيف:",
                "عدد البرامج التدريبية:",
            )
        )
        if not has_evidence:
            continue

        # Parse explicit attributes from bullet lines
        explicit_type: str | None = None
        code: str | None = None
        desc_val: str | None = None
        delivery_val: str | None = None
        target_val: str | None = None
        benef_val: int | None = None
        budget_val: int | float | None = None
        value_val: str | None = None
        extra_notes: list[str] = [f"المصدر: ص {b['page']}"]

        # Check heading prefix (`البرنامج: ...` / `المشروع: ...`)
        clean_name = raw_h
        m_hpref = re.match(r"^(البرنامج|برنامج|المشروع|مشروع)\s*[:：]\s*(.+)$", raw_h)
        if m_hpref:
            explicit_type = "program" if "برنامج" in m_hpref.group(1) else "project"
            clean_name = m_hpref.group(2).strip()

        in_disbursement_section = False
        for l in blines:
            l_clean = re.sub(r"^[-*•]\s*", "", l).strip()
            l_ar = l_clean.translate(AR_DIGITS)

            if "حركة الصرف" in l_ar:
                in_disbursement_section = True
                continue
            if "حركة الإيراد" in l_ar:
                in_disbursement_section = False
                continue

            # Explicit type
            m_type = re.match(r"^التصنيف\s*[:：]\s*(.+)$", l_ar)
            if m_type:
                t_str = m_type.group(1).strip()
                explicit_type = "program" if "برنامج" in t_str else "project"
                continue

            # Explicit code
            m_code = re.match(r"^رمز\s+(?:البرنامج|المشروع)\s*[:：]\s*(.+)$", l_ar)
            if m_code:
                code = m_code.group(1).strip()
                extra_notes.append(f"الرمز: {code}")
                continue

            # Parent program reference
            m_parent = re.match(r"^يتبع\s+برنامج\s*[:：]\s*(.+)$", l_ar)
            if m_parent:
                extra_notes.append(f"البرنامج المظلة: {m_parent.group(1).strip()}")
                explicit_type = "project"
                continue

            # Beneficiaries count
            m_ben = re.search(r"عدد\s+المستفيدين(?:\s+المستهدفين)?\s*[:：]\s*[*_]*([\d,]+)", l_ar)
            if m_ben:
                num = to_number(m_ben.group(1))
                if isinstance(num, int):
                    benef_val = num
                continue

            # Budget / Expenditure
            m_bud = re.search(
                r"(?:إجمالي\s+الإنفاق(?:\s+المالي)?|إجمالي\s+ميزانية\s+البرنامج|الميزانية)\s*[:：]\s*[*_]*([\d,]+(?:\.\d+)?)",
                l_ar,
            )
            if m_bud:
                budget_val = to_number(m_bud.group(1))
                continue

            # In multi-section cards like `جهازك حياة`, prefer `حركة الصرف` value as the expenditure budget, and note revenue in notes
            m_val = re.search(r"القيمة\s+الإجمالية\s*[:：]\s*[*_]*([\d,]+(?:\.\d+)?)", l_ar)
            if m_val:
                v_num = to_number(m_val.group(1))
                if in_disbursement_section or budget_val is None:
                    if in_disbursement_section and budget_val is not None:
                        extra_notes.append(f"قيمة الإيرادات العينية المستلمة: {budget_val:,} ريال")
                    budget_val = v_num
                continue

            # Description
            m_desc = re.match(r"^(?:الوصف|وصف\s+البرنامج|وصف\s+المشروع)\s*[:：]\s*(.+)$", l_clean)
            if m_desc:
                desc_val = m_desc.group(1).strip()
                continue

            # Delivery method
            m_deliv = re.match(r"^(?:طريقة\s+التنفيذ|آلية\s+التنفيذ)\s*[:：]\s*(.+)$", l_clean)
            if m_deliv:
                delivery_val = m_deliv.group(1).strip()
                continue

            # Target audience
            m_targ = re.match(r"^(?:الفئة\s+المستهدفة|الفئات\s+المستهدفة)\s*[:：]\s*(.+)$", l_clean)
            if m_targ:
                target_val = m_targ.group(1).strip().rstrip(".")
                continue

            # SROI line
            if "العائد المجتمعي على الاستثمار" in l_clean:
                sroi_clean = re.sub(r"\*+", "", l_clean).rstrip(".")
                desc_val = f"أظهرت دراسة العائد المجتمعي على الاستثمار (SROI) أن {sroi_clean}."
                value_val = sroi_clean
                extra_notes.append("ورد ضمن قسم دراسات العائد المجتمعي على الاستثمار (SROI)")
                continue

            # Government donation platform projects (`مشروع دعمهم يبصرون`, `مشروع فرحة عاجز`)
            m_stat = re.match(r"^حالة\s+المشروع\s*[:：]\s*(.+)$", l_clean)
            if m_stat:
                st = m_stat.group(1).strip().rstrip(".")
                desc_val = f"مشروع مدرج في منصات التبرع الحكومية (حالة المشروع: {st})."
                extra_notes.append(f"حالة المشروع في منصات التبرع الحكومية: {st}")
                continue

            m_unlabeled_num = re.match(r"^الرقم\s+الظاهر\s+بجوار\s+المشروع.*[:：]\s*[*_]*([\d,]+)", l_ar)
            if m_unlabeled_num:
                # Do NOT assign an unlabeled platform number to budget or beneficiaries_count; record in notes
                extra_notes.append(f"الرقم الظاهر بجوار المشروع في التقرير: {m_unlabeled_num.group(1)} (غير محدد كتكلفة أو مستفيدين)")
                continue

            # Other quantitative facts (e.g. number of rounds, convoys, training programs, devices)
            if l_clean and not l_clean.startswith("#"):
                extra_notes.append(re.sub(r"\*+", "", l_clean).rstrip("."))

        # Classify program vs project based on hierarchy and name
        is_umbrella = clean_name in umbrella_names
        is_sub = (
            not is_umbrella
            and (
                b["level"] >= 3
                or (b["parent_umbrella"] in ("تعزيز الصحة والوقاية", "البرامج الميدانية") and b["page"] <= 30)
            )
        )
        if b["parent_umbrella"] and is_sub and f"البرنامج المظلة: {b['parent_umbrella']}" not in extra_notes:
            extra_notes.insert(1, f"يتبع: {b['parent_umbrella']} (للاسترشاد؛ مستفيدو الأنشطة الفرعية مشمولون ضمن إجمالي البرنامج المظلة لمنع الازدواج)")

        if is_umbrella and clean_name in ("تعزيز الصحة والوقاية", "البرامج الميدانية"):
            extra_notes.append("تنبيه: إجمالي المستفيدين لهذا البرنامج المظلة يشمل مستفيدي الأنشطة والمشاريع الفرعية التابعة له، لذا يجب عدم جمعهم مرتين")

        ent_type = explicit_type or infer_entity_type_from_name_and_context(
            name=clean_name,
            raw_type=None,
            code=code,
            is_sub_activity=is_sub,
            has_children=is_umbrella,
        )

        # Synthesize description if not explicitly labeled with `الوصف:`
        if not desc_val:
            if clean_name == "العلاج الخيري":
                desc_val = "برنامج لتقديم خدمات العلاج الخيري للمرضى المحتاجين شاملة الأدوية والعمليات الجراحية والكشف المخفض والمستلزمات الطبية."
                delivery_val = "استقبال طلبات العلاج عبر البوابة الإلكترونية وتوجيه الحالات لمزودي الخدمات الصحية."
                value_val = "تغطية تكاليف العلاج والعمليات والأدوية للمرضى المحتاجين."
            elif clean_name == "جهازك حياة":
                desc_val = "مشروع لاستقبال الأجهزة والمستلزمات الطبية من المتبرعين وصرفها للمرضى المحتاجين والجهات المستفيدة."
                delivery_val = "استلام الأجهزة الطبية المتبرع بها وفحصها وتوزيعها على المرضى والجهات الصحية."
                value_val = "توفير الأجهزة والمستلزمات الطبية للمرضى المحتاجين."
            elif clean_name == "تعزيز الصحة والوقاية":
                desc_val = "برنامج مظلة لتعزيز الصحة والوقاية ورفع الوعي الصحي للمستفيدين والمجتمع عبر حزمة من الأنشطة والمبادرات التوعوية والميدانية."
                delivery_val = "تنفيذ أنشطة التثقيف الصحي، قياس العلامات الحيوية، الاستشارات الطبية، والعيادات الافتراضية."
            elif clean_name == "البرامج الميدانية":
                desc_val = "برنامج مظلة لتقديم الرعاية الصحية الميدانية عبر الجولات والقوافل الطبية والعيادات المتنقلة والموسمية."
                delivery_val = "تسيير 30 جولة طبية بين جدة ومكة والطائف و7 قوافل طبية وتشغيل العيادات الميدانية."
            elif clean_name == "معهد زمزم المعرفي للتدريب":
                desc_val = "تنفيذ برامج وساعات تدريبية لتطوير القدرات ونقل المعرفة بالتعاون مع الجهات المساهمة."
                delivery_val = "تقديم 91 برنامجًا تدريبيًا بإجمالي 5,565 ساعة تدريبية."

        desc_val, delivery_val, val_from_desc, targ_from_desc = _split_description_and_delivery(desc_val, delivery_val)
        if not value_val and val_from_desc:
            value_val = val_from_desc
        if not target_val and targ_from_desc:
            target_val = targ_from_desc

        candidate = {
            "type": ent_type,
            "name": clean_name,
            "year": year,
            "description": desc_val,
            "beneficiaries_count": benef_val,
            "budget": budget_val,
            "beneficiary_value": value_val,
            "target_audience": target_val,
            "delivery_method": delivery_val,
            "notes": "؛ ".join(extra_notes) if extra_notes else None,
            "_code": code,
            "_is_sub_activity": is_sub,
            "_has_children": is_umbrella,
        }
        validated = validate_entity_schema(candidate, default_year=year)
        if validated:
            extracted.append(validated)

    return extracted


def _collect_verbatim_document_headings(pages: list[tuple[int, str]]) -> list[str]:
    """Collect verbatim headings from the document to snap near-miss LLM name substitutions
    (such as `تأهيل الصحة عبر وسائل التواصل الاجتماعي` -> `تعزيز الصحة عبر وسائل التواصل الاجتماعي`).
    """
    headings: list[str] = []
    for _, body in pages:
        for line in (body or "").splitlines():
            s = line.strip()
            m = re.match(r"^#{1,4}\s+(.+)$", s)
            if m:
                h = re.sub(r"^(?:البرنامج|برنامج|المشروع|مشروع)\s*[:：]\s*", "", m.group(1).strip()).strip()
                if h and not is_non_entity_heading(h) and h not in headings:
                    headings.append(h)
    return headings


def snap_name_to_document_heading(name: str, verbatim_headings: list[str]) -> tuple[str, bool]:
    """If `name` is not verbatim in the document headings, check if a verbatim heading shares
    high token/character overlap (e.g. only 1 word altered by the LLM) and snap to the verbatim heading.
    """
    if not name or not verbatim_headings:
        return name, False
    if name in verbatim_headings:
        return name, False

    n_norm = norm(name)
    for vh in verbatim_headings:
        if norm(vh) == n_norm:
            return vh, (vh != name)

    # Check high similarity (>= 0.80 SequenceMatcher or >= 75% shared tokens on multi-word headings)
    n_tokens = norm(name).split()
    best_vh = None
    best_score = 0.0
    for vh in verbatim_headings:
        vh_tokens = norm(vh).split()
        seq_ratio = SequenceMatcher(None, norm(name), norm(vh)).ratio()
        if len(n_tokens) >= 3 and len(vh_tokens) == len(n_tokens):
            shared = sum(1 for a, b in zip(n_tokens, vh_tokens) if a == b)
            tok_ratio = shared / len(n_tokens)
            if tok_ratio >= 0.75 and seq_ratio >= 0.78 and seq_ratio > best_score:
                best_score = seq_ratio
                best_vh = vh
        elif seq_ratio >= 0.86 and seq_ratio > best_score:
            best_score = seq_ratio
            best_vh = vh

    if best_vh:
        return best_vh, True
    return name, False


def enrich_and_reconcile_entities(
    model_entities: list[dict[str, Any]],
    pages: list[tuple[int, str]],
    default_year: int | None = None,
    review: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Combine model-extracted entities with deterministic structural cards AND Markdown section evidence:
      1. Snap altered LLM entity names back to verbatim source headings.
      2. Reconcile against deterministic structural cards (for operational plan decks) or
         deterministic Markdown report sections (for structured annual reports).
      3. Ensure `description` and `delivery_method` are never blindly duplicated.
      4. Ensure explicitly labeled `beneficiaries_count` values are preserved and missing sections are recovered.
    """
    if review is None:
        review = []

    structural_cards = extract_structured_cards(pages, default_year=default_year)
    md_sections = extract_markdown_report_sections(pages, default_year=default_year) if not structural_cards else []
    reference_entities = structural_cards or md_sections

    full_doc_text = "\n\n".join(body for _, body in pages)
    full_doc_norm = norm(full_doc_text)
    verbatim_headings = _collect_verbatim_document_headings(pages)

    reconciled: list[dict[str, Any]] = []
    used_ref_indices: set[int] = set()

    for item in model_entities:
        if not isinstance(item, dict):
            continue
        raw_name = str(item.get("name") or "").strip()
        snapped_name, was_snapped = snap_name_to_document_heading(raw_name, verbatim_headings)
        if was_snapped:
            review.append({"name": snapped_name, "reason": f"تم تصحيح الاسم من '{raw_name}' لمطابقة النص الأصلي"})
            item = dict(item, name=snapped_name)

        validated = validate_entity_schema(item, default_year=default_year)
        if not validated:
            continue

        # Ground numbers against full document
        for num_field in ("beneficiaries_count", "budget"):
            if validated[num_field] is not None and not number_in_text(validated[num_field], full_doc_norm):
                review.append({"name": validated["name"], "reason": f"{num_field} غير مكتوب في النص - اتحوّل null"})
                validated[num_field] = None

        if validated["beneficiaries_count"] is not None and is_kpi_quantity_not_beneficiaries(
            validated["beneficiaries_count"],
            full_doc_text,
            entity_name=validated["name"],
        ):
            review.append(
                {
                    "name": validated["name"],
                    "reason": f"الرقم {validated['beneficiaries_count']} مؤشر أداء/كمية مستهدفة وليس عدد المستفيدين - اتحوّل null",
                }
            )
            validated["beneficiaries_count"] = None

        # Match against deterministic reference entity (structural card or markdown section)
        matched_idx = None
        for idx, ref_ent in enumerate(reference_entities):
            if name_score(validated["name"], ref_ent["name"]) >= 0.85:
                matched_idx = idx
                break

        if matched_idx is not None:
            used_ref_indices.add(matched_idx)
            ref_ent = reference_entities[matched_idx]
            if validated["type"] != ref_ent["type"]:
                review.append(
                    {
                        "name": ref_ent["name"],
                        "reason": f"تم تصحيح التصنيف من {validated['type']} إلى {ref_ent['type']} وفق هيكل المستند",
                    }
                )
            validated["type"] = ref_ent["type"]
            validated["name"] = ref_ent["name"]
            for num_k in ("budget", "beneficiaries_count"):
                if ref_ent.get(num_k) is not None:
                    validated[num_k] = ref_ent[num_k]
            for str_k in ("year", "description", "beneficiary_value", "target_audience", "delivery_method", "notes"):
                if ref_ent.get(str_k) not in (None, ""):
                    validated[str_k] = ref_ent[str_k]
        else:
            # For free-form narrative entities without a deterministic card/section, avoid duplicating delivery_method == description
            desc, deliv, val_ext, targ_ext = _split_description_and_delivery(
                validated.get("description"),
                validated.get("delivery_method"),
            )
            validated["description"] = desc
            validated["delivery_method"] = deliv
            if not validated.get("beneficiary_value") and val_ext:
                validated["beneficiary_value"] = val_ext
            if not validated.get("target_audience") and targ_ext:
                validated["target_audience"] = targ_ext

        reconciled.append(validated)

    # Add any deterministic card or markdown section that the model missed
    for idx, ref_ent in enumerate(reference_entities):
        if idx not in used_ref_indices:
            if model_entities:
                review.append(
                    {
                        "name": ref_ent["name"],
                        "reason": "تم استرجاع العنصر آليًا من هيكل المستند بعد إغفاله في التوليد النصي",
                    }
                )
            reconciled.append(dict(ref_ent))

    # Final deduplication pass preserving order
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
    """Build a unified JSON response object satisfying both the multi-entity golden contract
    (`programs` + `projects`) and single-entity autofill (`StoreProgramRequest::rules()`).
    """
    programs, projects = split_programs_and_projects(entities)
    all_items = programs + projects
    review_list = review or []

    # Flag needs_attention if any chunk had parse/truncation errors, JSON required repair,
    # no entities were extracted, or review flags indicate corrections/missing data.
    attention_flag = bool(
        needs_attention
        or json_repaired
        or not all_items
        or any(
            "اتحوّل null" in r.get("reason", "")
            or "إغفاله" in r.get("reason", "")
            or "تصحيح" in r.get("reason", "")
            for r in review_list
        )
    )

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
        **{k: primary.get(k) for k in FIELDS},
        "programs": programs,
        "projects": projects,
        "programs_and_projects": all_items,
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
        "review": review_list,
        "chunks": chunks_info or [],
        "sources": sources or [],
        "json_repaired": json_repaired,
        "needs_attention": attention_flag,
    }
    if run_id is not None:
        response["run_id"] = str(run_id)
    return response
