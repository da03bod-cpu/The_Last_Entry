"""
Automated test suite for the Athar NGO Program & Project Extraction System.

Covers all 20 mandatory testing areas:
  1. PDF ingestion (native selectable text)
  2. Scanned PDF OCR fallback
  3. XLSX ingestion (all worksheets, merged cells, numeric cells)
  4. DOCX ingestion (paragraphs, headings, tables in reading order)
  5. Markdown ingestion
  6. Text-only requests
  7. File-only requests
  8. Mixed text plus attachments
  9. Multiple documents (including duplicate filenames matched by position)
 10. Long documents (hundreds of pages without arbitrary character rejection)
 11. Oversized single pages
 12. Token-budget handling
 13. JSON parsing and repair
 14. Program/project classification (including initiative-level projects and event-named projects)
 15. Numeric grounding & KPI target filtering (e.g. 2,160 baskets != beneficiaries_count)
 16. Deduplication across page overlaps and repeated tables
 17. Missing fields & no-hallucination guarantees
 18. Incomplete generation & adaptive sub-splitting
 19. Model initialization failures & cache path discovery
 20. Backend/n8n contract validation & Golden Reference 33-entity benchmark comparison
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
from unittest.mock import patch

import docx
import openpyxl
import pymupdf
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config
import handler
import ingestion
import postprocess as pp

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
GOLD_JSON_PATH = os.path.join(FIXTURES_DIR, "athar_2027_programs_projects_contract.json")
MD_FIXTURE_PATH = os.path.join(FIXTURES_DIR, "الخطة_التشغيلية_والموازنة_التقديرية_2027.md")
DOCX_FIXTURE_PATH = os.path.join(FIXTURES_DIR, "الخطة_التشغيلية_والموازنة_التقديرية_2027.docx")
XLSX_FIXTURE_PATH = os.path.join(FIXTURES_DIR, "الخطة_التشغيلية_والموازنة_التقديرية_2027.xlsx")
SCRATCH_PDF_PATH = r"C:\Users\omari\.gemini\antigravity\brain\b49d2610-47b1-47df-845a-cf5b44f22c3b\scratch\athar_2027.pdf"


def _make_sample_pdf_bytes(pages_text: list[str]) -> bytes:
    """Create a valid in-memory PDF with the given text on each page."""
    doc = pymupdf.open()
    for txt in pages_text:
        page = doc.new_page()
        page.insert_text((50, 72), txt, fontsize=11)
    out = doc.tobytes()
    doc.close()
    return out


def _make_scanned_pdf_bytes() -> bytes:
    """Create a valid PDF page containing only an embedded image and no selectable text."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (400, 150), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.text((20, 50), "Scanned Arabic NGO Page", fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    doc = pymupdf.open()
    page = doc.new_page(width=400, height=150)
    page.insert_image(pymupdf.Rect(0, 0, 400, 150), stream=png_bytes)
    out = doc.tobytes()
    doc.close()
    return out


# ----------------------------------------------------------------------
# 1 & 2. PDF Ingestion & Scanned PDF OCR
# ----------------------------------------------------------------------
def test_pdf_native_ingestion():
    pdf_bytes = _make_sample_pdf_bytes(
        [
            "Association Annual Report 2027 - Page 1 with native selectable text that is long enough to skip OCR.",
            "Page 2 content describing community development programs and projects with clear native text.",
        ]
    )
    res = ingestion.ingest_pdf_bytes(pdf_bytes, filename="report.pdf", enable_ocr=True)
    assert res["format"] == "pdf"
    assert res["page_count"] == 2
    assert res["ocr_pages"] == []  # Native text was sufficient, OCR skipped
    assert "===== PAGE 1 =====" in res["text"]
    assert "===== PAGE 2 =====" in res["text"]


def test_scanned_pdf_ocr_fallback():
    scanned_bytes = _make_scanned_pdf_bytes()
    with patch("ingestion._ocr_pdf_page", return_value="برنامج كفالة الأيتام - التكلفة 50000 ريال") as mock_ocr:
        res = ingestion.ingest_pdf_bytes(scanned_bytes, filename="scanned.pdf", enable_ocr=True)
        assert mock_ocr.called
        assert res["ocr_pages"] == [1]
        assert "برنامج كفالة الأيتام" in res["text"]


