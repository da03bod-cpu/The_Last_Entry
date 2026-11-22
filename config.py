"""
config.py - Runtime configuration for Athar NGO Program & Project Extraction.

All parameters can be overridden via environment variables on the RunPod endpoint
without rebuilding the Docker image. No secrets are stored in this file.
"""

import os

MODEL_ID = os.getenv("MODEL_ID", "mistralai/Mistral-Small-24B-Instruct-2501")

# Fine-tuned LoRA adapter downloaded at startup from Hugging Face (private or public repo):
#   LORA_REPO = USERNAME/REPO
#   HF_TOKEN  = read-only Hugging Face token (configured in RunPod Secrets)
#   LORA_PATH = optional local path override for offline testing
LORA_REPO = os.getenv("LORA_REPO", "YOUR-HF-USERNAME/nonprofit-lora")
LORA_PATH = os.getenv("LORA_PATH", "")

# Cache directories (checks RunPod Network Volume, RunPod Cached Models, and local HF cache)
HF_CACHE_ROOT = os.getenv("HF_CACHE_ROOT", "/runpod-volume/huggingface-cache/hub")
LORA_CACHE_DIR = os.getenv("LORA_CACHE_DIR", "/runpod-volume/lora-cache")

# Training-aligned chunking parameters (prepare_chunks.py defaults)
CHUNK_TARGET_CHARS = int(os.getenv("CHUNK_TARGET_CHARS", "10000"))
CHUNK_OVERLAP_PAGES = int(os.getenv("CHUNK_OVERLAP_PAGES", "1"))

# Generation & context budget settings
MAX_NEW_TOKENS = int(os.getenv("MAX_NEW_TOKENS", "6144"))
MAX_INPUT_TOKENS = int(os.getenv("MAX_INPUT_TOKENS", "12000"))
DO_SAMPLE = False
REPETITION_PENALTY = float(os.getenv("REPETITION_PENALTY", "1.0"))
MERGE_NAME_THRESHOLD = float(os.getenv("MERGE_NAME_THRESHOLD", "0.9"))

# Bounded retry and timeout policies
MAX_CHUNK_RETRIES = int(os.getenv("MAX_CHUNK_RETRIES", "1"))
EXECUTION_TIMEOUT_SECONDS = int(os.getenv("EXECUTION_TIMEOUT_SECONDS", "540"))
INIT_TIMEOUT_SECONDS = int(os.getenv("INIT_TIMEOUT_SECONDS", "600"))
ENABLE_OCR = os.getenv("ENABLE_OCR", "1").strip().lower() not in ("0", "false", "no")

# Hybrid optimization: skip redundant GPU inference on pages whose structured cards
# were already deterministically extracted with 100% field grounding.
SKIP_LLM_FOR_STRUCTURED_CARD_PAGES = (
    os.getenv("SKIP_LLM_FOR_STRUCTURED_CARD_PAGES", "1").strip().lower() not in ("0", "false", "no")
)

# Progressive streaming settings (RunPod Serverless generator handler)
STREAM_SCHEMA_VERSION = "1.0"
RUNPOD_ENABLE_STREAMING = os.getenv("RUNPOD_ENABLE_STREAMING", "1").strip().lower() not in ("0", "false", "no")
RETURN_AGGREGATE_STREAM = os.getenv("RETURN_AGGREGATE_STREAM", "1").strip().lower() not in ("0", "false", "no")
STREAM_EMIT_PROGRESS = os.getenv("STREAM_EMIT_PROGRESS", "1").strip().lower() not in ("0", "false", "no")
STREAM_DEADLINE_BUFFER_SECONDS = int(os.getenv("STREAM_DEADLINE_BUFFER_SECONDS", "15"))

# Must match SYSTEM_PROMPT in train_v3.py (overridden automatically if adapter contains system_prompt.txt)
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
