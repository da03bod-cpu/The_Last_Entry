# Test & Golden Reference Benchmark Report

## 1. Automated Unit & Pipeline Test Suite Results

Command executed:
```bash
python -m pytest -v
python -m ruff check config.py ingestion.py postprocess.py handler.py benchmark_report.py tests/
```

**Outcome**: **19 passed, 0 failed** (`2.59s`), Ruff linter: **All checks passed!**

| # | Test Case | Area Covered | Status |
|---|---|---|---|
| 1 | `test_pdf_native_ingestion` | Native PDF text extraction & OCR skip when selectable text exists | **PASSED** |
| 2 | `test_scanned_pdf_ocr_fallback` | Automatic Arabic+English OCR fallback on scanned image-only PDF pages | **PASSED** |
| 3 | `test_invalid_pdf_raises_meaningful_error` | Validation & meaningful error reporting on corrupted/non-PDF bytes | **PASSED** |
| 4 | `test_xlsx_ingestion_fixture_and_merged_cells` | Multi-worksheet Excel parsing (`ملخص الموازنة`, `نص الصفحات`, `مشاريع برنامج كفاية`), merged cells, numeric values | **PASSED** |
| 5 | `test_docx_ingestion_fixture_and_tables` | Word document paragraphs, headings, and table cells in exact reading order | **PASSED** |
| 6 | `test_markdown_ingestion_fixture` | Markdown page splitting (`## صفحة N`) and corrupted glyph filtering | **PASSED** |
| 7 | `test_text_only_file_only_mixed_and_duplicate_filenames` | Backend/n8n `input` JSON string parsing for text-only, file-only, mixed, and duplicate filenames matched by position | **PASSED** |
| 8 | `test_long_document_and_oversized_single_page_chunking` | 150+ page document + 35,000-char single page chunking with zero dropped pages | **PASSED** |
| 9 | `test_json_parsing_and_repair` | Native JSON parsing + `json-repair` recovery on truncated/malformed model output | **PASSED** |
| 10 | `test_classification_and_numeric_grounding_rules` | Filtering of financial totals/strategic goals, KPI quantity vs. `beneficiaries_count` protection (`2,160` baskets != beneficiaries), ungrounded number nulling | **PASSED** |
| 11 | `test_deduplication_preserves_similar_program_vs_project` | Cross-chunk deduplication while preserving distinct entities with similar names (`حضور وأثر` program vs. `حضور وصال` project) | **PASSED** |
| 12 | `test_incomplete_generation_adaptive_split_recovery` | Bounded adaptive sub-splitting when a chunk hits `max_new_tokens` or JSON parse error, plus evidence-based description synthesis | **PASSED** |
| 13 | `test_model_cache_discovery_and_init_failure_handling` | Multi-path RunPod cache discovery (`models--org--name`), container readiness health check, and missing `LORA_REPO` guard | **PASSED** |
| 14 | `test_golden_reference_extraction_benchmark[markdown]` | End-to-end extraction on `الخطة_التشغيلية_والموازنة_التقديرية_2027.md` vs. Golden JSON | **PASSED** |
| 15 | `test_golden_reference_extraction_benchmark[docx]` | End-to-end extraction on `الخطة_التشغيلية_والموازنة_التقديرية_2027.docx` vs. Golden JSON | **PASSED** |
| 16 | `test_golden_reference_extraction_benchmark[xlsx]` | End-to-end extraction on `الخطة_التشغيلية_والموازنة_التقديرية_2027.xlsx` vs. Golden JSON | **PASSED** |
| 17 | `test_golden_reference_extraction_benchmark[pdf]` | End-to-end extraction on original 72-page PDF (`جمعية البر الخيرية بالثنية (وصال) الخطة التشغيلية والموازنة التقديرية 2027.pdf`) vs. Golden JSON | **PASSED** |
| 18 | `test_zamzam_2023_annual_report_regression` | 56-page Zamzam 2023 Annual Report regression test: missing entities (`العلاج الخيري`, `جهازك حياة`, `العيادات الموسمية (الحج)`), umbrella programs (`تعزيز الصحة والوقاية`, `البرامج الميدانية`, `معهد زمزم المعرفي للتدريب`), 5 preserved beneficiary counts (`28,428`, `2,372`, `160`, `1,165`, `402`), 6 `مشروع` classifications, distinct `description` vs `delivery_method`, verbatim name snapping, and `needs_attention` diagnostics | **PASSED** |
| 19 | `test_independent_2028_extraction` | Independent 2028 operational plan (`جمعية الأمل للتنمية المجتمعية`): program `جسور المعرفة` + subordinate projects `مهارات الهاتف الذكي` and `نافذة الخدمات الإلكترونية` | **PASSED** |

---

## 2. Golden Reference Extraction Comparison Report

Command executed:
```bash
python benchmark_report.py
```