def test_invalid_pdf_raises_meaningful_error():
    with pytest.raises(ingestion.IngestionError, match="Invalid or corrupted PDF"):
        ingestion.ingest_pdf_bytes(b"not a pdf file", filename="bad.pdf")


# ----------------------------------------------------------------------
# 3. XLSX Ingestion (Multiple Sheets, Merged Cells, Numeric Values)
# ----------------------------------------------------------------------
def test_xlsx_ingestion_fixture_and_merged_cells():
    with open(XLSX_FIXTURE_PATH, "rb") as f:
        xlsx_bytes = f.read()
    res = ingestion.ingest_xlsx_bytes(xlsx_bytes, filename="plan_2027.xlsx")
    assert res["format"] == "xlsx"
    assert res["page_count"] >= 72
    sheet_names = [s["sheet"] for s in res["sheets"]]
    assert "ملخص الموازنة" in sheet_names
    assert "نص الصفحات" in sheet_names
    assert "مشاريع برنامج كفاية" in sheet_names
    assert "السلة الغذائية" in res["text"]
    assert "1155000" in res["text"] or "1,155,000" in res["text"]

    # Also test a custom workbook with merged cells and floats
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "المشاريع"
    ws.append(["البرنامج", "المشروع", "الموازنة"])
    ws.append(["برنامج الرعاية", "مشروع السقيا", 79920.0])
    ws.append([None, "مشروع الكسوة", 176280])
    ws.merge_cells("A2:A3")
    buf = io.BytesIO()
    wb.save(buf)

    custom_res = ingestion.ingest_xlsx_bytes(buf.getvalue(), filename="merged.xlsx")
    assert "برنامج الرعاية" in custom_res["text"]
    assert "79920" in custom_res["text"]
    assert "176280" in custom_res["text"]


# ----------------------------------------------------------------------
# 4 & 5. DOCX and Markdown Ingestion
# ----------------------------------------------------------------------
def test_docx_ingestion_fixture_and_tables():
    with open(DOCX_FIXTURE_PATH, "rb") as f:
        docx_bytes = f.read()
    res = ingestion.ingest_docx_bytes(docx_bytes, filename="plan_2027.docx")
    assert res["format"] == "docx"
    assert res["page_count"] == 72
    assert "===== PAGE 10 =====" in res["text"]

    # Also test a DOCX with an embedded table
    d = docx.Document()
    d.add_heading("صفحة 1", level=2)
    d.add_paragraph("جمعية البر الخيرية")
    tbl = d.add_table(rows=2, cols=2)
    tbl.cell(0, 0).text = "اسم المشروع"
    tbl.cell(0, 1).text = "التكلفة"
    tbl.cell(1, 0).text = "السلة الغذائية"
    tbl.cell(1, 1).text = "1155000"
    buf = io.BytesIO()
    d.save(buf)

    tbl_res = ingestion.ingest_docx_bytes(buf.getvalue(), filename="with_table.docx")
    assert "السلة الغذائية | 1155000" in tbl_res["text"]


def test_markdown_ingestion_fixture():
    with open(MD_FIXTURE_PATH, encoding="utf-8") as f:
        md_text = f.read()
    res = ingestion.ingest_markdown_or_text(md_text, filename="plan_2027.md")
    assert res["format"] == "markdown"
    assert res["page_count"] == 72
    assert "===== PAGE 8 =====" in res["text"]
    assert "===== PAGE 62 =====" in res["text"]


