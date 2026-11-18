"""
handler.py - RunPod Serverless handler for Athar NGO Program & Project Extraction.

Base model: mistralai/Mistral-Small-24B-Instruct-2501 (4-bit NF4) + Fine-tuned LoRA v3.

Key production capabilities:
  1. Multi-format document ingestion (PDF with Arabic OCR fallback, XLSX, DOCX, MD, plain text).
  2. Full backend/n8n contract support (`run_id`, `type`, `input` JSON string, positional `files[]`).
  3. Multi-path RunPod model cache discovery to prevent repeated 48GB Hugging Face downloads.
  4. Separation of container readiness from model readiness (`health_check` action & lazy init).
  5. Token-budgeted chunking with adaptive recursive splitting on truncation/parse failures.
  6. Deterministic structural card & evidence enrichment so `description` (and all 10 schema fields)
     are populated accurately without hallucination.
"""

from __future__ import annotations

import sys

# Prevent incompatible base-image torchvision from breaking transformers import
if "torchvision" not in sys.modules:
    sys.modules["torchvision"] = None  # type: ignore[assignment]

import json
import os
import re
import time
import traceback
from typing import Any

import config
import ingestion
import postprocess as pp

MODEL_NAME = os.getenv("MODEL_NAME", config.MODEL_ID)

# Global runtime state for lazy/safe initialization
_MODEL_STATE: dict[str, Any] = {
    "initialized": False,
    "error": None,
    "tokenizer": None,
    "model": None,
    "base_path": None,
    "lora_path": None,
    "system_prompt": config.SYSTEM_PROMPT,
    "prompt_token_budget": int(config.MAX_INPUT_TOKENS) + 600,
    "init_seconds": None,
}

# Candidate Hugging Face cache directories on RunPod Serverless (Cached Models & Network Volumes)
CACHE_CANDIDATE_ROOTS = [
    os.getenv("HF_CACHE_ROOT", ""),
    config.HF_CACHE_ROOT,
    "/runpod-volume/huggingface-cache/hub",
    "/runpod-volume/hf-cache/hub",
    "/workspace/huggingface-cache/hub",
    "/root/.cache/huggingface/hub",
    os.path.join(os.getenv("HF_HOME", "/tmp/hf"), "hub"),
]


def resolve_base_path(model_id: str, cache_roots: list[str] | None = None) -> str:
    """Locate the cached base model snapshot across all standard RunPod cache locations
    before falling back to downloading from Hugging Face.
    """
    if os.path.isdir(model_id) and os.path.isfile(os.path.join(model_id, "config.json")):
        return model_id

    if "/" not in model_id:
        return model_id

    org, name = model_id.split("/", 1)
    roots = cache_roots if cache_roots is not None else CACHE_CANDIDATE_ROOTS

    for root_dir in roots:
        if not root_dir or not os.path.isdir(root_dir):
            continue
        # RunPod Cached Models sometimes mount as lowercase or exact org--name
        for folder_name in (f"models--{org}--{name}", f"models--{org.lower()}--{name.lower()}"):
            model_root = os.path.join(root_dir, folder_name)
            if not os.path.isdir(model_root):
                continue
            snaps = os.path.join(model_root, "snapshots")
            refs_main = os.path.join(model_root, "refs", "main")
            if os.path.isfile(refs_main):
                with open(refs_main, encoding="utf-8") as f:
                    rev = f.read().strip()
                cand = os.path.join(snaps, rev)
                if os.path.isdir(cand) and os.path.isfile(os.path.join(cand, "config.json")):
                    return cand
            if os.path.isdir(snaps):
                versions = sorted(
                    d
                    for d in os.listdir(snaps)
                    if os.path.isdir(os.path.join(snaps, d))
                    and os.path.isfile(os.path.join(snaps, d, "config.json"))
                )
                if versions:
                    return os.path.join(snaps, versions[-1])

    print("WARNING: Base model not found in RunPod cache - downloading from Hugging Face.", flush=True)
    return model_id


def _get_hf_token() -> str | None:
    tok = (os.getenv("HF_TOKEN") or os.getenv("HUGGING_FACE_HUB_TOKEN") or "").strip().strip("\"'").strip()
    return tok or None


