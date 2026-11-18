"""
RunPod Serverless handler - extracting NGO programs/projects.
Mistral Small 24B 4-bit + LoRA v3.

Whole-document length has no application-level cap: document pages are split
into ~10,000-char chunks with page overlap and, only if necessary, further
split to fit a SINGLE inference call's token budget. The model's finite
context window and RunPod payload/execution limits still apply.
"""
import sys

sys.modules["torchvision"] = None  # Must come before transformers.

import json
import os
import re
import time

import runpod
import torch
from huggingface_hub import snapshot_download
from peft import PeftModel
from transformers import (AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig,
                          StoppingCriteria, StoppingCriteriaList)

import config
import postprocess as pp

MODEL_NAME = os.getenv("MODEL_NAME", config.MODEL_ID)
HF_CACHE_ROOT = os.getenv("HF_CACHE_ROOT", "/runpod-volume/huggingface-cache/hub")


def resolve_base_path(model_id: str) -> str:
    org, name = model_id.split("/", 1)
    root = os.path.join(HF_CACHE_ROOT, f"models--{org}--{name}")
    snaps = os.path.join(root, "snapshots")
    refs_main = os.path.join(root, "refs", "main")
    if os.path.isfile(refs_main):
        cand = os.path.join(snaps, open(refs_main).read().strip())
        if os.path.isdir(cand):
            return cand
    if os.path.isdir(snaps):
        versions = sorted(d for d in os.listdir(snaps) if os.path.isdir(os.path.join(snaps, d)))
        if versions:
            return os.path.join(snaps, versions[0])
    print("WARNING: Base model not found in RunPod cache - downloading from Hugging Face.")
    return model_id


BASE_PATH = resolve_base_path(MODEL_NAME)
print("Base model source:", BASE_PATH)

tokenizer = AutoTokenizer.from_pretrained(BASE_PATH)
if tokenizer.pad_token is None:
    tokenizer.pad_token = tokenizer.eos_token

bnb_config = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                                bnb_4bit_compute_dtype=torch.bfloat16, bnb_4bit_use_double_quant=True)
base_model = AutoModelForCausalLM.from_pretrained(BASE_PATH, quantization_config=bnb_config,
                                                  device_map={"": 0}, torch_dtype=torch.bfloat16)


def resolve_lora_path() -> str:
    if config.LORA_PATH and os.path.exists(os.path.join(config.LORA_PATH, "adapter_config.json")):
        return config.LORA_PATH
    print("Downloading LoRA from Hugging Face:", config.LORA_REPO)
    tok = (os.getenv("HF_TOKEN") or os.getenv("HUGGING_FACE_HUB_TOKEN") or "").strip().strip("\"'").strip()
    repo = (config.LORA_REPO or "").strip().strip("\"'")
    print(f"HF_TOKEN present={bool(tok)} len={len(tok)} prefix={tok[:3]!r} repo={repo!r}")
    return snapshot_download(
        repo_id=repo, token=tok or None,
        allow_patterns=["adapter_model.safetensors", "adapter_config.json", "system_prompt.txt"],
        cache_dir=os.getenv("LORA_CACHE_DIR", "/tmp/lora_cache"))


lora_path = resolve_lora_path()
model = PeftModel.from_pretrained(base_model, lora_path)
model.eval()

SYSTEM_PROMPT = config.SYSTEM_PROMPT
_sp = os.path.join(lora_path, "system_prompt.txt")
if os.path.isfile(_sp):
    saved = open(_sp, encoding="utf-8").read().strip()
    if saved and saved != config.SYSTEM_PROMPT.strip():
        print("NOTE: adapter system_prompt.txt overrides config.py to match training.")
        SYSTEM_PROMPT = saved
print("Model ready.")


def _model_context_tokens():
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