# ----------------------------------------------------------------------
# 6, 7, 8, 9. Request Variants (Text-only, File-only, Mixed, Multi-file & Duplicate Names)
# ----------------------------------------------------------------------
def test_text_only_file_only_mixed_and_duplicate_filenames():
    # 6. Text-only via n8n contract `input` JSON string
    pasted = "===== PAGE 8 =====\n2027\nبرنامج1.1.A\n08كفاية\n3,723,725برنامج مرتبط بمشاريع1.1\nوصف البرنامج\nالمستفيدين.\nجميع فئات المستفيدين\nالفئات المستهدفة"
    req_text_only = {
        "run_id": "101",
        "type": "program_autofill",
        "input": json.dumps({"text": pasted}, ensure_ascii=False),
    }
    res_text = handler.handler({"input": req_text_only})
    assert res_text["run_id"] == "101"
    assert len(res_text["programs"]) == 1
    assert res_text["programs"][0]["name"] == "كفاية"
    assert res_text["programs"][0]["budget"] == 3723725

    # 7. File-only request
    b64_md = base64.b64encode(pasted.encode("utf-8")).decode("ascii")
    req_file_only = {
        "run_id": "102",
        "type": "program_autofill",
        "input": json.dumps({"files": ["brief.md"]}, ensure_ascii=False),
        "files": [{"content_base64": b64_md}],
    }
    res_file = handler.handler({"input": req_file_only})
    assert res_file["run_id"] == "102"
    assert res_file["sources"][0]["filename"] == "brief.md"
    assert len(res_file["programs"]) == 1

    # 8 & 9. Mixed text + multiple files with identical filenames matched by position
    p2 = "===== PAGE 10 =====\n2027\nمشروع1.1.A.1\n10السلة الغذائية\n1,155,000\n12 شهر\nوصف المشروع\nأهداف المشروع\nجميع مستفيدي الجمعية"
    req_mixed_dup = {
        "run_id": "103",
        "type": "program_autofill",
        "input": json.dumps({"text": "مقدمة عامة للخطة", "files": ["part.md", "part.md"]}, ensure_ascii=False),
        "files": [
            {"content_base64": base64.b64encode(pasted.encode("utf-8")).decode("ascii")},
            {"content_base64": base64.b64encode(p2.encode("utf-8")).decode("ascii")},
        ],
    }
    res_mixed = handler.handler({"input": req_mixed_dup})
    assert len(res_mixed["programs"]) == 1
    assert len(res_mixed["projects"]) == 1
    assert res_mixed["projects"][0]["name"] == "السلة الغذائية"


# ----------------------------------------------------------------------
# 10, 11, 12. Long Documents, Oversized Single Pages, Token-Budget Handling
# ----------------------------------------------------------------------
def test_long_document_and_oversized_single_page_chunking():
    # 150 pages + 1 oversized page of 35,000 chars
    pages = [(i, f"محتوى الصفحة رقم {i} " * 40) for i in range(1, 151)]
    pages.append((151, "فقرة طويلة جدًا في صفحة واحدة " * 1200))  # ~36,000 chars

    chunks = pp.build_chunks(pages, target_chars=10000, overlap_pages=1)
    assert len(chunks) >= 12
    # Every page number from 1..151 must be present in chunks (no silent skipping)
    covered_pages = {p_no for ch in chunks for p_no, _ in ch}
    assert covered_pages == set(range(1, 152))

    # Verify token budget splitter further splits any chunk that exceeds a tight token budget
    meta = pp.build_meta_block("جمعية اختبار", 2027, None)
    tight_budget = 600
    for ch in chunks[-3:]:
        subchunks = handler._split_chunk_to_token_budget(ch, meta, tight_budget)
        assert len(subchunks) >= 1
        for sc in subchunks:
            assert handler._count_input_tokens(meta + pp.render(sc)) <= tight_budget


# ----------------------------------------------------------------------
# 13, 14, 15, 16, 17, 18. JSON Repair, Classification, Grounding, Deduplication, Truncation Recovery
# ----------------------------------------------------------------------
def test_json_parsing_and_repair():
    # Valid JSON
    obj, rep = pp.parse_json_output('{"programs_and_projects": [{"type": "program", "name": "كفاية"}]}')
    assert obj is not None and not rep

    # Broken/truncated JSON repaired by json-repair
    broken = '```json\n{"programs_and_projects": [{"type": "project", "name": "السلة الغذائية", "budget": 1155000, }]}'
    obj2, _rep2 = pp.parse_json_output(broken)
    assert isinstance(obj2, dict)
    assert obj2["programs_and_projects"][0]["name"] == "السلة الغذائية"