def resolve_lora_path() -> str:
    """Resolve the fine-tuned LoRA adapter from a local directory, persistent volume cache, or Hugging Face."""
    if config.LORA_PATH and os.path.isfile(os.path.join(config.LORA_PATH, "adapter_config.json")):
        return config.LORA_PATH

    repo = (os.getenv("LORA_REPO") or config.LORA_REPO or "").strip().strip("\"'")
    if not repo or repo == "YOUR-HF-USERNAME/nonprofit-lora":
        raise RuntimeError(
            "LORA_REPO environment variable is not configured (set LORA_REPO to your Hugging Face adapter repo "
            "or LORA_PATH to a local directory)."
        )

    from huggingface_hub import snapshot_download

    tok = _get_hf_token()
    print(f"Downloading/verifying LoRA from Hugging Face: repo={repo!r} token_present={bool(tok)}", flush=True)

    # Prefer persistent volume cache if writable, otherwise /tmp/lora_cache
    cache_dir = config.LORA_CACHE_DIR
    try:
        os.makedirs(cache_dir, exist_ok=True)
    except OSError:
        cache_dir = "/tmp/lora_cache"
        os.makedirs(cache_dir, exist_ok=True)

    return snapshot_download(
        repo_id=repo,
        token=tok,
        allow_patterns=["adapter_model.safetensors", "adapter_model.bin", "adapter_config.json", "system_prompt.txt"],
        cache_dir=cache_dir,
    )


def _model_context_tokens(model: Any, tokenizer: Any) -> int | None:
    """Find physical context capacity; ignore tokenizer sentinel 'infinite' values."""
    cfg = getattr(model, "config", None)
    text_cfg = getattr(cfg, "text_config", None)
    candidates = [
        getattr(cfg, "max_position_embeddings", None),
        getattr(text_cfg, "max_position_embeddings", None),
        getattr(tokenizer, "model_max_length", None),
    ]
    valid = [int(n) for n in candidates if isinstance(n, int) and 1024 <= n < 10_000_000]
    return min(valid) if valid else None


def _per_chunk_token_budget(model: Any = None, tokenizer: Any = None) -> int:
    """Calculate safe per-inference prompt token budget including output reservation and margin."""
    budget = int(config.MAX_INPUT_TOKENS) + 600
    context = _model_context_tokens(model, tokenizer) if (model is not None or tokenizer is not None) else None
    if context is not None:
        budget = min(budget, context - int(config.MAX_NEW_TOKENS) - 128)
    if budget < 256:
        raise ValueError("No usable prompt budget: check MAX_INPUT_TOKENS, MAX_NEW_TOKENS and model context window")
    return budget


def initialize_model(force: bool = False) -> dict[str, Any]:
    """Initialize tokenizer, 4-bit base model, and LoRA adapter with detailed startup diagnostics."""
    if _MODEL_STATE["initialized"] and not force:
        return _MODEL_STATE

    t0 = time.time()
    try:
        import torch
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

        if torch.cuda.is_available():
            dev_name = torch.cuda.get_device_name(0)
            vram_gb = round(torch.cuda.get_device_properties(0).total_memory / (1024**3), 2)
            print(f"GPU detected: {dev_name} ({vram_gb} GB VRAM)", flush=True)
        else:
            print("WARNING: CUDA is not available. 4-bit NF4 model loading requires an NVIDIA GPU.", flush=True)

        base_path = resolve_base_path(MODEL_NAME)
        print("Base model source:", base_path, flush=True)
        hf_tok = _get_hf_token()

        tokenizer = AutoTokenizer.from_pretrained(base_path, token=hf_tok)
        if tokenizer.pad_token is None:
            tokenizer.pad_token = tokenizer.eos_token

        bnb_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True,
        )
        base_model = AutoModelForCausalLM.from_pretrained(
            base_path,
            quantization_config=bnb_config,
            device_map={"": 0},
            torch_dtype=torch.bfloat16,
            token=hf_tok,
        )

        lora_path = resolve_lora_path()
        model = PeftModel.from_pretrained(base_model, lora_path)
        model.eval()

        sys_prompt = config.SYSTEM_PROMPT
        sp_file = os.path.join(lora_path, "system_prompt.txt")
        if os.path.isfile(sp_file):
            with open(sp_file, encoding="utf-8") as f:
                saved = f.read().strip()
            if saved and saved != config.SYSTEM_PROMPT.strip():
                print("NOTE: adapter system_prompt.txt overrides config.py to match training.", flush=True)
                sys_prompt = saved

        budget = _per_chunk_token_budget(model, tokenizer)
        elapsed = round(time.time() - t0, 2)

        _MODEL_STATE.update(
            {
                "initialized": True,
                "error": None,
                "tokenizer": tokenizer,
                "model": model,
                "base_path": base_path,
                "lora_path": lora_path,
                "system_prompt": sys_prompt,
                "prompt_token_budget": budget,
                "init_seconds": elapsed,
            }
        )
        print(f"Model ready. (initialized in {elapsed}s, prompt_token_budget={budget})", flush=True)
        return _MODEL_STATE
    except Exception as exc:
        _MODEL_STATE["initialized"] = False
        _MODEL_STATE["error"] = f"{type(exc).__name__}: {exc}"
        print(f"ERROR during model initialization: {_MODEL_STATE['error']}", flush=True)
        raise


