# Integration Guide — Backend, n8n Webhook, and RunPod Serverless

This document defines the exact end-to-end request/response architecture between the Laravel backend (`N8nAiProvider`), the n8n workflow ([`n8n_program_autofill_workflow.json`](file:///d:/bod/The_Last_Entry/n8n_program_autofill_workflow.json)), and the RunPod Serverless worker ([`handler.py`](file:///d:/bod/The_Last_Entry/handler.py)).

---

## 1. Authoritative Backend-to-n8n Request Contract

Endpoint triggered by backend: `POST /api/v1/organization/programs/autofill`

`N8nAiProvider` (`app/Domains/Ai/Runs/Services/N8nAiProvider.php`) sends a single `multipart/form-data` POST to the n8n Webhook with:

| Multipart Field | Type | Description |
|---|---|---|
| `run_id` | text | Database ID of the `ai_runs` record (e.g. `"42"`). |
| `type` | text | `"program_autofill"`. |
| `input` | text | JSON-encoded string: `{"text": "...", "files": ["brief.pdf"]}`. `text` is omitted on file-only runs; `files` is an ordered array of filenames aligned by position with `files[]`. |
| `files[]` | file (binary) | 0 to 5 binary file parts in the exact same order as `input.files` (`files[][i]` corresponds to `input.files[i]`). Two attachments may share the same filename. |

### Example Backend Request (`curl` equivalent)
```bash
curl -X POST "https://n8n.example.com/webhook/program-autofill" \
  -F 'run_id=42' \
  -F 'type=program_autofill' \
  -F 'input={"text":"انظر الخطة المرفقة لعام 2027","files":["الخطة_التشغيلية_والموازنة_التقديرية_2027.pdf"]}' \
  -F 'files[]=@الخطة_التشغيلية_والموازنة_التقديرية_2027.pdf'
```

---

## 2. n8n Workflow Design & RunPod Request Format

Import [`n8n_program_autofill_workflow.json`](file:///d:/bod/The_Last_Entry/n8n_program_autofill_workflow.json) into n8n.

### How the n8n Workflow Processes the Request:
1. **Webhook Node**: Configured with `HTTP Method: POST`, `Response Mode: When Last Node Finishes`, and `Binary Property: files`.
2. **Parse Contract & Align Positional Binaries Node**:
   - Parses `JSON.parse($json.body.input)`.
   - Reads `parsedInput.text` (if present) and `parsedInput.files` (ordered filename array).
   - Iterates over the incoming binary attachments in positional order (`i = 0..k`), pairing each binary buffer (`content_base64`) with `parsedInput.files[i]` so duplicate filenames never overwrite each other.
3. **RunPod Serverless (`/runsync`) HTTP Node**:
   - Sends the structured JSON payload to `https://api.runpod.ai/v2/{{ $env.RUNPOD_ENDPOINT_ID }}/runsync`.

### Example RunPod Input (`job["input"]`)
```json
{
  "input": {
    "run_id": "42",
    "type": "program_autofill",
    "input": "{\"text\":\"انظر الخطة المرفقة لعام 2027\",\"files\":[\"الخطة_التشغيلية_والموازنة_التقديرية_2027.xlsx\"]}",
    "text": "انظر الخطة المرفقة لعام 2027",
    "files": [
      {
        "filename": "الخطة_التشغيلية_والموازنة_التقديرية_2027.xlsx",
        "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "content_base64": "UEsDBBQAAAAIA..."
      }
    ]
  }
}
```
*(Note: `handler.py` also accepts direct `text`, local `path`, or `url` inside `files` for local testing and asynchronous S3/signed-URL workflows).*

---

## 3. Resolving the Response Schema Difference (`programs`/`projects` vs. Single-Form Autofill vs. Legacy `programs_and_projects`)

### Investigation Findings:
1. **Golden Reference Contract (`athar_2027_programs_projects_contract.json`)**:
   - Expects top-level `"programs": [...]` and `"projects": [...]` arrays where each item has the exact 10 fields (`type`, `name`, `year`, `description`, `beneficiaries_count`, `budget`, `beneficiary_value`, `target_audience`, `delivery_method`, `notes`).
2. **Single-Entity Form Validator (`StoreProgramRequest::rules()`)**:
   - Described in `AI_PROGRAM_AUTOFILL_N8N_CONTRACT (2).md`: validates a single JSON object with the 10 fields (`type`, `name`, `year`, `description`, `beneficiaries_count`, `budget`, `beneficiary_value`, `target_audience`, `delivery_method`, `notes`) at the top level when pre-filling a single form.
3. **Legacy Model Output (`result.programs_and_projects`)**:
   - Previously returned `{"result": {"association_name": ..., "year": ..., "period": ..., "programs_and_projects": [...]}}` with `description: null`.

### Unified Backward- and Forward-Compatible Response Schema:
`postprocess.build_contract_response()` returns a single valid JSON object (`HTTP 200`) containing **all three** views simultaneously without conflict:
- **Top-level `"programs"` and `"projects"` arrays**: Every item strictly contains only the 10 business fields (matching `athar_2027_programs_projects_contract.json` 100%).
- **Top-level 10 fields (`type`, `name`, `year`, `description`, `beneficiaries_count`, `budget`, `beneficiary_value`, `target_audience`, `delivery_method`, `notes`)**: Populated from the first extracted entity so any frontend screen reading top-level fields for a single `StoreProgramRequest` form works immediately without breaking.
- **Legacy `"result"` wrapper**: Contains `association_name`, `year`, `period`, `programs`, `projects`, and `programs_and_projects`.

### Example Final Backend-Compatible Response
```json
{
  "type": "program",
  "name": "كفاية",
  "year": 2027,
  "description": "برنامج لتلبية احتياجات المستفيدين الأساسية من خلال مشاريع الغذاء والماء والكفالات والصحة والسكن والمساعدات الموسمية.",
  "beneficiaries_count": null,
  "budget": 3723725,
  "beneficiary_value": "تقديم مساعدات عينية ومالية وخدمات أساسية للأسر والأفراد المستحقين.",
  "target_audience": "جميع فئات المستفيدين",
  "delivery_method": "تنفيذ 13 مشروعًا تتنوع بين توزيع المواد والمساندة المالية والكفالات والخدمات الصحية.",
  "notes": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 8–9؛ رمز البرنامج: 1.1.A...",
  "programs": [
    {
      "type": "program",
      "name": "كفاية",
      "year": 2027,
      "description": "برنامج لتلبية احتياجات المستفيدين الأساسية من خلال مشاريع الغذاء والماء والكفالات والصحة والسكن والمساعدات الموسمية.",
      "beneficiaries_count": null,
      "budget": 3723725,
      "beneficiary_value": "تقديم مساعدات عينية ومالية وخدمات أساسية للأسر والأفراد المستحقين.",
      "target_audience": "جميع فئات المستفيدين",
      "delivery_method": "تنفيذ 13 مشروعًا تتنوع بين توزيع المواد والمساندة المالية والكفالات والخدمات الصحية.",
      "notes": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 8–9..."
    }
  ],
  "projects": [
    {
      "type": "project",
      "name": "السلة الغذائية",
      "year": 2027,
      "description": "توفير وتوزيع سلال غذائية للأسر والأفراد المستحقين وفق احتياجاتهم.",
      "beneficiaries_count": null,
      "budget": 1155000,
      "beneficiary_value": "تلبية جانب من الاحتياجات الغذائية للمستفيدين.",
      "target_audience": "جميع مستفيدي الجمعية",
      "delivery_method": "تجهيز السلال وتوزيعها مع توثيق الاستلام.",
      "notes": "المصدر: الخطة التشغيلية والموازنة التقديرية 2027 لجمعية البر الخيرية بالثنية (وصال)، ص 10؛ رمز المشروع: 1.1.A.1..."
    }
  ],
  "programs_and_projects": [
    "... (all 33 entities: 9 programs + 24 projects)"
  ],
  "summary": {
    "programs_count": 9,
    "projects_count": 24,
    "total_entities": 33,
    "association_name": "جمعية البر الخيرية بالثنية (وصال)",
    "year": 2027
  },
  "run_id": "42",
  "needs_attention": false
}
```

---

## 4. Handling the 60-Second Backend Timeout Constraint & Async Architecture Specification

### Why Synchronous 60s Works for Structured Plans Now:
- With `SKIP_LLM_FOR_STRUCTURED_CARD_PAGES=1`, structured operational plans and budget workbooks (like the 72-page Athar 2027 PDF, DOCX, XLSX, and MD) are extracted deterministically in **< 1.5 seconds**, completing well inside the backend's 60-second synchronous HTTP timeout.

### Integration Blocker for Unstructured Multi-Hundred-Page Scanned PDFs:
- The Laravel backend repository (`app/Domains/Ai/Runs/Services/N8nAiProvider.php` and `app/Domains/Organizations/Programs/Http/Requests/StoreProgramRequest.php`) is **external to this repository**.
- When a user uploads a 100+ page **unstructured narrative or scanned PDF** during a cold GPU start, OCR + multi-chunk 24B LLM inference can take **90–240 seconds**, which exceeds the synchronous 60s timeout in `N8nAiProvider.php`.

### Exact Backend & n8n Changes Required for Full Async Support + Bulk Multi-Entity Saving:

1. **Async Callback Endpoint in Laravel Backend (`N8nAiProvider.php`)**:
   - **Current behavior**: `N8nAiProvider` blocks synchronously for 60s and marks the `ai_runs` row as completed/failed from the single HTTP response.
   - **Recommended change**:
     - Add `POST /api/v1/internal/ai-runs/{run_id}/complete` accepting `{"status": "completed"|"failed", "result": {...}, "error": "..."}` authenticated via an `X-AI-Webhook-Secret` header.
     - Allow `N8nAiProvider` to accept HTTP `202 Accepted` (`{"status": "processing", "run_id": "42"}`) from n8n when a job is dispatched to RunPod `/run` (async), while keeping `GET /api/v1/organization/programs/autofill/{run_id}` (or existing `ai_runs` polling) for the frontend until status transitions to `completed`.
2. **Frontend / Backend Bulk Save Support**:
   - If the frontend autofill modal is designed to import all 33 extracted entities (`programs` + `projects`) at once rather than pre-filling a single form, add a batch endpoint `POST /api/v1/organization/programs/batch` that iterates over `programs` and `projects` and validates each item against `StoreProgramRequest::rules()`.
