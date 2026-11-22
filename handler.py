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

import contextlib
import json
import os
import re
import threading
import time
import traceback
from collections.abc import Iterator
from typing import Any

import config
import ingestion
import postprocess as pp
import streaming

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


def _build_repeat_cancel_stopper(
    prompt_len: int,
    cancel_event: threading.Event | None = None,
) -> Any:
    """Create a Hugging Face StoppingCriteria stopping on repetition loops or cancellation."""
    import torch
    from transformers import StoppingCriteria

    class RepeatCancelStop(StoppingCriteria):
        """Stop generation on repeated short token sequences or external cancellation."""

        def __init__(self, n_prompt: int, window: int = 96, max_period: int = 12, check_every: int = 16):
            self.prompt_len = n_prompt
            self.window = window
            self.max_period = max_period
            self.check_every = check_every
            self.triggered = False
            self.cancelled = False
            self.generated_tokens = 0

        def __call__(self, input_ids: Any, scores: Any, **kwargs: Any) -> Any:
            n = int(input_ids.shape[1]) - self.prompt_len
            self.generated_tokens = max(0, n)
            if cancel_event is not None and cancel_event.is_set():
                self.cancelled = True
                return torch.full((input_ids.shape[0],), True, dtype=torch.bool, device=input_ids.device)

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

    return RepeatCancelStop(prompt_len)


def generate_once(user_text: str, rp: float) -> tuple[str, int, int, str]:
    import torch
    from transformers import StoppingCriteriaList

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

    stopper = _build_repeat_cancel_stopper(n_in)
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


def generate_stream(
    user_text: str,
    rp: float,
    cancel_event: threading.Event | None = None,
) -> Iterator[tuple[str | None, dict[str, Any] | None]]:
    """Stream decoded text deltas from GPU generation via `TextIteratorStreamer`."""
    import torch
    from transformers import StoppingCriteriaList, TextIteratorStreamer

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

    stop_event = cancel_event or threading.Event()
    stopper = _build_repeat_cancel_stopper(n_in, cancel_event=stop_event)
    streamer = TextIteratorStreamer(tokenizer, skip_prompt=True, skip_special_tokens=True)
    gen_error: list[BaseException] = []

    def _run_generate() -> None:
        try:
            with torch.no_grad():
                model.generate(
                    **inputs,
                    max_new_tokens=config.MAX_NEW_TOKENS,
                    do_sample=False,
                    repetition_penalty=rp,
                    pad_token_id=tokenizer.pad_token_id,
                    stopping_criteria=StoppingCriteriaList([stopper]),
                    streamer=streamer,
                )
        except BaseException as exc:  # noqa: BLE001
            gen_error.append(exc)
        finally:
            with contextlib.suppress(Exception):
                streamer.end()

    thread = threading.Thread(target=_run_generate, daemon=True)
    thread.start()
    try:
        for delta in streamer:
            if delta:
                yield delta, None
        thread.join(timeout=10)
        if gen_error:
            raise gen_error[0]
        n_gen = int(stopper.generated_tokens)
        reason = (
            "cancelled"
            if stopper.cancelled or stop_event.is_set()
            else (
                "repetition_loop"
                if stopper.triggered
                else ("max_new_tokens" if n_gen >= config.MAX_NEW_TOKENS else "eos")
            )
        )
        yield None, {"input_tokens": n_in, "generated_tokens": n_gen, "stop_reason": reason}
    finally:
        stop_event.set()
        thread.join(timeout=5)