Reference file: [`tests/fixtures/athar_2027_programs_projects_contract.json`](file:///d:/bod/The_Last_Entry/tests/fixtures/athar_2027_programs_projects_contract.json)
- Expected Programs: **9**
- Expected Projects: **24**
- Expected Total Entities: **33**

### Summary Metrics by Document Format

| Input Document Format | Programs Extracted | Projects Extracted | Total Entities | Recall | Precision | False Positives | Missing Entities | Execution Time |
|---|---|---|---|---|---|---|---|---|
| **Markdown (`.md`)** | 9 / 9 | 24 / 24 | 33 / 33 | **100.0%** | **100.0%** | 0 | 0 | `0.035s` |
| **Word (`.docx`)** | 9 / 9 | 24 / 24 | 33 / 33 | **100.0%** | **100.0%** | 0 | 0 | `0.322s` |
| **Excel (`.xlsx`)** | 9 / 9 | 24 / 24 | 33 / 33 | **100.0%** | **100.0%** | 0 | 0 | `0.333s` |
| **Original PDF (`.pdf`)** | 9 / 9 | 24 / 24 | 33 / 33 | **100.0%** | **100.0%** | 0 | 0 | `1.339s` |

### Per-Field Accuracy Across All 33 Entities

| Schema Field | `.md` Accuracy | `.docx` Accuracy | `.xlsx` Accuracy | `.pdf` Accuracy | Notes |
|---|---|---|---|---|---|
| `type` | 100.0% (33/33) | 100.0% (33/33) | 100.0% (33/33) | 100.0% (33/33) | Strictly `"program"` (9) or `"project"` (24) |
| `name` | 100.0% (33/33) | 100.0% (33/33) | 100.0% (33/33) | 100.0% (33/33) | Exact Arabic names preserved |
| `year` | 100.0% (33/33) | 100.0% (33/33) | 100.0% (33/33) | 100.0% (33/33) | `2027` |
| `description` | 100.0% (33/33) | 100.0% (33/33) | 100.0% (33/33) | 100.0% (33/33) | Populated via evidence-based enrichment (resolving legacy `description=null`) |
| `beneficiaries_count` | 100.0% (33/33) | 100.0% (33/33) | 100.0% (33/33) | 100.0% (33/33) | All `null`; KPI targets (`2,160` baskets, `3,000` meals) never misassigned |
| `budget` | 100.0% (33/33) | 100.0% (33/33) | 100.0% (33/33) | 100.0% (33/33) | Exact numeric budgets grounded in card headers & budget tables |
| `beneficiary_value` | 100.0% (33/33) | 100.0% (33/33) | 100.0% (33/33) | 100.0% (33/33) | Grounded in card goals & results |
| `target_audience` | 100.0% (33/33) | 100.0% (33/33) | 100.0% (33/33) | 100.0% (33/33) | Grounded in `الفئات المستهدفة` |
| `delivery_method` | 100.0% (33/33) | 100.0% (33/33) | 100.0% (33/33) | 100.0% (33/33) | Grounded in scope, resources, and subordinate project lists |
| `notes` | 100.0% (33/33) | 100.0% (33/33) | 100.0% (33/33) | 100.0% (33/33) | Preserves page references, entity codes, durations, KPIs, and risk ratings |

### Mandatory Edge-Case Verification
- **`كفاية`**: Extracted as `type: "program"`, `budget: 3723725`, `beneficiaries_count: null`.
- **`السلة الغذائية`**: Extracted as `type: "project"`, `budget: 1155000`, `beneficiaries_count: null` (target of `2,160` baskets recorded in `notes`, NOT `beneficiaries_count`).
- **`فرصتك تصنع مستقبلك`**: Extracted as `type: "project"`, `budget: 12000` (code `1.1.-.1`, directly associated with initiative `1.1` without a parent program).
- **`حضور وصال`**: Extracted as `type: "project"`, `budget: 10000` (preserved as a genuine project despite involving conferences/events, and kept distinct from umbrella program `حضور وأثر`).

---

## 3. Distinction Between Locally Verified Behavior vs. Live GPU / OCR Environment

1. **Locally Verified (Executed & Confirmed on This Machine)**:
   - Native PDF ingestion (`pymupdf`) on the 72-page original Athar 2027 PDF, plus DOCX (`python-docx`), XLSX (`openpyxl`), and Markdown ingestion.
   - Structural card extraction, evidence-based enrichment, numeric grounding, KPI-vs-beneficiary disambiguation, and deduplication across all 4 formats (100% match with the 33-entity golden reference JSON).
   - Adaptive chunk splitting, token budget enforcement, `json-repair` recovery, and `handler.py` request/response formatting.
   - Scanned PDF detection and OCR invocation flow (tested via mocked OCR engine in `test_scanned_pdf_ocr_fallback`).
2. **Requires Container / RunPod GPU Environment at Deployment Time**:
   - Live `Mistral-Small-24B-Instruct-2501` 4-bit NF4 + LoRA v3 GPU inference on unstructured narrative documents requires an NVIDIA A40 GPU on RunPod Serverless with `LORA_REPO` and `HF_TOKEN` configured.
   - Live Arabic Tesseract OCR (`tesseract-ocr-ara`) binary execution runs inside the Linux Docker container built by `Dockerfile`.
