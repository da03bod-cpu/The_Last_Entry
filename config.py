import os

MODEL_ID = os.getenv("MODEL_ID", "mistralai/Mistral-Small-24B-Instruct-2501")
# الـLoRA بيتحمّل وقت التشغيل من Hugging Face (ريبو خاص): env variables في الـendpoint
#   LORA_REPO = USERNAME/REPO     HF_TOKEN = توكن read-only
#   LORA_PATH = (اختياري) مسار محلي للاختبار بس
LORA_REPO = os.getenv("LORA_REPO", "YOUR-HF-USERNAME/nonprofit-lora")
LORA_PATH = os.getenv("LORA_PATH", "")

# نفس إعدادات التقسيم اللي اتدرّب عليها الموديل (prepare_chunks.py): لا تغيّرها.
CHUNK_TARGET_CHARS = int(os.getenv("CHUNK_TARGET_CHARS", "10000"))
CHUNK_OVERLAP_PAGES = int(os.getenv("CHUNK_OVERLAP_PAGES", "1"))

# توليد: greedy، نفس تقييم التدريب (6144 توكن جديد للجزء الواحد)
MAX_NEW_TOKENS = int(os.getenv("MAX_NEW_TOKENS", "6144"))
MAX_INPUT_TOKENS = int(os.getenv("MAX_INPUT_TOKENS", "12000"))   # حماية: الجزء العادي ~4-5 آلاف توكن
DO_SAMPLE = False
REPETITION_PENALTY = float(os.getenv("REPETITION_PENALTY", "1.0"))
MERGE_NAME_THRESHOLD = float(os.getenv("MERGE_NAME_THRESHOLD", "0.9"))

# لازم يتطابق حرفيًا مع SYSTEM_PROMPT في train_v3.py. (لو الـadapter فيه system_prompt.txt بيتاخد هو بدل ده تلقائيًا.)
SYSTEM_PROMPT = (
    "أنت نموذج متخصص في استخراج البرامج والمشاريع والمبادرات والفعاليات الرئيسية من التقارير السنوية "
    "للجمعيات غير الربحية. اقرأ النص كما هو (قد يحتوي على أخطاء OCR بسيطة وصفحات غير مكتملة)، "
    "واستخرج فقط ما ذُكر فعليًا فيه، بدون تكرار وبدون دمج عناصر مختلفة. لا تستخرج قرارات مجلس الإدارة "
    "ولا الشراكات الموقعة وحدها ولا الحسابات البنكية ولا بيانات التواصل ولا الأهداف الاستراتيجية العامة. "
    "أعد JSON فقط بالشكل {\"association_name\": ..., \"year\": ..., \"period\": ..., \"programs_and_projects\": [...]}. "
    "يبدأ النص بكتلة ===== METADATA ===== فيها اسم الجمعية وسنة التقرير والفترة: انسخ هذه القيم كما هي إلى association_name وyear وperiod "
    "(ضع null إذا كُتب غير مذكور أو غير مذكورة، وضع period بـnull إذا كُتب تقرير سنوي)، ولا تستخرج برامج من هذه الكتلة. "
    "وكل عنصر في programs_and_projects يحوي الحقول: type, name, year, description, beneficiaries_count, budget, "
    "beneficiary_value, target_audience, delivery_method, notes. ضع description دائمًا null، واكتب تفاصيل النشاط في "
    "delivery_method، والمكان أو التنبيهات في notes. إذا لم يوجد أي برنامج فأعد programs_and_projects قائمة فارغة. "
    "ولا تضف أي شرح خارج JSON. ضع null للمعلومة غير الموجودة في النص ولا تخترع أرقامًا."
)