def test_classification_and_numeric_grounding_rules():
    # Strategic goals and financial summary rows must be rejected
    assert pp.validate_entity_schema({"type": "program", "name": "إجمالي مصروفات الأنشطة", "budget": 4852025}) is None
    assert pp.validate_entity_schema({"type": "program", "name": "تعظيم الأثر التنموي للمستفيدين"}) is None
    assert pp.validate_entity_schema({"type": "program", "name": "الموازنة الشهرية"}) is None

    # KPI basket count (2,160) must NOT become beneficiaries_count
    chunk_text = "مشروع السلة الغذائية التكلفة 1,155,000 مؤشر الأداء المستهدف 2,160 عدد السلال الغذائية خط الأساس 2,016"
    review: list[dict[str, str]] = []
    cleaned = pp.clean_program(
        {
            "type": "project",
            "name": "السلة الغذائية",
            "budget": 1155000,
            "beneficiaries_count": 2160,
            "year": 2027,
        },
        pp.norm(chunk_text),
        review,
        chunk_raw_text=chunk_text,
    )
    assert cleaned is not None
    assert cleaned["budget"] == 1155000
    assert cleaned["beneficiaries_count"] is None
    assert any("مؤشر أداء" in r["reason"] for r in review)

    # Hallucinated budget not in text must be nulled
    cleaned_hallucinated = pp.clean_program(
        {"type": "project", "name": "مشروع تجريبي", "budget": 999999},
        pp.norm(chunk_text),
        review,
        chunk_raw_text=chunk_text,
    )
    assert cleaned_hallucinated is not None
    assert cleaned_hallucinated["budget"] is None


def test_deduplication_preserves_similar_program_vs_project():
    # `حضور وأثر` (program) and `حضور وصال` (project) must NOT be merged together
    c1 = [{"type": "program", "name": "حضور وأثر", "budget": 25000, "beneficiaries_count": None}]
    c2 = [
        {"type": "program", "name": "حضور وأثر", "budget": 25000, "target_audience": "الإعلام والمتطوعون"},
        {"type": "project", "name": "حضور وصال", "budget": 10000, "beneficiaries_count": None},
    ]
    merged = pp.merge_programs([c1, c2], thr=0.9)
    assert len(merged) == 2
    assert merged[0]["name"] == "حضور وأثر" and merged[0]["target_audience"] == "الإعلام والمتطوعون"
    assert merged[1]["name"] == "حضور وصال" and merged[1]["type"] == "project"


def test_incomplete_generation_adaptive_split_recovery():
    # Simulate a 2-page unstructured narrative chunk where full-chunk generation truncates (`max_new_tokens`),
    # triggering bounded adaptive sub-splitting into 1-page calls that succeed.
    narrative_p1 = "===== PAGE 1 =====\n" + ("تقرير سردي عن أنشطة الجمعية الخيرية وتفاصيل المبادرات الميدانية. " * 5) + "توزيع 500 سلة بميزانية 250000 ريال."
    narrative_p2 = "===== PAGE 2 =====\n" + ("استكمال التقرير السردي السنوي لأنشطة الجمعية الخيرية في المحافظة. " * 5) + "برنامج رعاية الأرامل بميزانية 180000 ريال."

    call_count = 0

    def fake_run_chunk(user_text: str):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First call on combined 2-page chunk truncates with broken JSON
            return '{"programs_and_projects": [{"type": "project", "name": "مبادرة الإطعام"', {
                "input_tokens": 500,
                "generated_tokens": 6144,
                "stop_reason": "max_new_tokens",
                "retried": False,
            }
        if "250000" in user_text:
            return json.dumps(
                {
                    "programs_and_projects": [
                        {
                            "type": "project",
                            "name": "مبادرة الإطعام",
                            "year": 2027,
                            "description": None,
                            "beneficiaries_count": None,
                            "budget": 250000,
                            "beneficiary_value": "توفير الغذاء للأسر",
                            "target_audience": "الأسر المحتاجة",
                            "delivery_method": "توزيع ميداني",
                            "notes": None,
                        }
                    ]
                }
            ), {"input_tokens": 250, "generated_tokens": 120, "stop_reason": "eos", "retried": False}
        return json.dumps(
            {
                "programs_and_projects": [
                    {
                        "type": "program",
                        "name": "برنامج رعاية الأرامل",
                        "year": 2027,
                        "description": None,
                        "beneficiaries_count": None,
                        "budget": 180000,
                        "beneficiary_value": "دعم الأرامل",
                        "target_audience": "الأرامل",
                        "delivery_method": "كفالات شهرية",
                        "notes": None,
                    }
                ]
            }
        ), {"input_tokens": 250, "generated_tokens": 120, "stop_reason": "eos", "retried": False}

    with patch("handler.run_chunk", side_effect=fake_run_chunk):
        res = handler.extract_document({"text": narrative_p1 + "\n\n" + narrative_p2, "year": 2027})
        assert call_count == 3  # 1 initial + 2 adaptive sub-chunk calls
        assert res["chunks"][0].get("adaptive_split_recovered") is True
        assert not res["needs_attention"]
        assert len(res["programs"]) == 1
        assert len(res["projects"]) == 1
        # Verify description was deterministically synthesized from delivery_method + beneficiary_value
        assert res["projects"][0]["description"] is not None