def run_chunk_stream(
    user_text: str,
    cancel_event: threading.Event | None = None,
) -> Iterator[tuple[Any, ...]]:
    """Run one chunk with token streaming, yielding complete JSON entities as they finish.

    Yields:
      - `("item", entity_dict)` immediately as each element object finishes closing `}`.
      - `("chunk_meta", raw_text, gi_dict)` once generation of the chunk completes.
    """
    parser = streaming.IncrementalProgramArrayParser()
    meta: dict[str, Any] = {"input_tokens": 0, "generated_tokens": 0, "stop_reason": "eos"}

    for delta, gen_meta in generate_stream(user_text, config.REPETITION_PENALTY, cancel_event=cancel_event):
        if delta is not None:
            for item_obj in parser.feed(delta):
                yield ("item", item_obj)
        if gen_meta is not None:
            meta = dict(gen_meta)

    raw_text = parser.full_text
    retried = False
    if meta.get("stop_reason") == "repetition_loop" and config.REPETITION_PENALTY < 1.1 and config.MAX_CHUNK_RETRIES > 0:
        retried = True
        retry_parser = streaming.IncrementalProgramArrayParser()
        retry_meta: dict[str, Any] = dict(meta)
        for delta, gen_meta in generate_stream(user_text, 1.1, cancel_event=cancel_event):
            if delta is not None:
                for item_obj in retry_parser.feed(delta):
                    yield ("item", item_obj)
            if gen_meta is not None:
                retry_meta = dict(gen_meta)
        if retry_meta.get("stop_reason") != "repetition_loop":
            parser = retry_parser
            meta = retry_meta
            raw_text = retry_parser.full_text

    gi: dict[str, Any] = {
        "input_tokens": int(meta.get("input_tokens", 0)),
        "generated_tokens": int(meta.get("generated_tokens", 0)),
        "stop_reason": meta.get("stop_reason", "eos"),
        "retried": retried,
        "parser_state": parser.state,
        "streamed_count": parser.emitted_count,
        "parser_errors": parser.parse_errors,
    }
    yield ("chunk_meta", raw_text, gi)


def _filter_unstructured_card_pages(pages: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Filter out card pages, financial tables, and administrative boilerplates in card decks."""
    unstructured: list[tuple[int, str]] = []
    for p_no, body in pages:
        if not body or not body.strip():
            continue
        if pp.CARD_CODE_RE.search("\n".join(body.splitlines()[:6])):
            continue
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
        if len(body.strip()) < 160:
            continue
        unstructured.append((p_no, body))
    return unstructured


def _identify_non_card_pages(pages: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Return pages that are NOT already covered by deterministic structural program/project cards
    or structured Markdown report sections so the LLM only runs on unstructured narrative pages.
    """
    if pp.extract_structured_cards(pages):
        return _filter_unstructured_card_pages(pages)

    # Check if the document is a structured Markdown report with explicit ## / ### entity sections
    md_sections = pp.extract_markdown_report_sections(pages)
    if len(md_sections) >= 3:
        return []

    return pages


def _prepare_document_context(job_input: dict[str, Any]) -> dict[str, Any]:
    """Ingest request inputs, split pages, and extract top-level association/year/period metadata."""
    enable_ocr = bool(job_input.get("enable_ocr", config.ENABLE_OCR))
    ingested = ingestion.ingest_request_inputs(job_input, enable_ocr=enable_ocr)
    raw_text = ingested["text"]
    stripped_text, meta_in = pp.strip_metadata(raw_text)

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

    return {
        "ingested": ingested,
        "pages": pages,
        "assoc": assoc,
        "year": year if isinstance(year, int) else None,
        "period": period or None,
    }


def extract_document(job_input: dict[str, Any]) -> dict[str, Any]:
    """Full end-to-end synchronous extraction pipeline preserving legacy single-dict output."""
    ctx = _prepare_document_context(job_input)
    ingested = ctx["ingested"]
    pages = ctx["pages"]
    assoc = ctx["assoc"]
    year = ctx["year"]
    period = ctx["period"]

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
            if (obj is None or gi["stop_reason"] == "max_new_tokens") and len(ch) > 1 and config.MAX_CHUNK_RETRIES > 0:
                mid = len(ch) // 2
                sub_progs: list[dict[str, Any]] = []
                sub_ok = True
                sub_repaired = False
                for sub_ch in (ch[:mid], ch[mid:]):
                    sub_body = pp.render(sub_ch)
                    sub_raw, _ = run_chunk(meta_block + sub_body)
                    sub_obj, sub_rep = pp.parse_json_output(sub_raw)
                    sub_repaired = sub_repaired or sub_rep
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
                    repaired = sub_repaired
                    gi["adaptive_split_recovered"] = True
                    gi["stop_reason"] = "eos"

            repaired_any = repaired_any or repaired

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
        default_year=year,
        review=review,
    )

    needs_attention = any(c.get("parse_error") or c.get("stop_reason") != "eos" for c in info)
    return pp.build_contract_response(
        entities=final_entities,
        association_name=assoc,
        year=year,
        period=period,
        run_id=streaming.extract_run_id(job_input),
        review=review,
        chunks_info=info,
        sources=ingested.get("sources", []),
        json_repaired=repaired_any,
        needs_attention=needs_attention,
    )


