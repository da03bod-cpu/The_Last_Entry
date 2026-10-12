"""
postprocess.py - منطق بدون GPU (بيتجرّب offline): تقسيم المستند لأجزاء بنفس طريقة التدريب، كتلة METADATA،
قراءة ناتج الموديل، تأريض الأرقام، الدمج بين الأجزاء، وعلامات المراجعة.
مهم: الأرقام (10000 حرف، تداخل صفحة، صيغة METADATA) لازم تفضل مطابقة لبيانات التدريب.
"""
import html
import json
import re
from difflib import SequenceMatcher

PAGE_RE = re.compile(r"^===== PAGE (\d+) =====\s*$", re.MULTILINE)
META_START = "===== METADATA ====="
META_END = "===== END METADATA ====="
FIELDS = ["type", "name", "year", "description", "beneficiaries_count", "budget",
          "beneficiary_value", "target_audience", "delivery_method", "notes"]

AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
AR_DIAC = re.compile("[ً-ٰٟـ]")
GENERIC = {"برنامج", "مشروع", "مبادره", "فعاليه", "حمله", "دوره", "ملتقي", "ملتقى"}
# أسماء شكلها فعاليات خارجية/مشاركات: بتتعلّم "للمراجعة" بس ولا بتتحذف (الداتا فيها حالات حقيقية بنفس الشكل)
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


# ---------------- التقسيم (نفس prepare_chunks.py) ----------------
def strip_metadata(text: str):
    """لو النص جاي فيه كتلة METADATA في أوله، بنشيلها ونرجّع قيمها (الطلب بيتفوق عليها)."""
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


def build_chunks(pages, target_chars: int, overlap_pages: int):
    chunks = []
    i, n = 0, len(pages)
    while i < n:
        j, size = i, 0
        while j < n and (j == i or size + len(pages[j][1]) <= target_chars):
            size += len(pages[j][1])
            j += 1
        chunks.append(pages[i:j])
        if j >= n:
            break
        i = max(j - overlap_pages, i + 1)
    return chunks


def render(chunk_pages) -> str:
    return "\n\n".join(f"===== PAGE {no} =====\n{txt}" for no, txt in chunk_pages)


def build_meta_block(name, year, period) -> str:
    return (f"{META_START}\nاسم الجمعية: {name or 'غير مذكور'}\nسنة التقرير: {year or 'غير مذكورة'}\n"
            f"الفترة: {period or 'تقرير سنوي'}\n{META_END}\n\n")


# ---------------- قراءة الناتج ----------------
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
    """بيوحّد شكل البرنامج (10 حقول)، description=null، والأرقام لازم تكون مكتوبة في نص الجزء وإلا null."""
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


def merge_programs(lists, thr=0.9):
    """نفس الاسم (تشابه >= thr) = عنصر واحد، والحقول الفاضية بتتملى من النسخة التانية (مفيد مع تداخل الصفحات)."""
    merged = []
    for lst in lists:
        for p in lst:
            for q in merged:
                if name_score(p.get("name"), q.get("name")) >= thr:
                    for k, v in p.items():
                        if q.get(k) in (None, "") and v not in (None, ""):
                            q[k] = v
                    break
            else:
                merged.append(dict(p))
    return merged