# ----------------------------------------------------------------------
# 19. Model Initialization Failure & Cache Discovery
# ----------------------------------------------------------------------
def test_model_cache_discovery_and_init_failure_handling(tmp_path):
    # Create a fake RunPod cache directory structure
    snap_dir = tmp_path / "models--mistralai--Mistral-Small-24B-Instruct-2501" / "snapshots" / "abc1234"
    snap_dir.mkdir(parents=True)
    (snap_dir / "config.json").write_text('{"model_type": "mistral"}', encoding="utf-8")
    refs_dir = tmp_path / "models--mistralai--Mistral-Small-24B-Instruct-2501" / "refs"
    refs_dir.mkdir(parents=True)
    (refs_dir / "main").write_text("abc1234", encoding="utf-8")

    resolved = handler.resolve_base_path("mistralai/Mistral-Small-24B-Instruct-2501", cache_roots=[str(tmp_path)])
    assert resolved == str(snap_dir)

    # Health check without loading GPU model
    hc = handler.handler({"input": {"action": "health_check"}})
    assert hc["status"] in ("container_ready", "ready")

    # Missing LORA_REPO raises clear error when resolve_lora_path is called without local path
    with (
        patch.object(config, "LORA_PATH", ""),
        patch.object(config, "LORA_REPO", "YOUR-HF-USERNAME/nonprofit-lora"),
        patch.dict(os.environ, {"LORA_REPO": ""}, clear=False),
        pytest.raises(RuntimeError, match="LORA_REPO environment variable is not configured"),
    ):
        handler.resolve_lora_path()


# ----------------------------------------------------------------------
# 20. Golden Reference 33-Entity Benchmark Comparison (MD, DOCX, XLSX, PDF)
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "fmt,fixture_path",
    [
        ("markdown", MD_FIXTURE_PATH),
        ("docx", DOCX_FIXTURE_PATH),
        ("xlsx", XLSX_FIXTURE_PATH),
        pytest.param(
            "pdf",
            SCRATCH_PDF_PATH,
            marks=pytest.mark.skipif(not os.path.isfile(SCRATCH_PDF_PATH), reason="Original PDF not in scratch dir"),
        ),
    ],
)
def test_golden_reference_extraction_benchmark(fmt: str, fixture_path: str):
    with open(GOLD_JSON_PATH, encoding="utf-8") as f:
        gold = json.load(f)

    res = handler.handler(
        {
            "input": {
                "run_id": f"bench-{fmt}",
                "type": "program_autofill",
                "input": json.dumps({"files": [os.path.basename(fixture_path)]}, ensure_ascii=False),
                "files": [{"filename": os.path.basename(fixture_path), "path": fixture_path}],
            }
        }
    )

    assert "error" not in res
    programs = res["programs"]
    projects = res["projects"]

    # 9 programs + 24 projects = 33 total entities
    assert len(programs) == 9
    assert len(projects) == 24
    assert res["summary"]["total_entities"] == 33

    # Verify mandatory golden test cases
    prog_by_name = {p["name"]: p for p in programs}
    proj_by_name = {p["name"]: p for p in projects}

    assert prog_by_name["كفاية"]["type"] == "program"
    assert prog_by_name["كفاية"]["budget"] == 3723725

    assert proj_by_name["السلة الغذائية"]["type"] == "project"
    assert proj_by_name["السلة الغذائية"]["budget"] == 1155000
    assert proj_by_name["السلة الغذائية"]["beneficiaries_count"] is None  # 2,160 baskets != beneficiaries

    assert proj_by_name["فرصتك تصنع مستقبلك"]["type"] == "project"
    assert proj_by_name["فرصتك تصنع مستقبلك"]["budget"] == 12000

    assert proj_by_name["حضور وصال"]["type"] == "project"
    assert proj_by_name["حضور وصال"]["budget"] == 10000

    # Verify exact 10-field schema compliance and field-by-field match with golden reference
    for gold_prog, got_prog in zip(gold["programs"], programs):
        assert list(got_prog.keys()) == pp.FIELDS
        assert got_prog == gold_prog

    for gold_proj, got_proj in zip(gold["projects"], projects):
        assert list(got_proj.keys()) == pp.FIELDS
        assert got_proj == gold_proj
