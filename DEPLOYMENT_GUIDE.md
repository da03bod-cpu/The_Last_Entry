# Deployment Guide — Athar NGO Program & Project Extraction (RunPod Serverless)

This guide covers building the production Docker image, publishing to GitHub Container Registry (GHCR), and deploying a **brand-new RunPod Serverless endpoint** on an NVIDIA A40 GPU while leaving the old endpoint untouched until cutover verification succeeds.

---

## 1. Root Causes Identified in the Legacy Deployment

1. **Repeated 48GB Base Model Downloads & Cold-Start Timeouts**:
   - `handler.py` previously checked only a single hardcoded path (`/runpod-volume/huggingface-cache/hub/models--mistralai--Mistral-Small-24B-Instruct-2501`). When RunPod mounted its Cached Model under lowercase org/model names or under `/root/.cache/huggingface/hub` or `/runpod-volume/hf-cache/hub`, the path check failed and triggered a live download of `mistralai/Mistral-Small-24B-Instruct-2501` (~48GB) from Hugging Face on every worker cold start.
   - Downloading 48GB exceeded RunPod's worker initialization window, causing workers to be terminated before printing `Model ready.` and leaving queued requests stuck in `IN_QUEUE`.
2. **Ephemeral LoRA Cache**:
   - The LoRA adapter was downloaded to `/tmp/lora_cache` on every container start instead of preferring persistent volume storage (`/runpod-volume/lora-cache`).
3. **Hard Coupling of Container Startup & Model Load**:
   - Any transient Hugging Face network error at import time crashed the Python process before `runpod.serverless.start()` could register the worker.
4. **Missing Document Ingestion Libraries & Arabic OCR**:
   - The image lacked `pymupdf`, `openpyxl`, `Pillow`, `pytesseract`, and system packages `tesseract-ocr` + `tesseract-ocr-ara`, so it could only accept pre-extracted plain text (`job["input"]["text"]`).

---

## 2. Docker Image Build & GHCR Publishing