def _format_prompt(user_text: str) -> str:
    sys_prompt = _MODEL_STATE.get("system_prompt") or config.SYSTEM_PROMPT
    tokenizer = _MODEL_STATE.get("tokenizer")
    msgs = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_text}]
    if tokenizer is not None and hasattr(tokenizer, "apply_chat_template"):
        return tokenizer.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
    # Fallback chat template calculation when running unit tests without loaded tokenizer
    return f"<s>[INST] {sys_prompt}\n\n{user_text} [/INST]"


def _count_input_tokens(user_text: str) -> int:
    """Count the exact complete prompt passed to generate_once: system + metadata + pages."""
    prompt = _format_prompt(user_text)
    tokenizer = _MODEL_STATE.get("tokenizer")
    if tokenizer is not None:
        return len(tokenizer(prompt, add_special_tokens=True)["input_ids"])
    # Conservative token estimate for Arabic + JSON when tokenizer is mocked/offline (~2.2 chars/token)
    return max(1, int(len(prompt) / 2.2))


def _split_page_in_half(text: str) -> tuple[str, str]:
    """Split a single too-token-heavy page near the middle; keep a small context overlap."""
    if len(text) < 2:
        raise ValueError("Cannot split input further: metadata/system prompt exceeds token budget")
    middle = len(text) // 2
    lo = max(1, middle - min(350, middle // 3))
    hi = min(len(text) - 1, middle + min(350, middle // 3))
    boundaries = [text.rfind("\n\n", lo, hi), text.rfind("\n", lo, hi), text.rfind(" ", lo, hi)]
    cut = next((p for p in boundaries if p > 0), middle)
    overlap = min(180, len(text) // 20)
    left = text[: min(len(text) - 1, cut + overlap)].strip()
    right = text[max(1, cut - overlap) :].strip()
    if not left or not right or left == text or right == text:
        left, right = text[:middle], text[middle:]
    return left, right


def _split_chunk_to_token_budget(
    chunk_pages: list[tuple[int, str]], meta_block: str, budget: int
) -> list[list[tuple[int, str]]]:
    """Only split *after* normal page chunking if complete prompt exceeds the token budget."""
    pending = [chunk_pages]
    ready: list[list[tuple[int, str]]] = []
    while pending:
        ch = pending.pop()
        if _count_input_tokens(meta_block + pp.render(ch)) <= budget:
            ready.append(ch)
            continue

        if len(ch) > 1:
            mid = len(ch) // 2
            a, b = ch[:mid], ch[mid:]
        else:
            page_no, page_text = ch[0]
            a_text, b_text = _split_page_in_half(page_text)
            a, b = [(page_no, a_text)], [(page_no, b_text)]

        pending.extend([b, a])
    return ready


def generate_once(user_text: str, rp: float) -> tuple[str, int, int, str]:
    import torch
    from transformers import StoppingCriteria, StoppingCriteriaList

    class RepeatLoopStop(StoppingCriteria):
        """Stop generation on repeated short token sequences."""

        def __init__(self, prompt_len: int, window: int = 96, max_period: int = 12, check_every: int = 16):
            self.prompt_len = prompt_len
            self.window = window
            self.max_period = max_period
            self.check_every = check_every
            self.triggered = False

        def __call__(self, input_ids: Any, scores: Any, **kwargs: Any) -> Any:
            n = input_ids.shape[1] - self.prompt_len
            hit = False
            if n >= self.window and n % self.check_every == 0:
                tail = input_ids[0, -self.window :].tolist()
                for p in range(1, self.max_period + 1):
                    if all(tail[i] == tail[i - p] for i in range(p, len(tail))):
                        hit = True
                        break
                if hit:
                    self.triggered = True
            return torch.full((input_ids.shape[0],), hit, dtype=torch.bool, device=input_ids.device)

    if not _MODEL_STATE["initialized"]:
        initialize_model()

    tokenizer = _MODEL_STATE["tokenizer"]
    model = _MODEL_STATE["model"]
    budget = _MODEL_STATE["prompt_token_budget"]

    prompt = _format_prompt(user_text)
    inputs = tokenizer(prompt, return_tensors="pt").to(model.device)
    n_in = int(inputs["input_ids"].shape[1])
    if n_in > budget:
        raise ValueError(f"Internal chunking error: {n_in} tokens > {budget} token budget")

    stopper = RepeatLoopStop(n_in)
    with torch.no_grad():
        out = model.generate(
            **inputs,
            max_new_tokens=config.MAX_NEW_TOKENS,
            do_sample=False,
            repetition_penalty=rp,
            pad_token_id=tokenizer.pad_token_id,
            stopping_criteria=StoppingCriteriaList([stopper]),
        )
    gen = out[0][n_in:]
    text = tokenizer.decode(gen, skip_special_tokens=True)
    n = int(gen.shape[0])
    reason = "repetition_loop" if stopper.triggered else ("max_new_tokens" if n >= config.MAX_NEW_TOKENS else "eos")
    return text, n, n_in, reason


def run_chunk(user_text: str) -> tuple[str, dict[str, Any]]:
    text, n, n_in, reason = generate_once(user_text, config.REPETITION_PENALTY)
    retried = False
    if reason == "repetition_loop" and config.REPETITION_PENALTY < 1.1 and config.MAX_CHUNK_RETRIES > 0:
        retried = True
        t2, n2, _, r2 = generate_once(user_text, 1.1)
        if r2 != "repetition_loop":
            text, n, reason = t2, n2, r2
    return text, {"input_tokens": n_in, "generated_tokens": n, "stop_reason": reason, "retried": retried}


def _identify_non_card_pages(pages: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Return pages that are NOT already covered by deterministic structural program/project cards
    (or summary TOC/budget tables of those exact cards) so the LLM only runs on unstructured narrative pages.
    """
    card_pages = pp.extract_structured_cards(pages)
    if not card_pages:
        return pages

    # If the document is a structured operational plan with >= 5 cards, check which pages are free-form narrative
    unstructured: list[tuple[int, str]] = []
    for p_no, body in pages:
        if not body or not body.strip():
            continue
        # Card pages or continuation pages
        if pp.CARD_CODE_RE.search("\n".join(body.splitlines()[:6])):
            continue
        # TOC, overview, numbering guide, budget summary, or empty notes pages in operational plans
        if any(
            marker in body
            for marker in (
                "الفهرس",
                "الخطة في أرقام",
                "آلية الترقيم",
                "خارطة التنفيذ",
                "رحلة التنفيذ",
                "في هذا البرنامج",
                "في هذا القسم",
                "ملخص الموازنة التقديرية",
                "تكلفة المشاريع ومدد تنفيذها",
                "نموذج الموازنة الربعي",
                "الموازنة الشهرية",
                "المصاريف التشغيلية",
                "71ملاحظات",
                "ورقة عمل Excel:",
            )
        ):
            continue
        # Cover / back-cover pages with < 150 chars
        if len(body.strip()) < 160:
            continue
        unstructured.append((p_no, body))
    return unstructured


def extract_document(job_input: dict[str, Any]) -> dict[str, Any]:
    """Full end-to-end extraction pipeline:
      1. Ingest pasted text and/or binary attachments (PDF, XLSX, DOCX, MD, TXT).
      2. Extract metadata (association_name, year, period).
      3. Run deterministic structural card extraction + LLM chunk inference with bounded retry/splitting.
      4. Ground numeric fields, filter non-entities, enrich descriptions, and format contract response.
    """
    enable_ocr = bool(job_input.get("enable_ocr", config.ENABLE_OCR))
    ingested = ingestion.ingest_request_inputs(job_input, enable_ocr=enable_ocr)
    raw_text = ingested["text"]
    stripped_text, meta_in = pp.strip_metadata(raw_text)

    # Re-split pages after stripping metadata block if present
    pages = pp.split_pages(stripped_text) if stripped_text != raw_text else ingested["pages"]
    doc_meta = pp.detect_document_metadata(pages)

    def pick(key: str, ar_key: str) -> Any:
        v = job_input.get(key)
        if v in (None, ""):
            v = meta_in.get(ar_key)
            if v in ("غير مذكور", "غير مذكورة", "تقرير سنوي"):
                v = None
        if v in (None, ""):
            v = doc_meta.get(key)
        return v

    assoc = pick("association_name", "اسم الجمعية")
    if isinstance(assoc, str):
        assoc = re.sub(r"\s*التقرير\s*(السنوي)?\s*\d{0,4}\s*$", "", assoc).strip()
    year = pick("year", "سنة التقرير")
    period = pick("period", "الفترة")
    if isinstance(year, str) and year.strip().isdigit():
        year = int(year.strip())

    # Determine which pages require LLM inference vs. deterministic structural extraction
    skip_llm_cards = bool(job_input.get("skip_llm_for_structured_cards", config.SKIP_LLM_FOR_STRUCTURED_CARD_PAGES))
    pages_for_llm = _identify_non_card_pages(pages) if skip_llm_cards else pages

    per_chunk_lists: list[list[dict[str, Any]]] = []
    review: list[dict[str, str]] = []
    info: list[dict[str, Any]] = []
    repaired_any = False

    if pages_for_llm:
        budget = _MODEL_STATE.get("prompt_token_budget") or _per_chunk_token_budget()
        original_chunks = pp.build_chunks(pages_for_llm, config.CHUNK_TARGET_CHARS, config.CHUNK_OVERLAP_PAGES)
        meta_block = pp.build_meta_block(assoc, year, period)

        chunks: list[tuple[int, list[tuple[int, str]]]] = []
        for original_index, ch in enumerate(original_chunks, 1):
            for fitted in _split_chunk_to_token_budget(ch, meta_block, budget):
                chunks.append((original_index, fitted))

        for k, (original_index, ch) in enumerate(chunks, 1):
            body = pp.render(ch)
            t0 = time.time()
            raw, gi = run_chunk(meta_block + body)
            obj, repaired = pp.parse_json_output(raw)
            repaired_any = repaired_any or repaired

            # Bounded adaptive sub-splitting if generation truncated (`max_new_tokens`) or failed JSON parse
            if (obj is None or gi["stop_reason"] == "max_new_tokens") and len(ch) > 1 and config.MAX_CHUNK_RETRIES > 0:
                mid = len(ch) // 2
                sub_progs: list[dict[str, Any]] = []
                sub_ok = True
                for sub_ch in (ch[:mid], ch[mid:]):
                    sub_body = pp.render(sub_ch)
                    sub_raw, _ = run_chunk(meta_block + sub_body)
                    sub_obj, sub_rep = pp.parse_json_output(sub_raw)
                    repaired_any = repaired_any or sub_rep
                    if isinstance(sub_obj, dict) and isinstance(sub_obj.get("programs_and_projects"), list):
                        sub_tn = pp.norm(sub_body)
                        for p in sub_obj["programs_and_projects"]:
                            cp = pp.clean_program(p, sub_tn, review, chunk_raw_text=sub_body)
                            if cp:
                                sub_progs.append(cp)
                    else:
                        sub_ok = False
                if sub_ok:
                    obj = {"programs_and_projects": sub_progs}
                    gi["adaptive_split_recovered"] = True
                    gi["stop_reason"] = "eos"

            gi.update(
                {
                    "chunk": k,
                    "source_chunk": original_index,
                    "pages": [ch[0][0], ch[-1][0]],
                    "seconds": round(time.time() - t0, 1),
                }
            )
            progs: list[dict[str, Any]] = []
            if isinstance(obj, dict) and isinstance(obj.get("programs_and_projects"), list):
                tn = pp.norm(body)
                for p in obj["programs_and_projects"]:
                    cp = pp.clean_program(p, tn, review, chunk_raw_text=body)
                    if cp:
                        progs.append(cp)
            else:
                gi["parse_error"] = True
                gi["raw_output"] = (raw or "")[:2000]
            gi["programs"] = len(progs)
            info.append(gi)
            per_chunk_lists.append(progs)

    merged_model_entities = pp.merge_programs(per_chunk_lists, config.MERGE_NAME_THRESHOLD)
    final_entities = pp.enrich_and_reconcile_entities(
        merged_model_entities,
        pages,
        default_year=year if isinstance(year, int) else None,
    )

    needs_attention = any(c.get("parse_error") or c.get("stop_reason") != "eos" for c in info)
    return pp.build_contract_response(
        entities=final_entities,
        association_name=assoc,
        year=year if isinstance(year, int) else None,
        period=period or None,
        run_id=job_input.get("run_id"),
        review=review,
        chunks_info=info,
        sources=ingested.get("sources", []),
        json_repaired=repaired_any,
        needs_attention=needs_attention,
    )


def health_check(load_model: bool = False) -> dict[str, Any]:
    """Return container and model readiness status for RunPod startup/health diagnostics."""
    cuda_available = False
    gpu_name = None
    try:
        import torch

        cuda_available = bool(torch.cuda.is_available())
        if cuda_available:
            gpu_name = torch.cuda.get_device_name(0)
    except ImportError:
        pass

    if load_model and not _MODEL_STATE["initialized"]:
        initialize_model()

    return {
        "status": "ready" if _MODEL_STATE["initialized"] else "container_ready",
        "model_initialized": _MODEL_STATE["initialized"],
        "model_id": MODEL_NAME,
        "base_path": _MODEL_STATE["base_path"],
        "lora_path": _MODEL_STATE["lora_path"],
        "prompt_token_budget": _MODEL_STATE["prompt_token_budget"],
        "init_seconds": _MODEL_STATE["init_seconds"],
        "cuda_available": cuda_available,
        "gpu_name": gpu_name,
        "error": _MODEL_STATE["error"],
    }


def handler(job: dict[str, Any]) -> dict[str, Any]:
    """RunPod Serverless entry point."""
    job_input = job.get("input", {}) if isinstance(job, dict) else {}
    if isinstance(job_input, str):
        try:
            job_input = json.loads(job_input)
        except (ValueError, TypeError):
            return {"error": "Invalid JSON string in job['input']"}

    if not isinstance(job_input, dict):
        return {"error": "job['input'] must be a JSON object"}

    # Support explicit health/readiness check requests
    action = str(job_input.get("action") or job_input.get("type") or "").strip().lower()
    if action in ("health", "health_check", "ping", "warmup"):
        try:
            return health_check(load_model=(action == "warmup"))
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "error": str(exc)}

    try:
        return extract_document(job_input)
    except ingestion.IngestionError as exc:
        return {"error": str(exc), "error_type": "validation_error", "run_id": job_input.get("run_id")}
    except Exception as exc:  # noqa: BLE001
        return {
            "error": str(exc),
            "error_type": type(exc).__name__,
            "run_id": job_input.get("run_id"),
            "traceback": traceback.format_exc()[-1500:],
        }


if __name__ == "__main__":
    import runpod

    if os.getenv("PRELOAD_MODEL_ON_START", "1").strip().lower() not in ("0", "false", "no"):
        try:
            initialize_model()
        except Exception as startup_exc:  # noqa: BLE001
            print(f"Startup model preload failed (will retry on request): {startup_exc}", flush=True)

    runpod.serverless.start({"handler": handler})