def _stream_single_raw_item(
    raw_obj: Any,
    *,
    tracker: streaming.IncrementalEntityTracker,
    run_id: str | None,
    chunk_index: int | None,
    item_index: int,
    already_validated: bool = False,
) -> dict[str, Any] | None:
    """Validate one candidate entity or emit an `item_failed` event if invalid."""
    try:
        evt, status = tracker.process_candidate(raw_obj, already_validated=already_validated)
    except Exception as exc:  # noqa: BLE001
        tracker.failed_items_count += 1
        err_id = streaming.make_failure_event_id(
            run_id,
            chunk_index,
            item_index,
            raw_obj.get("name") if isinstance(raw_obj, dict) else None,
            "item_extraction_error",
        )
        return streaming.make_item_failed_event(
            run_id,
            err_id,
            f"Item validation error: {exc}",
            item_name=str(raw_obj.get("name")) if isinstance(raw_obj, dict) and raw_obj.get("name") else None,
            chunk_index=chunk_index,
            reason_code="item_extraction_error",
        )

    if evt is not None:
        return evt
    if status and status.startswith("invalid:"):
        tracker.failed_items_count += 1
        raw_name = raw_obj.get("name") if isinstance(raw_obj, dict) else None
        err_id = streaming.make_failure_event_id(
            run_id,
            chunk_index,
            item_index,
            str(raw_name) if raw_name else None,
            "validation_failed",
        )
        return streaming.make_item_failed_event(
            run_id,
            err_id,
            f"Extracted item failed validation ({status})",
            item_name=str(raw_name) if raw_name else None,
            chunk_index=chunk_index,
            reason_code="validation_failed",
        )
    return None


def _run_streaming_llm_unit(
    prompt_text: str,
    *,
    tracker: streaming.IncrementalEntityTracker,
    run_id: str | None,
    chunk_index: int,
    cancel_event: threading.Event,
    allow_repair_fallback: bool = True,
) -> Iterator[tuple[str, Any]]:
    """Run one `run_chunk_stream` unit, yielding `("event", ev)` immediately per entity
    and finally `("summary", chunk_raw_text, gi, repaired, chunk_ok)`.
    """
    raw_item_idx = 0
    chunk_raw_text = ""
    gi: dict[str, Any] = {}

    for msg in run_chunk_stream(prompt_text, cancel_event=cancel_event):
        kind = msg[0]
        if kind == "item":
            raw_item_idx += 1
            ev = _stream_single_raw_item(
                msg[1],
                tracker=tracker,
                run_id=run_id,
                chunk_index=chunk_index,
                item_index=raw_item_idx,
                already_validated=False,
            )
            if ev is not None:
                yield ("event", ev)
        elif kind == "chunk_meta":
            chunk_raw_text = msg[1]
            gi = dict(msg[2])

    repaired = False
    chunk_ok = True
    needs_fallback = (
        gi.get("parser_errors", 0) > 0
        or gi.get("parser_state") != "DONE"
        or gi.get("streamed_count", 0) == 0
    )
    if needs_fallback:
        if gi.get("stop_reason") == "max_new_tokens" and not allow_repair_fallback:
            chunk_ok = False
        else:
            obj, repaired = pp.parse_json_output(chunk_raw_text)
            if isinstance(obj, dict) and isinstance(obj.get("programs_and_projects"), list):
                for p in obj["programs_and_projects"]:
                    raw_item_idx += 1
                    ev = _stream_single_raw_item(
                        p,
                        tracker=tracker,
                        run_id=run_id,
                        chunk_index=chunk_index,
                        item_index=raw_item_idx,
                        already_validated=False,
                    )
                    if ev is not None:
                        yield ("event", ev)
            else:
                chunk_ok = False

    yield ("summary", chunk_raw_text, gi, repaired, chunk_ok)