### Automated CI/CD (GitHub Actions)
The workflow in [`.github/workflows/build.yml`](file:///d:/bod/The_Last_Entry/.github/workflows/build.yml) runs automatically on pushes to `main` or `feat/**`:
1. **`test-and-lint` job**: Installs Python 3.11 dependencies, runs `ruff check` across all modules, and executes the 17-test CPU suite (`pytest -v -m "not gpu"`).
2. **`build` job**: Builds the Docker image and pushes versioned tags to GHCR:
   - `ghcr.io/da03bod-cpu/the_last_entry:latest`
   - `ghcr.io/da03bod-cpu/the_last_entry:v2.0.0`
   - `ghcr.io/da03bod-cpu/the_last_entry:sha-<7-char-commit>`
   - `ghcr.io/da03bod-cpu/the_last_entry:<full-commit-sha>`

### Manual Local Build & Push
```bash
docker build -t ghcr.io/da03bod-cpu/the_last_entry:v2.0.0 .
docker push ghcr.io/da03bod-cpu/the_last_entry:v2.0.0
```

---

## 3. Creating the NEW RunPod Serverless Endpoint (Step-by-Step)

> **Important**: Create a **NEW** endpoint in RunPod Console (`Serverless` -> `+ New Endpoint`). Do **not** modify or delete the existing endpoint until the new endpoint passes smoke and benchmark verification.

### Step 3.1: Hardware & Worker Configuration (NVIDIA A40)
| Setting | Recommended Value | Reason |
|---|---|---|
| **GPU Type** | **NVIDIA A40 (48 GB VRAM)** | Fits `Mistral-Small-24B` 4-bit NF4 (~14.5 GB VRAM) + LoRA + 12k token KV cache with ~28 GB headroom. |
| **Active Workers (Min)** | `0` (or `1` during peak business hours) | Set to `1` if zero cold-start latency is required for synchronous 60s backend webhook calls; `0` with **FlashBoot** enabled for cost savings. |
| **Max Workers** | `3` | Prevents queue buildup on concurrent requests while capping GPU spend. |
| **GPUs per Worker** | `1` | Single-GPU device map (`device_map={"": 0}`). |
| **FlashBoot** | **Enabled** | Reduces cold-start time significantly after initial worker snapshot. |
| **Idle Timeout** | `30` seconds | Keeps warm worker alive across back-to-back user uploads. |
| **Execution Timeout** | `600` seconds (10 min) | Allows multi-hundred-page PDFs to complete all chunks safely. |
| **Container Disk** | `40 GB` | Sufficient for container layers, OCR temp buffers, and LoRA weights. |

### Step 3.2: Base Model Caching (Critical to Prevent Repeated Downloads)
Use **either** (or both) of the following RunPod mechanisms so the 24B base model is never downloaded at worker startup:

1. **RunPod Cached Models (Recommended)**:
   - In the Endpoint configuration under **Model**, set:
     `mistralai/Mistral-Small-24B-Instruct-2501`
   - RunPod pre-mounts the model snapshot on the host before starting your container. `resolve_base_path()` automatically scans `/runpod-volume/huggingface-cache/hub` (both exact-case and lowercase `models--mistralai--mistral-small-24b-instruct-2501`).
2. **RunPod Network Volume (Alternative / Persistent Cache)**:
   - Attach a Network Volume (e.g. `50 GB`) mounted at `/runpod-volume`.
   - Set `HF_HOME=/runpod-volume/huggingface-cache` so once downloaded, the base model and LoRA adapter persist across all workers permanently.

### Step 3.3: Environment Variables (Placeholders Only — No Secrets in Code)

Configure these in the RunPod Endpoint **Environment Variables** UI (use **RunPod Secrets** for `HF_TOKEN`):

| Variable | Placeholder / Default Value | Description |
|---|---|---|
| `MODEL_ID` | `mistralai/Mistral-Small-24B-Instruct-2501` | Base model identifier. |
| `LORA_REPO` | `<YOUR_HF_USERNAME>/<YOUR_PRIVATE_LORA_REPO>` | Hugging Face repo ID for fine-tuned LoRA v3 adapter. |
| `HF_TOKEN` | `{{ RUNPOD_SECRET_HF_TOKEN }}` | Read-only Hugging Face token (stored in RunPod Secrets). |
| `HF_CACHE_ROOT` | `/runpod-volume/huggingface-cache/hub` | Root path for RunPod Cached Models / Network Volume cache. |
| `LORA_CACHE_DIR` | `/runpod-volume/lora-cache` | Persistent cache directory for the LoRA adapter. |
| `CHUNK_TARGET_CHARS` | `10000` | Training-aligned character target per chunk. |
| `CHUNK_OVERLAP_PAGES` | `1` | Page overlap between adjacent chunks. |
| `MAX_INPUT_TOKENS` | `12000` | Safe input token budget ceiling per inference call. |
| `MAX_NEW_TOKENS` | `6144` | Max generated output tokens per chunk. |
| `MAX_CHUNK_RETRIES` | `1` | Bounded retry / adaptive split limit per chunk. |
| `ENABLE_OCR` | `1` | Enable Arabic+English Tesseract OCR fallback on scanned PDF pages. |
| `SKIP_LLM_FOR_STRUCTURED_CARD_PAGES` | `1` | Use fast deterministic card extraction on structured operational plan cards and run LLM only on unstructured narrative pages. |
| `PRELOAD_MODEL_ON_START` | `1` | Preload base model + LoRA at worker boot and log `Model ready.` |

---

## 4. Verification & Cutover Procedure

1. **Container & Model Readiness Check**:
   Send a `warmup` health check to the new endpoint:
   ```bash
   curl -X POST "https://api.runpod.ai/v2/<NEW_ENDPOINT_ID>/runsync" \
     -H "Authorization: Bearer <YOUR_RUNPOD_API_KEY>" \
     -H "Content-Type: application/json" \
     -d '{"input": {"action": "warmup"}}'
   ```
   Verify the response shows `"status": "ready"`, `"model_initialized": true`, and `"gpu_name": "NVIDIA A40"`.
2. **Smoke Test with Reference Document**:
   Run a test request through n8n pointing to `<NEW_ENDPOINT_ID>` and verify all **9 programs** and **24 projects** are returned with HTTP 200.
3. **Cutover**:
   Update `RUNPOD_ENDPOINT_ID` in n8n environment variables to the new endpoint ID. Keep the old endpoint scaled to `0` workers for 48 hours as a rollback target before deleting it.
