# Program Autofill — n8n Contract

What the n8n workflow behind `POST /api/v1/organization/programs/autofill` receives, and
what it must return.

## Request — what the backend POSTs to the webhook

`N8nAiProvider` (`app/Domains/Ai/Runs/Services/N8nAiProvider.php`) sends **one
`multipart/form-data` POST**, the same shape for every AI feature:

| part | type | notes |
|---|---|---|
| `run_id` | text | the `ai_runs` row id — echo it back in logs if useful |
| `type` | text | the `AiRunType` value. Branch on this. `program_autofill` here; other features reuse the same webhook with a different `type` |
| `input` | text | a **JSON string** — parse it. The full run input. For autofill: `{ "text": "...", "files": ["brief.pdf"] }` (`text` absent if none sent; `files` is the ordered list of original filenames, absent if none) |
| `files[]` | file | 0–5 binary attachments, raw bytes. Same order as `input.files`. Two attachments may share a filename |

So the autofill workflow reads `JSON.parse($json.input).text` and the `files[]` binaries.
There is **no** top-level `text` field any more — it lives inside `input`.

The Webhook node must be set to **"Respond: When Last Node Finishes"** (see HTTP-level
contract below).

### What changed from the previous shape

The pasted text used to arrive as a **top-level `text` field**. It now lives inside the
`input` JSON string. `run_id`, `type`, `files[]`, and the whole **response** contract are
unchanged. Nothing was live, so there is nothing to migrate — just build to the new shape.

| scenario | before (fields sent) | after (fields sent) |
|---|---|---|
| **text only** | `run_id`, `type`, `text` | `run_id`, `type`, `input` = `{"text":"…"}` |
| **text + file** | `run_id`, `type`, `text`, `files[]` | `run_id`, `type`, `input` = `{"text":"…","files":["brief.pdf"]}`, `files[]` |
| **files only** | `run_id`, `type`, `files[]` *(`text` omitted)* | `run_id`, `type`, `input` = `{"files":["brief.xlsx"]}`, `files[]` |
| **neither** | `run_id`, `type` | `run_id`, `type`, `input` = `{}` |

Read: `JSON.parse($json.input).text` instead of `$json.text`. `input.files` is a new,
ordered filename list that lines up with the `files[]` binary parts by position (the
previous shape sent the binaries only, no name list).

## Request — Example (what n8n receives)

The webhook payload (`$json`), for a run with pasted text and one attached file.

**Before**

```json
{
  "run_id": "42",
  "type": "program_autofill",
  "text": "انظر الملف المرفق"
}
```

`text` is a plain field; the `files` name list was not sent (binaries only).

**After**

```json
{
  "run_id": "42",
  "type": "program_autofill",
  "input": "{
      \"text\": \"انظر الملف المرفق\",
      \"files\": [\"brief.pdf\"]
  }"
}
```

`input` is a single **JSON string** value (shown wrapped above for readability — on the wire
it is one line: `"{\"text\":\"انظر الملف المرفق\",\"files\":[\"brief.pdf\"]}"`).
`JSON.parse($json.input)` gives:

```json
{
  "text": "انظر الملف المرفق",
  "files": ["brief.pdf"]
}
```

- `text` — the pasted description. Absent when the user sent files only.
- `files` — ordered list of the original filenames, one per `files[]` binary part, same
  order (`files[][0]` ↔ `input.files[0]`). Match by position, not name — two files can share
  a name. Absent when no files were sent.
- Both absent → `input` is `"{}"`.

`files[]` binary parts are attached alongside `$json` in both versions, unchanged.

## Response — Example

```json
{
  "type": "program",
  "name": "Clean Water Initiative",
  "year": 2025,
  "description": "Provides potable water access to rural communities through well construction and maintenance training.",
  "beneficiaries_count": 1200,
  "budget": 45000.00,
  "beneficiary_value": "Each household gains reliable year-round access to clean drinking water.",
  "target_audience": "Rural households without piped water access",
  "delivery_method": "Community-led well construction with quarterly maintenance visits",
  "notes": "Budget figure extracted from the attached spreadsheet; confirm before saving."
}
```

## Field validation

Matches `StoreProgramRequest::rules()` exactly (`app/Domains/Organizations/Programs/Http/Requests/StoreProgramRequest.php`) — this is the form the frontend pre-fills from the result, so the shapes must line up.

| Field | Rule | Notes |
|---|---|---|
| `type` | must be `"program"` or `"project"` | the only two `ProgramType` enum values — anything else fails validation *at save time*, not at autofill time |
| `name` | required, string, max 255 | if n8n can't confidently extract a name, still return its best guess — an empty string fails at save time and the user has to fill it in manually |
| `year` | nullable, integer, 1900–2100 | omit or `null` if not found |
| `description` | nullable, string | |
| `beneficiaries_count` | nullable, integer, ≥ 0 | |
| `budget` | nullable, numeric, ≥ 0 | |
| `beneficiary_value` | nullable, string | |
| `target_audience` | nullable, string | |
| `delivery_method` | nullable, string | |
| `notes` | nullable, string | good place to flag low-confidence extractions in plain language — there's no separate confidence field |

## HTTP-level contract

- **Status**: must be `2xx`. Any other status is treated as failure — the run is marked `failed` with that as the error, and n8n should use this (not a field in the body) to signal it failed.
- **Body**: must be valid JSON, and specifically a JSON **object** (`{...}`) — not a bare array/list or a scalar value.
- **Timing**: the Webhook node must be set to **"Respond: When Last Node Finishes"**, not "Respond Immediately." Our backend waits synchronously on this single HTTP response (60s timeout) and treats it as the final result the instant it arrives — there is no separate callback or second signal. If the webhook responds early with an acknowledgment instead of the real result, that acknowledgment becomes the run's `result` and the actual extraction is lost.

## What's *not* currently enforced

None of the field-level table above is validated by our backend today (`app/Domains/Ai/Runs/Services/N8nAiProvider.php`) — only "2xx status + JSON object body" is checked. A malformed result (wrong field names, wrong types) is stored as-is and only surfaces as a failure later, when the frontend tries to save it via `POST /api/v1/organization/programs`. If stricter validation at the run level (reject early as `failed` rather than downstream) is wanted, that's a deliberate addition to make — not yet built.
