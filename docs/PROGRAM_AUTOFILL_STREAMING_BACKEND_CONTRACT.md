# Program & Project Autofill — Progressive Streaming Backend Integration Contract

- **Contract / Schema Version:** `1.0`
- **Target AI Worker Repository:** `da03bod-cpu/The_Last_Entry` (`Mistral-Small-24B-Instruct-2501` 4-bit NF4 + LoRA v3)
- **Target Deployment Platform:** RunPod Serverless (Queue-Based HTTP API v2)
- **Verified RunPod Python SDK Version:** `runpod==2.4.0` (`requires runpod>=1.7.0`)
- **Audience:** Laravel Backend & Integration Engineering Team

---

## A. Current vs. New Architecture

### 1. Previous Synchronous Flow (Limitations)

Currently, `POST /api/v1/organization/programs/autofill` routes through `N8nAiProvider`:

```
Frontend ──POST──▶ Laravel API ──multipart/form-data (60s sync timeout)──▶ n8n Webhook
                                                                              │
                                                                 waits for full extraction
                                                                              │
Frontend ◀──single JSON── Laravel API ◀──single HTTP response (2xx)────────────┘ ◀── RunPod (/runsync)
```

Why this cannot deliver progressive streaming:
1. `N8nAiProvider` issues one synchronous `multipart/form-data` POST and holds the HTTP connection open with a **60-second timeout**, expecting `"Respond: When Last Node Finishes"`.
2. Large Arabic NGO reports requiring GPU LLM inference can take **40–120+ seconds** across multiple chunks, hitting the 60s timeout or forcing the user to stare at an empty screen until every last page completes.
3. A webhook node set to "Respond When Last Node Finishes" cannot emit multiple completed program/project objects over one standard JSON response as they finish.

### 2. New Progressive Streaming Flow (Direct RunPod Serverless Queue API)

**Recommendation:** Have Laravel communicate **directly with RunPod Serverless Queue-Based API v2** from a queued Laravel job, bypassing n8n for `program_autofill` streaming runs. (n8n can be retired for Autofill, or retained only if you explicitly keep a legacy non-streaming fallback path or an attachment preprocessor.)

```
Frontend
  │  POST /api/v1/organization/programs/autofill
  ▼
Laravel Controller
  │  1. Create ai_runs row (status: queued) & validate uploads
  │  2. Dispatch queued worker job (ProcessProgramAutofillStream)
  │  3. Return 202 Accepted { run_id, status: "queued" } (or SSE stream)
  ▼
Laravel Queue Worker (Horizon / Database Queue)
  │  1. Transform files[] ──▶ base64 or pre-signed URLs
  │  2. POST https://api.runpod.ai/v2/{ENDPOINT_ID}/run  (stream: true) ──▶ { id: job_id }
  │  3. Poll GET /v2/{ENDPOINT_ID}/stream/{job_id} in a loop:
  │       ├── receive "progress" ──▶ update ai_runs stage/counts
  │       ├── receive "item_completed" ──▶ upsert item by (ai_run_id, event_id) immediately!
  │       ├── receive "item_failed" ──▶ record warning / partial_failure
  │       ├── receive "failed" ──▶ capture failure metadata & check /status/{job_id}
  │       └── receive "completed" + FINAL status ──▶ finalize ai_runs row
  ▼
Frontend polls GET /api/v1/ai-runs/{run_id} (or subscribes to Laravel SSE)
  └── Sees each program & project appear in real time as soon as extracted & validated
```