def extract_document_stream(job_input: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Incremental generator emitting each validated program/project as soon as it finishes."""
    run_id = streaming.extract_run_id(job_input)
    review: list[dict[str, str]] = []
    info: list[dict[str, Any]] = []
    repaired_any = False
    tracker: streaming.IncrementalEntityTracker | None = None
    cancel_event = threading.Event()
    emit_progress = config.STREAM_EMIT_PROGRESS

    start_time = time.monotonic()
    deadline = start_time + max(30, int(config.EXECUTION_TIMEOUT_SECONDS) - int(config.STREAM_DEADLINE_BUFFER_SECONDS))

    def _check_deadline() -> None:
        if time.monotonic() > deadline:
            raise TimeoutError(f"Extraction exceeded execution deadline ({config.EXECUTION_TIMEOUT_SECONDS}s)")

    try:
        ctx = _prepare_document_context(job_input)
        ingested = ctx["ingested"]
        pages = ctx["pages"]
        assoc = ctx["assoc"]
        year = ctx["year"]
        period = ctx["period"]

        tracker = streaming.IncrementalEntityTracker(
            run_id=run_id,
            pages=pages,
            default_year=year,
            review=review,
        )

        if emit_progress:
            yield streaming.make_progress_event(
                run_id,
                "ingested",
                tracker.total_items,
                tracker.programs_count,
                tracker.projects_count,
            )

        skip_llm_cards = bool(
            job_input.get("skip_llm_for_structured_cards", config.SKIP_LLM_FOR_STRUCTURED_CARD_PAGES)
        )
        pages_for_llm: list[tuple[int, str]] = []

        if skip_llm_cards:
            card_iter = pp.iter_structured_cards(pages, default_year=year)
            first_card = next(card_iter, None)
            if first_card is not None:
                if emit_progress:
                    yield streaming.make_progress_event(
                        run_id,
                        "extracting",
                        tracker.total_items,
                        tracker.programs_count,
                        tracker.projects_count,
                    )
                for idx, card_ent in enumerate(
                    _chain_first(first_card, card_iter),
                    1,
                ):
                    _check_deadline()
                    ev = _stream_single_raw_item(
                        card_ent,
                        tracker=tracker,
                        run_id=run_id,
                        chunk_index=None,
                        item_index=idx,
                        already_validated=True,
                    )
                    if ev is not None:
                        yield ev
                pages_for_llm = _filter_unstructured_card_pages(pages)
            else:
                md_iter = pp.iter_markdown_report_sections(pages, default_year=year)
                peeked: list[dict[str, Any]] = []
                for _ in range(3):
                    sec = next(md_iter, None)
                    if sec is None:
                        break
                    peeked.append(sec)

                if len(peeked) >= 3:
                    if emit_progress:
                        yield streaming.make_progress_event(
                            run_id,
                            "extracting",
                            tracker.total_items,
                            tracker.programs_count,
                            tracker.projects_count,
                        )
                    for idx, md_ent in enumerate(_chain_list_and_iter(peeked, md_iter), 1):
                        _check_deadline()
                        ev = _stream_single_raw_item(
                            md_ent,
                            tracker=tracker,
                            run_id=run_id,
                            chunk_index=None,
                            item_index=idx,
                            already_validated=True,
                        )
                        if ev is not None:
                            yield ev
                    pages_for_llm = []
                else:
                    tracker.reference_entities = peeked
                    pages_for_llm = pages
        else:
            pages_for_llm = pages

        if pages_for_llm:
            budget = _MODEL_STATE.get("prompt_token_budget") or _per_chunk_token_budget()
            original_chunks = pp.build_chunks(pages_for_llm, config.CHUNK_TARGET_CHARS, config.CHUNK_OVERLAP_PAGES)
            meta_block = pp.build_meta_block(assoc, year, period)

            chunks: list[tuple[int, list[tuple[int, str]]]] = []
            for original_index, ch in enumerate(original_chunks, 1):
                for fitted in _split_chunk_to_token_budget(ch, meta_block, budget):
                    chunks.append((original_index, fitted))

            if emit_progress:
                yield streaming.make_progress_event(
                    run_id,
                    "extracting",
                    tracker.total_items,
                    tracker.programs_count,
                    tracker.projects_count,
                    total_chunks=len(chunks),
                )

            for k, (original_index, ch) in enumerate(chunks, 1):
                _check_deadline()
                body = pp.render(ch)
                t0 = time.time()
                before_count = tracker.total_items

                gi: dict[str, Any] = {}
                repaired = False
                chunk_ok = True
                can_split_further = len(ch) > 1 and config.MAX_CHUNK_RETRIES > 0
                for tag, *rest in _run_streaming_llm_unit(
                    meta_block + body,
                    tracker=tracker,
                    run_id=run_id,
                    chunk_index=k,
                    cancel_event=cancel_event,
                    allow_repair_fallback=not can_split_further,
                ):
                    if tag == "event":
                        yield rest[0]
                    elif tag == "summary":
                        _, gi, repaired, chunk_ok = rest

                need_adaptive = (not chunk_ok or gi.get("stop_reason") == "max_new_tokens") and len(ch) > 1
                if need_adaptive and config.MAX_CHUNK_RETRIES > 0:
                    mid = len(ch) // 2
                    sub_ok_all = True
                    sub_repaired = False
                    for sub_ch in (ch[:mid], ch[mid:]):
                        _check_deadline()
                        sub_ok = True
                        sub_rep = False
                        for tag, *rest in _run_streaming_llm_unit(
                            meta_block + pp.render(sub_ch),
                            tracker=tracker,
                            run_id=run_id,
                            chunk_index=k,
                            cancel_event=cancel_event,
                        ):
                            if tag == "event":
                                yield rest[0]
                            elif tag == "summary":
                                sub_rep, sub_ok = rest[2], rest[3]
                        sub_repaired = sub_repaired or sub_rep
                        if not sub_ok:
                            sub_ok_all = False
                    if sub_ok_all:
                        chunk_ok = True
                        repaired = sub_repaired
                        gi["adaptive_split_recovered"] = True
                        gi["stop_reason"] = "eos"

                repaired_any = repaired_any or repaired
                emitted_in_chunk = tracker.total_items - before_count

                compact_gi: dict[str, Any] = {
                    "chunk": k,
                    "source_chunk": original_index,
                    "pages": [ch[0][0], ch[-1][0]],
                    "seconds": round(time.time() - t0, 1),
                    "input_tokens": gi.get("input_tokens", 0),
                    "generated_tokens": gi.get("generated_tokens", 0),
                    "stop_reason": gi.get("stop_reason", "eos"),
                    "retried": bool(gi.get("retried", False)),
                    "programs": emitted_in_chunk,
                }
                if gi.get("adaptive_split_recovered"):
                    compact_gi["adaptive_split_recovered"] = True
                if not chunk_ok:
                    compact_gi["parse_error"] = True
                    tracker.failed_items_count += 1
                    err_id = streaming.make_failure_event_id(run_id, k, None, None, "chunk_parse_error")
                    yield streaming.make_item_failed_event(
                        run_id,
                        err_id,
                        f"Chunk {k} failed JSON parsing",
                        chunk_index=k,
                        reason_code="chunk_parse_error",
                    )

                info.append(compact_gi)
                if emit_progress:
                    yield streaming.make_progress_event(
                        run_id,
                        "chunk_completed",
                        tracker.total_items,
                        tracker.programs_count,
                        tracker.projects_count,
                        chunk_index=k,
                        total_chunks=len(chunks),
                    )

            if chunks and all(c.get("parse_error") for c in info) and tracker.total_items == 0:
                raise streaming.StreamingExtractionError(
                    "All extraction chunks failed to produce valid JSON output",
                    error_type="ChunkParseError",
                    run_id=run_id,
                    completed_items=0,
                    partial_results_emitted=False,
                )

        # Emit any missed reference entities if LLM mode had <3 deterministic reference sections
        for idx, ref_ent in enumerate(tracker.reference_entities):
            if idx not in tracker.used_ref_indices:
                review.append(
                    {
                        "name": ref_ent["name"],
                        "reason": "تم استرجاع العنصر آليًا من هيكل المستند بعد إغفاله في التوليد النصي",
                    }
                )
                ev = _stream_single_raw_item(
                    ref_ent,
                    tracker=tracker,
                    run_id=run_id,
                    chunk_index=None,
                    item_index=idx + 1,
                    already_validated=True,
                )
                if ev is not None:
                    yield ev

        needs_attention = pp.compute_needs_attention(
            review,
            all_items=tracker.items,
            json_repaired=repaired_any,
            needs_attention=any(c.get("parse_error") or c.get("stop_reason") != "eos" for c in info),
            failed_items_count=tracker.failed_items_count,
        )

        yield streaming.make_completed_event(
            run_id,
            total_items=tracker.total_items,
            programs_count=tracker.programs_count,
            projects_count=tracker.projects_count,
            failed_items_count=tracker.failed_items_count,
            partial_failure=tracker.failed_items_count > 0,
            needs_attention=needs_attention,
            association_name=assoc,
            year=year,
            period=period,
            review=review,
            chunks=info,
            sources=ingested.get("sources", []),
            json_repaired=repaired_any,
        )
    except GeneratorExit:
        cancel_event.set()
        raise
    except streaming.StreamingExtractionError as exc:
        yield streaming.make_failed_event(
            run_id,
            error_message=str(exc),
            error_type=exc.error_type,
            completed_items=tracker.total_items if tracker else 0,
            programs_count=tracker.programs_count if tracker else 0,
            projects_count=tracker.projects_count if tracker else 0,
            partial_results_emitted=bool(tracker and tracker.total_items > 0),
        )
        raise
    except ingestion.IngestionError as exc:
        completed = tracker.total_items if tracker else 0
        yield streaming.make_failed_event(
            run_id,
            error_message=str(exc),
            error_type="validation_error",
            completed_items=completed,
            programs_count=tracker.programs_count if tracker else 0,
            projects_count=tracker.projects_count if tracker else 0,
            partial_results_emitted=completed > 0,
        )
        raise streaming.StreamingExtractionError(
            str(exc),
            error_type="validation_error",
            run_id=run_id,
            completed_items=completed,
            partial_results_emitted=completed > 0,
        ) from exc
    except Exception as exc:
        completed = tracker.total_items if tracker else 0
        yield streaming.make_failed_event(
            run_id,
            error_message=str(exc),
            error_type=type(exc).__name__,
            completed_items=completed,
            programs_count=tracker.programs_count if tracker else 0,
            projects_count=tracker.projects_count if tracker else 0,
            partial_results_emitted=completed > 0,
        )
        raise streaming.StreamingExtractionError(
            str(exc),
            error_type=type(exc).__name__,
            run_id=run_id,
            completed_items=completed,
            partial_results_emitted=completed > 0,
        ) from exc


def _chain_first(first: dict[str, Any], rest: Iterator[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    yield first
    yield from rest


def _chain_list_and_iter(prefix: list[dict[str, Any]], rest: Iterator[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    yield from prefix
    yield from rest


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


def _parse_job_input(job: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    raw_input = job.get("input", {}) if isinstance(job, dict) else {}
    if isinstance(raw_input, str):
        try:
            raw_input = json.loads(raw_input)
        except (ValueError, TypeError):
            return None, {"error": "Invalid JSON string in job['input']"}
    if not isinstance(raw_input, dict):
        return None, {"error": "job['input'] must be a JSON object"}
    return raw_input, None


def handler(job: dict[str, Any]) -> dict[str, Any]:
    """Synchronous RunPod / Python entry point returning one unified dict."""
    job_input, err = _parse_job_input(job)
    if err is not None or job_input is None:
        return err or {"error": "job['input'] must be a JSON object"}

    action = str(job_input.get("action") or job_input.get("type") or "").strip().lower()
    if action in ("health", "health_check", "ping", "warmup"):
        try:
            return health_check(load_model=(action == "warmup"))
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "error": str(exc)}

    run_id = streaming.extract_run_id(job_input)
    try:
        return extract_document(job_input)
    except ingestion.IngestionError as exc:
        return {"error": str(exc), "error_type": "validation_error", "run_id": run_id}
    except Exception as exc:  # noqa: BLE001
        return {
            "error": str(exc),
            "error_type": type(exc).__name__,
            "run_id": run_id,
            "traceback": traceback.format_exc()[-1500:],
        }


def stream_handler(job: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """RunPod Serverless generator entry point supporting opt-in incremental streaming."""
    job_input, err = _parse_job_input(job)
    if err is not None or job_input is None:
        msg = (err or {}).get("error", "Invalid request input")
        yield streaming.make_failed_event(
            None,
            error_message=msg,
            error_type="validation_error",
        )
        raise streaming.StreamingExtractionError(msg, error_type="validation_error")

    action = str(job_input.get("action") or job_input.get("type") or "").strip().lower()
    if action in ("health", "health_check", "ping"):
        try:
            hc = health_check(load_model=False)
        except Exception as exc:  # noqa: BLE001
            hc = {"status": "error", "error": str(exc)}
        # Prevent RunPod rp_job.handle_job from aborting a readiness check when _MODEL_STATE['error'] is set
        if hc.get("error") and hc.get("status") != "error":
            hc = dict(hc)
            hc["model_error"] = hc.pop("error")
            hc["error"] = None
        yield hc
        return

    if action == "warmup":
        try:
            yield health_check(load_model=True)
        except Exception as exc:  # noqa: BLE001
            yield {"status": "error", "error": str(exc)}
        return

    if not streaming.is_streaming_requested(job_input):
        yield handler({"input": job_input})
        return

    yield from extract_document_stream(job_input)


if __name__ == "__main__":
    import runpod

    if os.getenv("PRELOAD_MODEL_ON_START", "1").strip().lower() not in ("0", "false", "no"):
        try:
            initialize_model()
        except Exception as startup_exc:  # noqa: BLE001
            print(f"Startup model preload failed (will retry on request): {startup_exc}", flush=True)

    if config.RUNPOD_ENABLE_STREAMING:
        runpod.serverless.start(
            {
                "handler": stream_handler,
                "return_aggregate_stream": config.RETURN_AGGREGATE_STREAM,
            }
        )
    else:
        runpod.serverless.start({"handler": handler})