def _per_chunk_token_budget():
    """A safety budget for ONE inference, not a limit on document length.

    Retain the former MAX_INPUT_TOKENS + 600 ceiling for similar inference
    behavior on the trained model; never exceed its physical context once
    the expected generated output and safety margin are included.
    """
    budget = int(config.MAX_INPUT_TOKENS) + 600
    context = _model_context_tokens()
    if context is not None:
        budget = min(budget, context - int(config.MAX_NEW_TOKENS) - 128)
    if budget < 256:
        raise ValueError(
            "No usable prompt budget: check MAX_INPUT_TOKENS, MAX_NEW_TOKENS and model context window"
        )
    return budget


PROMPT_TOKEN_BUDGET = _per_chunk_token_budget()
print("Per-inference input token budget:", PROMPT_TOKEN_BUDGET)


def _format_prompt(user_text: str):
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_text}]
    return tokenizer.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)


def _count_input_tokens(user_text: str) -> int:
    """Count the SAME complete prompt passed to generate_once: system + metadata + pages."""
    prompt = _format_prompt(user_text)
    return len(tokenizer(prompt, add_special_tokens=True)["input_ids"])


def _split_page_in_half(text: str):
    """Split a single too-token-heavy page near the middle; small context overlap."""
    if len(text) < 2:
        raise ValueError("Cannot split input further: metadata/system prompt exceeds token budget")
    middle = len(text) // 2
    # Prefer a natural separator close to the middle; always make progress.
    lo = max(1, middle - min(350, middle // 3))
    hi = min(len(text) - 1, middle + min(350, middle // 3))
    boundaries = [text.rfind("\n\n", lo, hi), text.rfind("\n", lo, hi), text.rfind(" ", lo, hi)]
    cut = next((p for p in boundaries if p > 0), middle)
    overlap = min(180, len(text) // 20)
    left = text[:min(len(text) - 1, cut + overlap)].strip()
    right = text[max(1, cut - overlap):].strip()
    if not left or not right or left == text or right == text:
        left, right = text[:middle], text[middle:]
    return left, right


def _split_chunk_to_token_budget(chunk_pages, meta_block, budget):
    """Only split *after* normal page chunking if complete prompt is oversized.

    Normal documents keep their original chunk boundaries and training format.
    Each returned subchunk is a list of (page_number, body) tuples.
    """
    pending = [chunk_pages]
    ready = []
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

        # Stack order is reversed so output remains in the document's order.
        pending.extend([b, a])
    return ready


class RepeatLoopStop(StoppingCriteria):
    """Stop generation on repeated short token sequences."""

    def __init__(self, prompt_len, window=96, max_period=12, check_every=16):
        self.prompt_len, self.window, self.max_period, self.check_every = prompt_len, window, max_period, check_every
        self.triggered = False

    def __call__(self, input_ids, scores, **kwargs):
        n = input_ids.shape[1] - self.prompt_len
        hit = False
        if n >= self.window and n % self.check_every == 0:
            tail = input_ids[0, -self.window:].tolist()
            for p in range(1, self.max_period + 1):
                if all(tail[i] == tail[i - p] for i in range(p, len(tail))):
                    hit = True
                    break
            if hit:
                self.triggered = True
        return torch.full((input_ids.shape[0],), hit, dtype=torch.bool, device=input_ids.device)


def generate_once(user_text: str, rp: float):
    prompt = _format_prompt(user_text)
    inputs = tokenizer(prompt, return_tensors="pt").to(model.device)
    n_in = int(inputs["input_ids"].shape[1])
    # Internal assertion: caller must pre-split; never silently drop chunks.
    if n_in > PROMPT_TOKEN_BUDGET:
        raise ValueError(
            f"Internal chunking error: {n_in} tokens > {PROMPT_TOKEN_BUDGET} token budget"
        )
    stopper = RepeatLoopStop(n_in)
    with torch.no_grad():
        out = model.generate(**inputs, max_new_tokens=config.MAX_NEW_TOKENS, do_sample=False,
                             repetition_penalty=rp, pad_token_id=tokenizer.pad_token_id,
                             stopping_criteria=StoppingCriteriaList([stopper]))
    gen = out[0][n_in:]
    text = tokenizer.decode(gen, skip_special_tokens=True)
    n = int(gen.shape[0])
    reason = "repetition_loop" if stopper.triggered else ("max_new_tokens" if n >= config.MAX_NEW_TOKENS else "eos")
    return text, n, n_in, reason


def run_chunk(user_text: str):
    text, n, n_in, reason = generate_once(user_text, config.REPETITION_PENALTY)
    retried = False
    if reason == "repetition_loop" and config.REPETITION_PENALTY < 1.1:
        retried = True
        t2, n2, _, r2 = generate_once(user_text, 1.1)
        if r2 != "repetition_loop":
            text, n, reason = t2, n2, r2
    return text, {"input_tokens": n_in, "generated_tokens": n, "stop_reason": reason, "retried": retried}


def extract_document(job_input: dict) -> dict:
    text = job_input.get("text") or ""
    text, meta_in = pp.strip_metadata(text)

    def pick(key, ar_key):
        v = job_input.get(key)
        if v in (None, ""):
            v = meta_in.get(ar_key)
            if v in ("غير مذكور", "غير مذكورة", "تقرير سنوي"):
                v = None
        return v

    assoc = pick("association_name", "اسم الجمعية")
    if isinstance(assoc, str):
        assoc = re.sub(r"\s*التقرير\s*(السنوي)?\s*\d{0,4}\s*$", "", assoc).strip()
    year = pick("year", "سنة التقرير")
    period = pick("period", "الفترة")
    if isinstance(year, str) and year.strip().isdigit():
        year = int(year.strip())

    pages = pp.split_pages(text)
    original_chunks = pp.build_chunks(pages, config.CHUNK_TARGET_CHARS, config.CHUNK_OVERLAP_PAGES)
    meta_block = pp.build_meta_block(assoc, year, period)

    # Preflight is CPU-side and ensures no input_too_long empty output is created.
    chunks = []
    for original_index, ch in enumerate(original_chunks, 1):
        for fitted in _split_chunk_to_token_budget(ch, meta_block, PROMPT_TOKEN_BUDGET):
            chunks.append((original_index, fitted))

    per_chunk_lists, review, info, repaired_any = [], [], [], False
    for k, (original_index, ch) in enumerate(chunks, 1):
        body = pp.render(ch)
        t0 = time.time()
        raw, gi = run_chunk(meta_block + body)
        obj, repaired = pp.parse_json_output(raw)
        repaired_any = repaired_any or repaired
        gi.update({"chunk": k, "source_chunk": original_index,
                   "pages": [ch[0][0], ch[-1][0]], "seconds": round(time.time() - t0, 1)})
        progs = []
        if isinstance(obj, dict) and isinstance(obj.get("programs_and_projects"), list):
            tn = pp.norm(body)
            for p in obj["programs_and_projects"]:
                cp = pp.clean_program(p, tn, review)
                if cp:
                    progs.append(cp)
        else:
            gi["parse_error"] = True
            gi["raw_output"] = raw[:2000]
        gi["programs"] = len(progs)
        info.append(gi)
        per_chunk_lists.append(progs)

    merged = pp.merge_programs(per_chunk_lists, config.MERGE_NAME_THRESHOLD)
    for p in merged:
        if p.get("year") in (None, "") and year:
            p["year"] = year
    return {
        "result": {"association_name": assoc, "year": year, "period": period or None,
                   "programs_and_projects": merged},
        "review": review,
        "chunks": info,
        "json_repaired": repaired_any,
        "needs_attention": any(c.get("parse_error") or c["stop_reason"] != "eos" for c in info),
    }


def handler(job):
    job_input = job.get("input", {}) or {}
    if not job_input.get("text"):
        return {"error": "missing 'text' field in input"}
    try:
        return extract_document(job_input)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