- **Zero new infrastructure required:** No WebSocket server, Redis Streams pub/sub cluster (beyond Laravel's existing cache/queue), Kafka, external worker database, or second RunPod endpoint is needed.
- **No permanent worker state:** Each RunPod request tracks state strictly for that single job and exits cleanly on completion, error, or cancellation.

---

## B. Exact Request Contract (Laravel → RunPod)

### 1. Endpoint & Authentication

- **Method & URL:** `POST https://api.runpod.ai/v2/{ENDPOINT_ID}/run`
- **Headers:**
  - `Authorization: Bearer {RUNPOD_API_KEY}`
  - `Content-Type: application/json`

> **Security:** Keep `RUNPOD_API_KEY` strictly on the Laravel server (`.env` / secrets manager). Never expose RunPod credentials or endpoint IDs to browser/frontend code.

### 2. Request JSON Schema

RunPod requires the top-level JSON body `{"input": { ... }}` (optionally including `"webhook": "https://..."` at top level for RunPod completion callbacks).

Inside `input`:

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `stream` | `boolean` | **Yes** (for streaming) | `false` | **Must be `true`** (or `"true"`/`1`) to activate incremental streaming events. Omit or set `false` for legacy single-object output. |
| `run_id` | `string \| int` | Recommended | `null` | Backend `ai_runs` ID. Echoed on every streaming event (`progress`, `item_completed`, `item_failed`, `completed`, `failed`) and used as part of deterministic `event_id` hashing. |
| `type` | `string` | Optional | `"program_autofill"` | Action identifier (`"program_autofill"`). Also supports health actions (`"health"`, `"health_check"`, `"ping"`, `"warmup"`). |
| `text` | `string` | Conditional* | `""` | Direct pasted description/document text. Optional if `files` is provided or `input.text` is passed inside nested `input`. |
| `input` | `string \| object` | Conditional* | `{}` | Supports the legacy `N8nAiProvider` nested shape (`"{\"text\":\"...\",\"files\":[\"brief.pdf\"]}"` or object equivalent) if forwarded directly. `stream` and `run_id` may live either at top level of `job["input"]` or inside nested `input`. |
| `files` | `array<object>` | Conditional* | `[]` | Ordered list of attachment objects (up to 5 recommended). Also accepts keys `attachments`, `files_data`, or `files[]`. \*At least one readable source (`text` or non-empty `files`) is required. |
| `association_name` | `string` | Optional | auto-detected | Override NGO/association name metadata. |
| `year` | `integer \| string` | Optional | auto-detected | Override report year (`1900`–`2100`). |
| `period` | `string` | Optional | `null` | Report period override (normalized to `null` if `"تقرير سنوي"`). |
| `enable_ocr` | `boolean` | Optional | `true` | Enable Arabic + English Tesseract OCR fallback for scanned PDF pages. |
| `skip_llm_for_structured_cards` | `boolean` | Optional | `true` | Use fast incremental deterministic extraction for structured card decks and structured Markdown reports; falls back automatically to LLM token-streaming for unstructured documents. |

### 3. Transforming Laravel Multipart `files[]` into RunPod JSON Inputs

Because RunPod's `/run` endpoint accepts `application/json` (not `multipart/form-data`), Laravel must convert each uploaded `UploadedFile` before posting to RunPod. Supported formats inside the worker (`ingestion.py`) are `.pdf`, `.xlsx`, `.docx`, `.md`/`.markdown`, `.txt`/`.csv`.

Each element in `input.files` must be a JSON object using **one of two delivery mechanisms**:

#### Option 1 — Base64 Inline (Recommended for combined payloads <= ~6 MB)

```json
{
  "filename": "الخطه_التشغيليه_2027.pdf",
  "mime_type": "application/pdf",
  "content_base64": "JVBERi0xLjQK..."
}
```

- Supported base64 keys: `content_base64` (preferred), `base64`, `data`, or `content` (optional `data:<mime>;base64,` prefix is automatically stripped).
- Keep positional alignment if also passing nested `input.files = ["a.pdf", "b.xlsx"]` so duplicate filenames match by index (`files[i]`).
- **RunPod Payload Limit:** RunPod `/run` rejects request bodies exceeding **10 MB** (and base64 expands binary size by ~33%). Do not inline large scanned PDFs above ~6 MB raw binary size.

#### Option 2 — Short-Lived Pre-Signed URL (Required for Large Attachments)

```json
{
  "filename": "annual_report_scan.pdf",
  "mime_type": "application/pdf",
  "url": "https://storage.example.com/temp/run-42/annual_report_scan.pdf?X-Amz-Signature=..."
}
```

- Worker downloads with HTTP GET (`30s` timeout per file), detects format by magic bytes / extension / MIME type, runs PyMuPDF + Arabic/English Tesseract OCR (for scanned PDFs), OpenPyXL (XLSX), python-docx (DOCX), or UTF-8 decode (MD/TXT), and feeds pages incrementally into extraction.

---

## C. RunPod API Integration (`/run`, `/stream/{job_id}`, `/status/{job_id}`)

### 1. Envelope vs. Application Event Distinction

When consuming `GET /stream/{job_id}`, always distinguish two nested layers:
1. **RunPod Platform Envelope** (managed by RunPod Cloud API): `id`, `status`, `stream`, `delayTime`, `executionTime`, `output`, `error`.
2. **Application Event** (produced by our AI worker inside each `stream[i].output` element): `schema_version`, `event`, `run_id`, plus event-specific fields.

### 2. Job Submission — `POST /v2/{ENDPOINT_ID}/run`

**Response (RunPod Envelope):**

```json
{
  "id": "sync-7b9b0f82-1234-4a5b-9c8d-0123456789ab",
  "status": "IN_QUEUE"
}
```

### 3. Stream Consumption — `GET /v2/{ENDPOINT_ID}/stream/{job_id}`

Poll this endpoint continuously (e.g., every **500 ms – 1500 ms**) until the job reaches a terminal state **and** all buffered chunks are drained.

#### Verified RunPod SDK Drain Semantics (`runpod==2.4.0`)

Verified directly against installed `runpod/endpoint/runner.py` (`Job.stream()`) and `runpod/endpoint/asyncio/asyncio_runner.py`:

- RunPod's `/stream/{job_id}` endpoint returns **newly buffered chunks emitted since the previous `/stream/{job_id}` poll** (destructive read / drain queue semantics), wrapped in `stream: [ { "output": <EVENT_DICT> }, ... ]`.
- Notice in `runpod/endpoint/runner.py`:
  ```python
  if stream_partial["status"] not in FINAL_STATES or len(stream_partial.get("stream", [])) > 0:
      for chunk in stream_partial.get("stream", []):
          yield chunk["output"]
  elif stream_partial["status"] in FINAL_STATES:
      break
  ```
- **CRITICAL FOR LARAVEL:** Do **NOT** exit your polling loop solely because `status` just transitioned to `COMPLETED` or `FAILED`! On the exact poll where `status` flips to a terminal state (`COMPLETED`, `FAILED`, `CANCELLED`, `TIMED_OUT`), `stream` often STILL holds the remaining events (`item_completed`, `completed`, or `failed`). **Process every element of `stream` first, and only break when `status in FINAL_STATES && empty($response['stream'])`** (or wait one extra `/status` check if `COMPLETED`/`FAILED` returned empty `stream`).

**Example `GET /stream/{job_id}` RunPod Envelopes:**

While waiting for worker:
```json
{
  "id": "job-123",
  "status": "IN_QUEUE"
}
```

While streaming incrementally (`IN_PROGRESS`):
```json
{
  "id": "job-123",
  "status": "IN_PROGRESS",
  "delayTime": 840,
  "stream": [
    {
      "output": {
        "schema_version": "1.0",
        "event": "progress",
        "run_id": "42",
        "stage": "extracting",
        "completed_items": 0,
        "programs_count": 0,
        "projects_count": 0,
        "total_chunks": 2
      }
    },
    {
      "output": {
        "schema_version": "1.0",
        "event": "item_completed",
        "run_id": "42",
        "event_id": "evt_8a4c1d2e9b0f7123",
        "sequence": 1,
        "item_type": "program",
        "data": { "...": "10 contract fields" }
      }
    }
  ]
}
```

When complete (final buffered items delivered alongside `COMPLETED` or drained cleanly, followed by `stream: []`):
```json
{
  "id": "job-123",
  "status": "COMPLETED",
  "delayTime": 840,
  "executionTime": 14250,
  "stream": [
    {
      "output": {
        "schema_version": "1.0",
        "event": "completed",
        "run_id": "42",
        "total_items": 5,
        "programs_count": 2,
        "projects_count": 3,
        "failed_items_count": 0,
        "partial_failure": false,
        "needs_attention": false,
        "association_name": "جمعية البر",
        "year": 2027,
        "period": null,
        "review": [],
        "chunks": [],
        "sources": [],
        "json_repaired": false
      }
    }
  ]
}
```

### 4. Status Check — `GET /v2/{ENDPOINT_ID}/status/{job_id}`

Use `/status/{job_id}` in three cases:
1. When `/stream/{job_id}` reports `status == "FAILED"`, `"TIMED_OUT"`, or `"CANCELLED"` without a prior `failed` event in `stream`, fetch `/status/{job_id}` to read RunPod's `error` message.
2. If Laravel recovers from a crashed queue worker mid-stream (`ai_run` already has `job_id` stored, `/stream/{job_id}` expired or already drained earlier chunks), `/status/{job_id}` returns `"output": [<ALL_YIELDED_EVENTS>]` because `RETURN_AGGREGATE_STREAM=1` (`true`) is enabled on the worker!
3. Legacy non-streaming callers (`stream: false` or omitted) polling `/status/{job_id}` receive `"output": [<SINGLE_LEGACY_DICT>]` (see **Section G** for unwrapping details).

### 5. Cancellation — `POST /v2/{ENDPOINT_ID}/cancel/{job_id}`

Cancels a queued or in-flight job on RunPod. In the worker, closing/cancelling sets `cancel_event`, halting PyTorch GPU token generation cleanly at the next token step.

### 6. Critical RunPod `2.4.0` SDK Error Rule

Verified in `runpod/serverless/modules/rp_job.py` (`handle_job`, lines 221–227):
If a generator yields any dict containing a truthy `"error"` key, RunPod's worker SDK **immediately aborts the generator loop, skips `stream_result()`, and marks the entire job `FAILED`**.

Because of this verified SDK behavior:
- **None** of our streaming events (`progress`, `item_completed`, `item_failed`, `completed`, `failed`) contain a top-level `"error"` key; failure descriptions live in **`"error_message"`**.
- For non-fatal **`item_failed`**, the worker yields the event cleanly and **continues** extracting the rest of the document.
- For fatal failures, the worker **first yields `event: "failed"`** (with `error_message` and `partial_results_emitted`), so `/stream/{job_id}` receives the structured JSON event, and **then raises `StreamingExtractionError`** so RunPod marks the job `status: "FAILED"` on `/status/{job_id}` and `/stream/{job_id}`.

---

## D. Streaming Response Contract

Every chunk element `stream[i].output` emitted when `stream: true` follows schema version `1.0`. All payloads are compact, strictly JSON-serializable (`allow_nan=False`, UTF-8 Arabic/English preserved), and free of credentials or large raw dumps.

### 1. Event Catalog & Field Specifications

#### Event 1: `progress` (Optional / Informational)

Emitted at reliable lifecycle milestones: `"ingested"`, `"extracting"`, and `"chunk_completed"`. Never fabricates percentages or total item counts before extraction completes.

| Field | Type | Required | Description |
|---|---|---|---|
| `schema_version` | `string` | Always | `"1.0"` |
| `event` | `string` | Always | `"progress"` |
| `run_id` | `string \| null` | Always | Backend run correlation ID |
| `stage` | `string` | Always | `"ingested"`, `"extracting"`, or `"chunk_completed"` |
| `completed_items` | `integer` | Always | Count of valid `item_completed` events emitted so far |
| `programs_count` | `integer` | Always | Count of emitted `program` items so far |
| `projects_count` | `integer` | Always | Count of emitted `project` items so far |
| `chunk_index` | `integer` | Conditional | 1-based completed chunk index (only on `"chunk_completed"`) |
| `total_chunks` | `integer` | Conditional | Exact number of LLM chunks built after token budgeting (only on LLM `"extracting"` and `"chunk_completed"`, absent for deterministic cards/Markdown) |
| `message` | `string` | Optional | Short diagnostic note (<= 250 chars) |

#### Event 2: `item_completed` (Emitted Immediately Per Validated Program or Project)

Emitted as soon as an individual item completes extraction, 10-field validation, numeric grounding, and deduplication—**before** subsequent items, sections, or chunks finish.

| Field | Type | Required | Description |
|---|---|---|---|
| `schema_version` | `string` | Always | `"1.0"` |
| `event` | `string` | Always | `"item_completed"` |
| `run_id` | `string \| null` | Always | Backend run correlation ID |
| `event_id` | `string` | Always | Deterministic idempotent key: `"evt_" + sha256[:16]` over `(run_id, type, norm(name), year, budget, beneficiaries_count[, disambiguator])` |
| `sequence` | `integer` | Always | Strictly increasing 1-based item sequence (`1, 2, 3, ...`) |
| `item_type` | `string` | Always | `"program"` or `"project"` (matches `data.type`) |
| `data` | `object` | Always | Complete validated 10-field item matching `StoreProgramRequest::rules()` |

**Exact 10 Fields inside `item_completed.data`:**

| Key | Type | Constraints | Notes |
|---|---|---|---|
| `type` | `string` | Required; strictly `"program"` or `"project"` | Verified against Arabic/English prefixes, hierarchy codes, and umbrella context |
| `name` | `string` | Required; non-empty, length `1..255` | Snapped to verbatim heading when minor LLM typo occurs |
| `year` | `integer \| null` | `1900 <= year <= 2100` or `null` | Falls back to detected/requested document year if omitted on item |
| `description` | `string \| null` | Narrative description or `null` | Never blindly duplicated to equal `delivery_method` |
| `beneficiaries_count` | `integer \| null` | Integer `>= 0` or `null` | Grounded in source text; KPI quantities (baskets, meals, hours) rejected as `null` unless explicitly labeled as beneficiaries |
| `budget` | `number \| null` | Non-negative `int` or `float`, or `null` | Grounded in source text; never fabricated |
| `beneficiary_value` | `string \| null` | Benefit/impact statement or `null` | Grounded in document or `null` |
| `target_audience` | `string \| null` | Target beneficiary segment or `null` | Grounded in document or `null` |
| `delivery_method` | `string \| null` | Execution/delivery mechanism or `null` | Distinct from `description` |
| `notes` | `string \| null` | Source page, hierarchy/anti-double-counting notes, caveats, or `null` | Includes umbrella linkage notes where applicable |

#### Event 3: `item_failed` (Non-Fatal Partial Item or Single-Chunk Failure)

Emitted when an individual extracted object fails schema validation (e.g., missing/empty `name`), raises a per-item error, or an individual LLM chunk fails JSON parsing while other chunks/items proceed. **Job continues.**

| Field | Type | Required | Description |
|---|---|---|---|
| `schema_version` | `string` | Always | `"1.0"` |
| `event` | `string` | Always | `"item_failed"` |
| `run_id` | `string \| null` | Always | Backend run correlation ID |
| `event_id` | `string` | Always | Deterministic failure ID: `"err_" + sha256[:16]` |
| `reason_code` | `string` | Always | `"validation_failed"`, `"item_extraction_error"`, or `"chunk_parse_error"` |
| `error_message` | `string` | Always | Human/log-readable failure reason (<= 500 chars). *Key is `error_message`, NOT `error`.* |
| `item_name` | `string \| null` | Always | Partial item name if available, else `null` |
| `chunk_index` | `integer` | Conditional | 1-based chunk index where failure occurred (present on LLM chunk path) |

#### Event 4: `completed` (Final Stream Success Event)

Emitted once after all cards, Markdown sections, or LLM chunks finish and zero fatal errors occurred.

| Field | Type | Required | Description |
|---|---|---|---|
| `schema_version` | `string` | Always | `"1.0"` |
| `event` | `string` | Always | `"completed"` |
| `run_id` | `string \| null` | Always | Backend run correlation ID |
| `total_items` | `integer` | Always | Equals `programs_count + projects_count` |
| `programs_count` | `integer` | Always | Count of emitted `"program"` items |
| `projects_count` | `integer` | Always | Count of emitted `"project"` items |
| `failed_items_count` | `integer` | Always | Count of `item_failed` events emitted during run (`0` if clean) |
| `partial_failure` | `boolean` | Always | `true` if `failed_items_count > 0` |
| `needs_attention` | `boolean` | Always | `true` if `partial_failure`, `total_items == 0`, JSON repair occurred, truncation remained unrecovered, or numbers/classifications required review flags |
| `association_name` | `string \| null` | Always | Detected or requested organization name |
| `year` | `integer \| null` | Always | Detected or requested year |
| `period` | `string \| null` | Always | Period or `null` |
| `review` | `array<object>` | Always | List of `{"name": "...", "reason": "..."}` diagnostic warnings |
| `chunks` | `array<object>` | Always | Compact per-chunk LLM telemetry (`[]` for pure deterministic runs) |
| `sources` | `array<object>` | Always | Metadata of ingested sources (`filename`, `format`, `page_count`, `ocr_pages`) |
| `json_repaired` | `boolean` | Always | Whether `json_repair` fallback was invoked |

#### Event 5: `failed` (Terminal Failure Event)

Emitted immediately before raising `StreamingExtractionError` to transition the RunPod job to `status: "FAILED"`. Triggered by empty/corrupted/unreadable input, invalid `job["input"]`, execution deadline timeout, unrecoverable runtime/GPU crash, or all LLM chunks failing parse with `0` valid items.

| Field | Type | Required | Description |
|---|---|---|---|
| `schema_version` | `string` | Always | `"1.0"` |
| `event` | `string` | Always | `"failed"` |
| `run_id` | `string \| null` | Always | Backend run correlation ID |
| `error_message` | `string` | Always | Terminal error description (<= 500 chars) |
| `error_type` | `string` | Always | e.g., `"validation_error"`, `"ChunkParseError"`, `"TimeoutError"`, `"RuntimeError"` |
| `completed_items` | `integer` | Always | Number of valid `item_completed` events emitted **before** failure |
| `programs_count` | `integer` | Always | Valid programs emitted before failure |
| `projects_count` | `integer` | Always | Valid projects emitted before failure |
| `partial_results_emitted` | `boolean` | Always | `true` when `completed_items > 0` (Laravel may preserve/show partial items with failed/warning state) |

---

### 2. Complete Progressive Example: 2 Programs & 3 Projects Arriving Incrementally

Below is the exact chronological sequence of application events (`stream[i].output`) for an operational/annual submission containing **2 programs and 3 projects** across mixed Arabic and English content (`run_id: "42"`):

#### Chunk 1 (`progress` — `ingested`)
```json
{
  "schema_version": "1.0",
  "event": "progress",
  "run_id": "42",
  "stage": "ingested",
  "completed_items": 0,
  "programs_count": 0,
  "projects_count": 0
}
```

#### Chunk 2 (`progress` — `extracting`)
```json
{
  "schema_version": "1.0",
  "event": "progress",
  "run_id": "42",
  "stage": "extracting",
  "completed_items": 0,
  "programs_count": 0,
  "projects_count": 0,
  "total_chunks": 2
}
```

#### Chunk 3 (`item_completed` #1 — Program 1 arrives while Chunk 2 hasn't started)
```json
{
  "schema_version": "1.0",
  "event": "item_completed",
  "run_id": "42",
  "event_id": "evt_6b1f92c80e44a31d",
  "sequence": 1,
  "item_type": "program",
  "data": {
    "type": "program",
    "name": "كفاية",
    "year": 2027,
    "description": "برنامج لتلبية الاحتياجات الأساسية للمستفيدين في الغذاء والماء والصحة والسكن.",
    "beneficiaries_count": null,
    "budget": 3723725,
    "beneficiary_value": "تقديم مساعدات عينية ومالية وخدمات أساسية للأسر والأفراد المستحقين.",
    "target_audience": "جميع فئات المستفيدين",
    "delivery_method": "تنفيذ مشاريع متخصصة عبر مسارات الصرف الموثقة ومتابعة الحالات.",
    "notes": "المصدر: ص 8–9؛ رمز البرنامج: 1.1.A"
  }
}
```

#### Chunk 4 (`item_completed` #2 — Project 1 arrives immediately as next object closes)
```json
{
  "schema_version": "1.0",
  "event": "item_completed",
  "run_id": "42",
  "event_id": "evt_d41289b0a1c77f02",
  "sequence": 2,
  "item_type": "project",
  "data": {
    "type": "project",
    "name": "السلة الغذائية",
    "year": 2027,
    "description": "توفير وتوزيع سلال غذائية للأسر والأفراد المستحقين وفق احتياجاتهم.",
    "beneficiaries_count": null,
    "budget": 1155000,
    "beneficiary_value": "تلبية جانب من الاحتياجات الغذائية للمستفيدين.",
    "target_audience": "جميع مستفيدي الجمعية",
    "delivery_method": "تجهيز السلال وتوزيعها مع توثيق الاستلام.",
    "notes": "المصدر: ص 10؛ مؤشر الأداء: عدد السلال الغذائية: المستهدف 2,160 سلة"
  }
}
```

#### Chunk 5 (`progress` — `chunk_completed` 1/2)
```json
{
  "schema_version": "1.0",
  "event": "progress",
  "run_id": "42",
  "stage": "chunk_completed",
  "completed_items": 2,
  "programs_count": 1,
  "projects_count": 1,
  "chunk_index": 1,
  "total_chunks": 2
}
```

#### Chunk 6 (`item_completed` #3 — Project 2 from Chunk 2)
```json
{
  "schema_version": "1.0",
  "event": "item_completed",
  "run_id": "42",
  "event_id": "evt_19c0f38b4e22a910",
  "sequence": 3,
  "item_type": "project",
  "data": {
    "type": "project",
    "name": "سقيا الماء",
    "year": 2027,
    "description": "توفير مياه للمستفيدين مع مراعاة المعايير الصحية وعمليات التسليم.",
    "beneficiaries_count": 1450,
    "budget": 145800,
    "beneficiary_value": "توفير المياه للمستفيدين المستهدفين.",
    "target_audience": "الأيتام",
    "delivery_method": "التوزيع عبر فريق ومواقع تخزين ومتابعة عمليات التسليم.",
    "notes": "المصدر: ص 11؛ مدة التنفيذ: 12 شهر"
  }
}
```

#### Chunk 7 (`item_completed` #4 — Program 2, English example)
```json
{
  "schema_version": "1.0",
  "event": "item_completed",
  "run_id": "42",
  "event_id": "evt_f032a471bc9e051a",
  "sequence": 4,
  "item_type": "program",
  "data": {
    "type": "program",
    "name": "Clean Water Initiative",
    "year": 2025,
    "description": "Provides potable water access to rural communities through well construction and maintenance training.",
    "beneficiaries_count": 1200,
    "budget": 45000,
    "beneficiary_value": "Each household gains reliable year-round access to clean drinking water.",
    "target_audience": "Rural households without piped water access",
    "delivery_method": "Community-led well construction with quarterly maintenance visits",
    "notes": "Budget figure extracted from the attached spreadsheet."
  }
}
```

#### Chunk 8 (`item_completed` #5 — Project 3)
```json
{
  "schema_version": "1.0",
  "event": "item_completed",
  "run_id": "42",
  "event_id": "evt_3c7e51d9902b8a44",
  "sequence": 5,
  "item_type": "project",
  "data": {
    "type": "project",
    "name": "Solar Rural Clinics",
    "year": 2025,
    "description": "Installing backup solar power units for remote primary healthcare centers.",
    "beneficiaries_count": 850,
    "budget": 60000,
    "beneficiary_value": "Uninterrupted vaccine refrigeration and clinic lighting.",
    "target_audience": "Remote clinic patients",
    "delivery_method": "Contracted PV installation with local technician handover",
    "notes": null
  }
}
```

#### Chunk 9 (`progress` — `chunk_completed` 2/2)
```json
{
  "schema_version": "1.0",
  "event": "progress",
  "run_id": "42",
  "stage": "chunk_completed",
  "completed_items": 5,
  "programs_count": 2,
  "projects_count": 3,
  "chunk_index": 2,
  "total_chunks": 2
}
```

#### Chunk 10 (`completed`)
```json
{
  "schema_version": "1.0",
  "event": "completed",
  "run_id": "42",
  "total_items": 5,
  "programs_count": 2,
  "projects_count": 3,
  "failed_items_count": 0,
  "partial_failure": false,
  "needs_attention": false,
  "association_name": "جمعية البر الخيرية بالثنية (وصال)",
  "year": 2027,
  "period": null,
  "review": [],
  "chunks": [
    {
      "chunk": 1,
      "source_chunk": 1,
      "pages": [8, 10],
      "seconds": 38.4,
      "input_tokens": 3120,
      "generated_tokens": 940,
      "stop_reason": "eos",
      "retried": false,
      "programs": 2
    },
    {
      "chunk": 2,
      "source_chunk": 2,
      "pages": [11, 14],
      "seconds": 41.1,
      "input_tokens": 2890,
      "generated_tokens": 1105,
      "stop_reason": "eos",
      "retried": false,
      "programs": 3
    }
  ],
  "sources": [
    {
      "index": 0,
      "filename": "plan_2027.pdf",
      "format": "pdf",
      "page_count": 14,
      "ocr_pages": []
    }
  ],
  "json_repaired": false
}
```

---

## E. Laravel Implementation Requirements

Do **not** reuse `N8nAiProvider`'s single-response 60s synchronous call for streaming runs. Implement a dedicated RunPod streaming provider + queued job with the following responsibilities:

1. **Request Submission & Storage (`POST /api/v1/organization/programs/autofill`):**
   - Authorize user/organization, validate attachments (count <= 5, supported MIME/ext), create the `ai_runs` record (`type = 'program_autofill'`, `status = 'queued'`).
   - Encode files: base64 inline if total attachment payload <= ~6 MB; otherwise upload temporarily to private object storage and generate short-lived signed `url`s (expires in e.g. 30 min, deleted upon run completion).
   - Dispatch a Laravel queued job (`ProcessProgramAutofillStream`) with `$run->id`. Do **not** hold the PHP-FPM HTTP worker open waiting for GPU extraction.

2. **Submitting to RunPod & Storing `provider_job_id`:**
   - Inside `ProcessProgramAutofillStream`, call `POST https://api.runpod.ai/v2/{ENDPOINT_ID}/run` with `"stream": true` and `"run_id": (string) $run->id`.
   - Store the returned `id` (`job_id`) on `ai_runs.external_job_id` (alongside `status = 'running'`) BEFORE entering the stream loop so retries/cancellations can inspect `/status/{job_id}` or `/cancel/{job_id}`.

3. **Stream Consumption Loop (`GET /v2/{ENDPOINT_ID}/stream/{job_id}`):**
   - Poll with an HTTP client timeout of e.g. `15s` per poll, sleeping `750ms`–`1000ms` between empty polls (`IN_QUEUE` / empty `stream`), with an overall wall-clock safety limit >= `600s` (above RunPod `EXECUTION_TIMEOUT_SECONDS=540` + queue wait).
   - For each poll response:
     - Read `$chunks = $resp['stream'] ?? []`.
     - Iterate every chunk in arrival order: `$ev = $chunk['output'] ?? null` (ensure `$ev` is an associative array containing `'event'`).
     - Exit loop ONLY when `in_array($resp['status'], ['COMPLETED', 'FAILED', 'CANCELLED', 'TIMED_OUT'], true)` **AND** `empty($chunks)`.
   - Also wrap `$job->attempts()` / `try/finally`: if the Laravel job is cancelled (`$run` deleted/cancelled by user), send `POST /v2/{ENDPOINT_ID}/cancel/{job_id}`.

4. **JSON Decoding & Validation Per `item_completed` Event:**
   - Validate `$ev['schema_version'] === '1.0'` and `$ev['event'] === 'item_completed'`.
   - Validate `$ev['data']` with `Validator::make($ev['data'], StoreProgramRequest::rules())` (adjusting any route/auth-context rules needed so draft staging succeeds cleanly):
     - `type`: `in:program,project`
     - `name`: `required|string|max:255`
     - `year`: `nullable|integer|between:1900,2100`
     - `beneficiaries_count`: `nullable|integer|min:0`
     - `budget`: `nullable|numeric|min:0`
     - `description`, `beneficiary_value`, `target_audience`, `delivery_method`, `notes`: `nullable|string`
   - Do **not** auto-reject the entire run if one item somehow fails Laravel validation; log the validation error on that draft/warning list and continue reading remaining streamed items.

5. **Incremental Persistence & Idempotent Deduplication:**
   - Persist each validated `item_completed` **immediately inside the poll loop iteration** into a staged/extracted items table (e.g. `ai_run_extracted_items` or structured JSON/relational draft rows linked to `ai_runs`), rather than waiting until `completed`.
   - Create a unique DB index on **`(ai_run_id, event_id)`** and use `upsert` / `firstOrCreate(['ai_run_id' => $run->id, 'event_id' => $ev['event_id']], ...)`:
     - Guarantees retries of the exact same `run_id` and input never insert duplicates (since `event_id` is deterministic SHA-256).
   - Store `sequence`, `item_type`, and `data` JSON/columns so the frontend renders items in exact `sequence` order.

6. **Run Status Updates & Partial Failures:**
   - On `progress`: update `ai_runs` metadata (`stage`, `programs_count`, `projects_count`, optional `chunk_index`/`total_chunks`) without overwriting persisted items.
   - On `item_failed`: append to `ai_runs.warnings` / `failed_items`, set `has_warnings = true`, keep run active.
   - On `completed`: verify persisted count, mark `ai_runs.status = 'completed'` (or `'completed_with_warnings'` if `partial_failure === true` or `needs_attention === true`), save `review`, `sources`, `needs_attention`, and final counts.
   - On `failed` event **or** RunPod terminal status in `['FAILED', 'TIMED_OUT', 'CANCELLED']`:
     - Call `GET /status/{job_id}` if no `failed` event was received in `stream` to capture RunPod's `error` text.
     - Keep any `item_completed` rows already saved earlier (`partial_results_emitted == true`!) so the organization doesn't lose already-extracted items, mark `ai_runs.status = 'failed'`, and expose `error_message` + `partial_results_emitted` flag.

7. **Retry Behavior:**
   - Retry transient HTTP `5xx`/`429`/network errors on `GET /stream/{job_id}` using the **same `job_id`** without resubmitting `/run`.
   - If resubmitting a fresh RunPod job for the same `ai_run` (`run_id` preserved), deterministic `event_id` values make DB upserts safe; if stale partial items from an aborted attempt shouldn't mix with different content, scope/clear staged items only if user explicitly starts a brand-new `ai_run`.

---

## F. Frontend Implications

1. **Preserving `POST /api/v1/organization/programs/autofill`:**
   - **Recommended Option (HTTP `202` + Polling — simplest, zero proxy timeouts):**
     - `POST /api/v1/organization/programs/autofill` returns `202 Accepted` immediately with `{"run_id": 42, "status": "queued"}`.
     - Frontend polls `GET /api/v1/ai-runs/42` (every `1s`–`1.5s`), which returns current `status`, `stage`, `programs_count`, `projects_count`, and the ordered array of `items` already persisted so far in Laravel's DB.
     - Works reliably behind Nginx, Cloudflare, load balancers, and PHP-FPM without holding long connections open.
   - **Alternative Option (Laravel Server-Sent Events / `GET /api/v1/ai-runs/{run_id}/events`):**
     - Queue job still consumes RunPod and persists each `item_completed` in DB/cache; a separate SSE controller uses Symfony `StreamedResponse` polling DB rows (`sequence > last_seen`) every `500ms` and flushing `event: item_completed\ndata: {...}\n\n`.
   - **No WebSocket server is required.** Polling or Laravel SSE completely satisfies progressive UI rendering (`Programs` & `Projects` forms/cards filling in one by one as extracted).

2. **Note on Multi-Entity vs. Single Form Pre-fill:**
   - Previously (`AI_PROGRAM_AUTOFILL_N8N_CONTRACT (2).md`), the synchronous autofill expected a single entity to pre-fill one form, whereas multi-page reports actually contain multiple programs and projects.
   - With progressive streaming, each `item_completed.data` conforms identically to `StoreProgramRequest::rules()`, allowing the frontend either to list extracted draft cards with one-click "Approve & Save" / bulk save, or select any arrived item immediately without waiting for the rest of the document.

---

## G. Backward Compatibility

| Component / Mode | Behavior Verified on `runpod==2.4.0` | Breaking Change? | Notes / Mitigation |
|---|---|---|---|
| Direct Python `handler.handler(job)` | Unchanged synchronous single `dict` | No | All 19 legacy pipeline unit tests pass unchanged |
| `type` / `action`: `health`, `health_check`, `ping`, `warmup` | Yields exact health dict (`status`, `model_initialized`, etc.) | **Envelope list-wrap on RunPod** (see below) | In `health`/`ping`, any prior `_MODEL_STATE['error']` is moved to `model_error` so RunPod does not mark readiness probes `FAILED` |
| Autofill with `stream` omitted or `stream: false` | Yields exact single legacy response `dict` (`programs`, `projects`, `programs_and_projects`, `summary`, etc.) | **Envelope list-wrap on RunPod** (see below) | Extraction fields inside the dict are 100% unchanged |
| Autofill with `stream: true` | Opt-in progressive events on `/stream/{job_id}` | New event contract (opt-in) | Only activated when caller explicitly passes `stream: true` |
| Extraction 10-field schema (`pp.FIELDS`) | Exact same 10 keys & types | No | Identical validation, Arabic preservation, and numeric grounding |

### Verified `return_aggregate_stream` Array-Wrapping Caveat (CRITICAL)

Verified directly in `runpod/serverless/modules/rp_handler.py` (`is_generator`) and `rp_job.py` (`handle_job`, lines 212–233):

- RunPod decides generator mode **statically at worker startup** via `inspect.isgeneratorfunction(handler)`. A single endpoint cannot be a non-generator for `stream: false` and a generator for `stream: true`.
- With our default worker config (`RUNPOD_ENABLE_STREAMING=1`, `RETURN_AGGREGATE_STREAM=1`):
  - RunPod aggregates ALL yielded chunks into a JSON **array**: `output: [chunk_0, chunk_1, ...]`.
  - Consequently, even for `stream: false` or `health_check` (which yield **one** legacy dict), RunPod `/status/{job_id}` and `/runsync` return:
    ```json
    {
      "id": "...",
      "status": "COMPLETED",
      "output": [ { "type": "program", "name": "...", "programs": [...], "projects": [...] } ]
    }
    ```
    instead of `"output": { ... }`.
- **Safe Client Unwrapping Rule (required for any `/runsync` or `/status` consumer):**
  ```php
  $output = $response['output'] ?? null;
  if (is_array($output) && array_is_list($output) && count($output) === 1 && is_array($output[0])) {
      $output = $output[0];
  }
  ```
- **Rollback Feature Flag:** Setting environment variable **`RUNPOD_ENABLE_STREAMING=0`** on the RunPod Serverless endpoint immediately reverts startup to the legacy non-generator `handler` (restoring bare object `"output": {...}`, while disabling `/stream/{job_id}`).

---

## H. Working Examples

### 1. Submit Streaming Job (`cURL`)

```bash
curl -sS -X POST "https://api.runpod.ai/v2/${RUNPOD_ENDPOINT_ID}/run" \
  -H "Authorization: Bearer ${RUNPOD_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "input": {
      "run_id": "42",
      "type": "program_autofill",
      "stream": true,
      "year": 2027,
      "text": "برنامج كفاية بميزانية 3,723,725 ريال ومشروع السلة الغذائية بميزانية 1,155,000 ريال.",
      "files": [
        {
          "filename": "plan_2027.pdf",
          "mime_type": "application/pdf",
          "url": "https://storage.example.com/signed/plan_2027.pdf"
        }
      ]
    }
  }'
```

**Response:**
```json
{"id": "4e1b9c20-0001-4abc-9def-1234567890ab-u1", "status": "IN_QUEUE"}
```

### 2. Poll Stream Chunks (`cURL`)

```bash
curl -sS -X GET "https://api.runpod.ai/v2/${RUNPOD_ENDPOINT_ID}/stream/${JOB_ID}" \
  -H "Authorization: Bearer ${RUNPOD_API_KEY}"
```

### 3. Partial Failure Sequence Example (`item_failed` followed by `completed`)

```json
[
  {
    "schema_version": "1.0",
    "event": "item_completed",
    "run_id": "42",
    "event_id": "evt_21a8e7c94b0012ef",
    "sequence": 1,
    "item_type": "program",
    "data": {
      "type": "program",
      "name": "برنامج الرعاية الصحية",
      "year": 2027,
      "description": null,
      "beneficiaries_count": null,
      "budget": 70000,
      "beneficiary_value": null,
      "target_audience": null,
      "delivery_method": null,
      "notes": null
    }
  },
  {
    "schema_version": "1.0",
    "event": "item_failed",
    "run_id": "42",
    "event_id": "err_7c910a24f38d11ab",
    "reason_code": "validation_failed",
    "error_message": "Extracted item failed validation (invalid:missing_entity_name)",
    "item_name": null,
    "chunk_index": 1
  },
  {
    "schema_version": "1.0",
    "event": "completed",
    "run_id": "42",
    "total_items": 1,
    "programs_count": 1,
    "projects_count": 0,
    "failed_items_count": 1,
    "partial_failure": true,
    "needs_attention": true,
    "association_name": null,
    "year": 2027,
    "period": null,
    "review": [],
    "chunks": [{"chunk": 1, "source_chunk": 1, "pages": [1, 1], "seconds": 4.2, "input_tokens": 310, "generated_tokens": 180, "stop_reason": "eos", "retried": false, "programs": 1}],
    "sources": [{"source": "pasted_text", "format": "text", "page_count": 1}],
    "json_repaired": false
  }
]
```

### 4. Terminal Failure After >= 1 Successful Event Example

Worker streams Item #1 first, then encounters an error on Chunk 2:
1. Chunk 1 streamed earlier via `/stream/{job_id}`: `item_completed` (`sequence: 1`).
2. Structured `failed` event streamed via `/stream/{job_id}` right before job terminates:
   ```json
   {
     "schema_version": "1.0",
     "event": "failed",
     "run_id": "42",
     "error_message": "CUDA out of memory",
     "error_type": "RuntimeError",
     "completed_items": 1,
     "programs_count": 1,
     "projects_count": 0,
     "partial_results_emitted": true
   }
   ```
3. Next `/stream/{job_id}` / `/status/{job_id}` reports RunPod platform status `"FAILED"`:
   ```json
   {
     "id": "4e1b9c20-0001-4abc-9def-1234567890ab-u1",
     "status": "FAILED",
     "error": "handler: CUDA out of memory \ntraceback: ..."
   }
   ```

### 5. Laravel Integration Pseudocode Sequence

```php
// Inside ProcessProgramAutofillStream::handle(RunPodStreamingClient $client)
$jobId = $client->submitRun([
    'run_id' => (string) $this->aiRun->id,
    'type' => 'program_autofill',
    'stream' => true,
    'text' => $this->pastedText,
    'files' => $this->preparedAttachments,
]);

$this->aiRun->update(['status' => 'running', 'external_job_id' => $jobId]);

$terminalCompletedEvent = null;
$terminalFailedEvent = null;

while (true) {
    $resp = $client->fetchStream($jobId); // GET /v2/{endpoint}/stream/{jobId}
    $chunks = $resp['stream'] ?? [];

    foreach ($chunks as $chunk) {
        $ev = $chunk['output'] ?? null;
        if (! is_array($ev) || empty($ev['event'])) {
            continue;
        }
        match ($ev['event']) {
            'progress' => $this->aiRun->update([
                'progress_stage' => $ev['stage'],
                'completed_items_count' => $ev['completed_items'],
            ]),
            'item_completed' => $this->persistValidatedItem($this->aiRun, $ev), // upsert by (ai_run_id, event_id)
            'item_failed' => $this->recordItemWarning($this->aiRun, $ev),
            'completed' => $terminalCompletedEvent = $ev,
            'failed' => $terminalFailedEvent = $ev,
            default => null,
        };
    }

    $rpStatus = $resp['status'] ?? 'UNKNOWN';
    if (in_array($rpStatus, ['COMPLETED', 'FAILED', 'CANCELLED', 'TIMED_OUT'], true) && empty($chunks)) {
        $this->finalizeRun($this->aiRun, $rpStatus, $terminalCompletedEvent, $terminalFailedEvent, $client, $jobId);
        break;
    }

    usleep(800_000); // 800 ms
}
```

---

## I. Deployment Requirements

### 1. Worker Repository Changes (`d:\bod\The_Last_Entry`)

- Added: [`streaming.py`](file:///d:/bod/The_Last_Entry/streaming.py), [`tests/test_streaming.py`](file:///d:/bod/The_Last_Entry/tests/test_streaming.py), [`docs/PROGRAM_AUTOFILL_STREAMING_BACKEND_CONTRACT.md`](file:///d:/bod/The_Last_Entry/docs/PROGRAM_AUTOFILL_STREAMING_BACKEND_CONTRACT.md), [`docs/PROGRAM_AUTOFILL_STREAMING_TEST_REPORT.md`](file:///d:/bod/The_Last_Entry/docs/PROGRAM_AUTOFILL_STREAMING_TEST_REPORT.md)
- Modified: [`config.py`](file:///d:/bod/The_Last_Entry/config.py), [`postprocess.py`](file:///d:/bod/The_Last_Entry/postprocess.py), [`handler.py`](file:///d:/bod/The_Last_Entry/handler.py), [`Dockerfile`](file:///d:/bod/The_Last_Entry/Dockerfile), [`.github/workflows/build.yml`](file:///d:/bod/The_Last_Entry/.github/workflows/build.yml)
- Docker image copies `config.py ingestion.py postprocess.py streaming.py handler.py`.
- Dependency requirement: `runpod>=1.7.0` in `requirements.txt` (verified with `runpod==2.4.0`).

### 2. RunPod Serverless Environment Variables

| Variable | Default | Notes |
|---|---|---|
| `RUNPOD_ENABLE_STREAMING` | `"1"` | `"1"` registers generator `stream_handler`; `"0"` reverts to legacy synchronous `handler` |
| `RETURN_AGGREGATE_STREAM` | `"1"` | `"1"` preserves final aggregated output array on `/status/{job_id}` & `/runsync` |
| `STREAM_EMIT_PROGRESS` | `"1"` | Set `"0"` to emit only `item_completed`, `item_failed`, `completed`, `failed` |
| `STREAM_DEADLINE_BUFFER_SECONDS` | `"15"` | Soft safety margin below `EXECUTION_TIMEOUT_SECONDS` (`540`) |
| `EXECUTION_TIMEOUT_SECONDS` | `"540"` | Worker soft timeout; configure RunPod endpoint Execution Timeout >= `600s` |
| `MODEL_ID`, `LORA_REPO`, `HF_TOKEN` | (unchanged) | Existing base model + LoRA v3 settings remain untouched |

### 3. Local Development SDK Quirk (`--rp_serve_api` vs `--test_input`)

Verified in `runpod/serverless/modules/rp_local.py`: RunPod's CLI `--test_input` mode invokes `run_job()` rather than `run_job_generator()`, whereas queue production mode (`handle_job`) and hosted dev server (`--rp_serve_api`) invoke `run_job_generator()`. Use `pytest` or `python -u handler.py --rp_serve_api` when testing streaming locally.

### 4. Post-Deployment Verification Checklist (Before Cutover)

1. Build & push updated Docker image (only when operator explicitly runs git push / image build).
2. Update RunPod Serverless endpoint image tag and ensure `RUNPOD_ENABLE_STREAMING=1`, `RETURN_AGGREGATE_STREAM=1`, and `RUNPOD_API_KEY` is configured on Laravel.
3. Run live GPU `/run` + `/stream/{job_id}` smoke test with:
   - Structured Arabic operational plan (verify rapid incremental emission).
   - Unstructured narrative Arabic/English PDF >= 2 chunks (verify first `item_completed` arrives in `/stream/{job_id}` while `status` is still `IN_PROGRESS`!).
   - Health check probe (`{"input": {"action": "health_check"}}`) and verify Laravel's array-unwrapping handles `"output": [{...}]`.
