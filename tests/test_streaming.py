"""
Automated streaming test suite for Program & Project Autofill (`d:/bod/The_Last_Entry`).

Covers:
  - One program, one project, multiple programs, multiple projects, mixed (2 programs + 3 projects)
  - Arabic-language input (Athar 2027 deck & Zamzam 2023 report) and mixed Arabic + English input
  - Missing optional fields (strictly null, never fabricated)
  - Empty or invalid input (explicit failure, never false success)
  - Duplicate item suppression across chunks and within a chunk
  - Failed individual extraction (partial failure -> `item_failed` + `partial_failure=True`)
  - Terminal failure after >= 1 successful `item_completed` events
  - Retries & deterministic idempotent `event_id` generation
  - Final `completed` event schema and strict JSON-serializability
  - Legacy / non-streaming behavior and non-regression on `health_check`/`warmup`/`ping`
  - Proof of real incremental emission before remaining items/chunks finish
  - Local simulation through the real installed `runpod` SDK (`rp_job.handle_job`)

NOTE: These tests use deterministic fixtures and mocked LLM streams (non-GPU).
Live NVIDIA GPU / RunPod cloud endpoint tests are separate and cannot run offline.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from typing import Any
from unittest.mock import patch

import pytest
import runpod
from runpod.serverless.modules import rp_handler, rp_job

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config
import handler
import postprocess as pp
import streaming

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
ATHAR_MD_PATH = os.path.join(FIXTURES_DIR, "الخطة_التشغيلية_والموازنة_التقديرية_2027.md")
ZAMZAM_MD_PATH = os.path.join(FIXTURES_DIR, "التقرير_السنوي_جمعية_زمزم_2023.md")


def _make_mock_chunk_stream(entities: list[Any], *, stop_reason: str = "eos", raw_text_override: str | None = None):
    """Build a fake `run_chunk_stream` generator yielding entities one by one."""
    payload = raw_text_override or json.dumps({"programs_and_projects": entities}, ensure_ascii=False)

    def _gen(user_text: str, cancel_event: threading.Event | None = None):
        _ = user_text, cancel_event
        for item in entities:
            yield ("item", item)
        yield (
            "chunk_meta",
            payload,
            {
                "input_tokens": 120,
                "generated_tokens": 85,
                "stop_reason": stop_reason,
                "retried": False,
                "parser_state": "DONE",
                "streamed_count": len(entities),
                "parser_errors": 0,
            },
        )

    return _gen


def _assert_valid_10_fields(data: dict[str, Any]) -> None:
    assert list(data.keys()) == pp.FIELDS
    assert data["type"] in ("program", "project")
    assert isinstance(data["name"], str) and 1 <= len(data["name"]) <= 255
    if data["year"] is not None:
        assert isinstance(data["year"], int) and 1900 <= data["year"] <= 2100
    if data["beneficiaries_count"] is not None:
        assert isinstance(data["beneficiaries_count"], int) and data["beneficiaries_count"] >= 0
    if data["budget"] is not None:
        assert isinstance(data["budget"], (int, float)) and data["budget"] >= 0


# ----------------------------------------------------------------------
# 1–5. Single/Multiple Programs, Projects, and Mixed (2 Programs + 3 Projects)
# ----------------------------------------------------------------------
def test_stream_single_program():
    doc = "برنامج تمكين الأسر بميزانية 150000 ريال ويستفيد منه 320 مستفيدًا في عام 2027."
    mock_fn = _make_mock_chunk_stream(
        [
            {
                "type": "program",
                "name": "برنامج تمكين الأسر",
                "year": 2027,
                "description": None,
                "beneficiaries_count": 320,
                "budget": 150000,
                "beneficiary_value": "تحسين دخل الأسر",
                "target_audience": "الأسر المستحقة",
                "delivery_method": "ورش تدريبية ودعم مشاريع صغيرة",
                "notes": None,
            }
        ]
    )
    with patch("handler.run_chunk_stream", side_effect=mock_fn):
        events = list(handler.stream_handler({"input": {"run_id": "r-1", "text": doc, "stream": True}}))

    completed_items = [e for e in events if e["event"] == "item_completed"]
    assert len(completed_items) == 1
    assert completed_items[0]["item_type"] == "program"
    assert completed_items[0]["sequence"] == 1
    _assert_valid_10_fields(completed_items[0]["data"])

    final = events[-1]
    assert final["event"] == "completed"
    assert final["programs_count"] == 1
    assert final["projects_count"] == 0
    assert final["total_items"] == 1


def test_stream_single_project():
    doc = "مشروع كسوة الشتاء بتكلفة 45000 ريال لخدمة 250 مستفيد خلال عام 2026."
    mock_fn = _make_mock_chunk_stream(
        [
            {
                "type": "project",
                "name": "مشروع كسوة الشتاء",
                "year": 2026,
                "description": None,
                "beneficiaries_count": 250,
                "budget": 45000,
                "beneficiary_value": "توفير ملابس الشتاء",
                "target_audience": "الأسر الأشد حاجة",
                "delivery_method": "توزيع قسائم وحقائب كسوة عبر الفرق الميدانية",
                "notes": None,
            }
        ]
    )
    with patch("handler.run_chunk_stream", side_effect=mock_fn):
        events = list(handler.stream_handler({"input": {"run_id": "r-2", "text": doc, "stream": True}}))

    items = [e for e in events if e["event"] == "item_completed"]
    assert len(items) == 1
    assert items[0]["item_type"] == "project"
    assert items[0]["data"]["budget"] == 45000
    assert events[-1]["programs_count"] == 0
    assert events[-1]["projects_count"] == 1
    assert events[-1]["total_items"] == 1


def test_stream_multiple_programs_and_multiple_projects():
    doc = "تقرير 2027 يحتوي على برنامج الرعاية، برنامج التعليم، برنامج الإغاثة بميزانيات 10000 و 20000 و 30000."
    mock_progs = _make_mock_chunk_stream(
        [
            {"type": "program", "name": "برنامج الرعاية", "budget": 10000},
            {"type": "program", "name": "برنامج التعليم", "budget": 20000},
            {"type": "program", "name": "برنامج الإغاثة", "budget": 30000},
        ]
    )
    with patch("handler.run_chunk_stream", side_effect=mock_progs):
        ev_p = list(handler.stream_handler({"input": {"run_id": "r-3", "text": doc, "stream": True}}))
    assert [e["sequence"] for e in ev_p if e["event"] == "item_completed"] == [1, 2, 3]
    assert ev_p[-1]["programs_count"] == 3
    assert ev_p[-1]["projects_count"] == 0

    mock_projs = _make_mock_chunk_stream(
        [
            {"type": "project", "name": "مشروع السقيا", "budget": 10000},
            {"type": "project", "name": "مشروع الغذاء", "budget": 20000},
            {"type": "project", "name": "مشروع الدواء", "budget": 30000},
        ]
    )
    with patch("handler.run_chunk_stream", side_effect=mock_projs):
        ev_j = list(handler.stream_handler({"input": {"run_id": "r-4", "text": doc, "stream": True}}))
    assert [e["sequence"] for e in ev_j if e["event"] == "item_completed"] == [1, 2, 3]
    assert ev_j[-1]["programs_count"] == 0
    assert ev_j[-1]["projects_count"] == 3


def test_stream_mixed_two_programs_and_three_projects():
    doc = (
        "خطة الجمعية لعام 2027: برنامج التمكين 500000 ريال لعدد 400 مستفيد، برنامج التوعية 120000 ريال، "
        "ومشروع السلال 90000 ريال لعدد 300 مستفيد، ومشروع التأهيل 80000 ريال، ومشروع الكفالة 150000 ريال."
    )
    mixed_entities = [
        {"type": "program", "name": "برنامج التمكين", "budget": 500000, "beneficiaries_count": 400, "year": 2027},
        {"type": "project", "name": "مشروع السلال", "budget": 90000, "beneficiaries_count": 300, "year": 2027},
        {"type": "program", "name": "برنامج التوعية", "budget": 120000, "year": 2027},
        {"type": "project", "name": "مشروع التأهيل", "budget": 80000, "year": 2027},
        {"type": "project", "name": "مشروع الكفالة", "budget": 150000, "year": 2027},
    ]
    with patch("handler.run_chunk_stream", side_effect=_make_mock_chunk_stream(mixed_entities)):
        events = list(handler.stream_handler({"input": {"run_id": "r-mixed", "text": doc, "stream": True}}))

    item_events = [e for e in events if e["event"] == "item_completed"]
    assert len(item_events) == 5
    assert [e["item_type"] for e in item_events] == ["program", "project", "program", "project", "project"]
    for ev in events:
        assert ev["schema_version"] == config.STREAM_SCHEMA_VERSION
        assert "error" not in ev
        json.dumps(ev, allow_nan=False, ensure_ascii=False)

    final = events[-1]
    assert final["event"] == "completed"
    assert final["programs_count"] == 2
    assert final["projects_count"] == 3
    assert final["total_items"] == 5


# ----------------------------------------------------------------------
# 6 & 7. Arabic Fixtures & Mixed Arabic/English Content
# ----------------------------------------------------------------------
def test_stream_arabic_fixtures_parity_and_quality():
    with open(ATHAR_MD_PATH, encoding="utf-8") as f:
        athar_md = f.read()

    events_athar = list(handler.stream_handler({"input": {"run_id": "ar-1", "text": athar_md, "stream": True}}))
    athar_items = [e["data"] for e in events_athar if e["event"] == "item_completed"]
    assert len(athar_items) == 33
    assert events_athar[-1]["programs_count"] == 9
    assert events_athar[-1]["projects_count"] == 24
    assert events_athar[-1]["total_items"] == 33

    with open(ZAMZAM_MD_PATH, encoding="utf-8") as f:
        zamzam_md = f.read()

    events_zamzam = list(handler.stream_handler({"input": {"run_id": "ar-2", "text": zamzam_md, "stream": True}}))
    zamzam_items = [e["data"] for e in events_zamzam if e["event"] == "item_completed"]
    assert events_zamzam[-1]["programs_count"] == 4
    assert events_zamzam[-1]["projects_count"] == 29
    for item in zamzam_items:
        _assert_valid_10_fields(item)
        if item["description"] and item["delivery_method"]:
            assert item["description"] != item["delivery_method"]


def test_stream_mixed_arabic_and_english_input():
    doc = (
        "Annual Plan 2027 - الجمعية الخيرية:\n"
        "Clean Water Initiative with budget 45000 and 1200 beneficiaries.\n"
        "Project: Solar Rural Clinics cost 60000.\n"
        "برنامج تمكين الشباب بتكلفة 80000 ريال.\n"
    )
    entities = [
        {
            "type": "program",
            "name": "Clean Water Initiative",
            "year": 2027,
            "beneficiaries_count": 1200,
            "budget": 45000,
            "delivery_method": "Community-led well construction with quarterly maintenance visits",
        },
        {
            "type": "program",  # Deliberately misclassified; `Project:` prefix must normalize to `project`
            "name": "Project: Solar Rural Clinics",
            "year": 2027,
            "budget": 60000,
        },
        {
            "type": "program",
            "name": "برنامج تمكين الشباب",
            "year": 2027,
            "budget": 80000,
        },
    ]
    with patch("handler.run_chunk_stream", side_effect=_make_mock_chunk_stream(entities)):
        events = list(handler.stream_handler({"input": {"run_id": "mix-en-ar", "text": doc, "stream": True}}))

    items = {e["data"]["name"]: e["data"] for e in events if e["event"] == "item_completed"}
    assert items["Clean Water Initiative"]["type"] == "program"
    assert items["Solar Rural Clinics"]["type"] == "project"
    assert items["برنامج تمكين الشباب"]["type"] == "program"
    assert events[-1]["programs_count"] == 2
    assert events[-1]["projects_count"] == 1


# ----------------------------------------------------------------------
# 8. Missing Optional Fields Must Remain Null (Never Fabricated)
# ----------------------------------------------------------------------
def test_stream_missing_optional_fields_remain_null():
    doc = "تم إطلاق مشروع العيادة المتنقلة ضمن أنشطة الجمعية."
    entities = [
        {
            "type": "project",
            "name": "مشروع العيادة المتنقلة",
            "year": None,
            "description": None,
            "beneficiaries_count": None,
            "budget": 999999,  # Not grounded in text -> must be nulled
            "beneficiary_value": "null",
            "target_audience": "غير مذكور",
            "delivery_method": "",
            "notes": "-",
        }
    ]
    with patch("handler.run_chunk_stream", side_effect=_make_mock_chunk_stream(entities)):
        events = list(handler.stream_handler({"input": {"run_id": "nulls-1", "text": doc, "stream": True}}))

    item = next(e for e in events if e["event"] == "item_completed")["data"]
    assert item["name"] == "مشروع العيادة المتنقلة"
    assert item["budget"] is None
    assert item["beneficiaries_count"] is None
    assert item["year"] is None
    assert item["beneficiary_value"] is None
    assert item["target_audience"] is None
    assert item["delivery_method"] is None
    assert item["notes"] is None
    assert events[-1]["needs_attention"] is True


# ----------------------------------------------------------------------
# 9. Empty or Invalid Input Must Fail Explicitly
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad_job",
    [
        {"input": {"run_id": "bad-1", "text": "", "stream": True}},
        {"input": {"run_id": "bad-2", "text": "   ", "files": [], "stream": True}},
        {
            "input": {
                "run_id": "bad-3",
                "stream": True,
                "files": [{"filename": "broken.pdf", "bytes": b"NOT_A_PDF"}],
            }
        },
        {"input": "{invalid json"},
    ],
)
def test_stream_empty_or_invalid_input_fails_explicitly(bad_job):
    gen = handler.stream_handler(bad_job)
    emitted: list[dict[str, Any]] = []
    with pytest.raises(streaming.StreamingExtractionError):
        emitted.extend(gen)

    assert len(emitted) >= 1
    assert emitted[-1]["event"] == "failed"
    assert "error_message" in emitted[-1]
    assert "error" not in emitted[-1]


# ----------------------------------------------------------------------
# 10. Duplicate Item Handling Across Chunks
# ----------------------------------------------------------------------
def test_stream_deduplicates_across_overlapping_chunks():
    page1 = "===== PAGE 1 =====\n" + ("تقرير البرامج الميدانية " * 10) + "مشروع السقيا 50000 ريال"
    page2 = "===== PAGE 2 =====\n" + ("متابعة التقرير السنوي " * 10) + "مشروع السقيا 50000 ريال ومشروع الكسوة 30000 ريال"

    chunk_outputs = [
        [{"type": "project", "name": "مشروع السقيا", "budget": 50000}],
        [
            {"type": "project", "name": "مشروع السقيا", "budget": 50000},
            {"type": "project", "name": "مشروع الكسوة", "budget": 30000},
        ],
    ]
    call_idx = 0

    def multi_chunk_mock(user_text: str, cancel_event: threading.Event | None = None):
        nonlocal call_idx
        _ = user_text, cancel_event
        idx = min(call_idx, len(chunk_outputs) - 1)
        call_idx += 1
        return _make_mock_chunk_stream(chunk_outputs[idx])(user_text, cancel_event)

    with (
        patch.object(config, "CHUNK_TARGET_CHARS", 250),
        patch.object(config, "CHUNK_OVERLAP_PAGES", 0),
        patch("handler.run_chunk_stream", side_effect=multi_chunk_mock),
    ):
        events = list(
            handler.stream_handler({"input": {"run_id": "dup-1", "text": f"{page1}\n\n{page2}", "stream": True}})
        )

    items = [e for e in events if e["event"] == "item_completed"]
    assert [e["data"]["name"] for e in items] == ["مشروع السقيا", "مشروع الكسوة"]
    assert [e["sequence"] for e in items] == [1, 2]
    assert events[-1]["total_items"] == 2


# ----------------------------------------------------------------------
# 11. Failed Individual Extraction (Partial Results + Partial Failure Flag)
# ----------------------------------------------------------------------
def test_stream_failed_individual_item_continues_remaining_items():
    doc = "برنامج الرعاية الصحية بتكلفة 70000 ريال ومشروع الدعم الغذائي بتكلفة 40000 ريال."
    entities = [
        {"type": "program", "name": "برنامج الرعاية الصحية", "budget": 70000},
        {"type": "project", "name": ""},  # Malformed candidate -> emits item_failed
        {"type": "project", "name": "مشروع الدعم الغذائي", "budget": 40000},
    ]
    with patch("handler.run_chunk_stream", side_effect=_make_mock_chunk_stream(entities)):
        events = list(handler.stream_handler({"input": {"run_id": "part-1", "text": doc, "stream": True}}))

    completed = [e for e in events if e["event"] == "item_completed"]
    item_failures = [e for e in events if e["event"] == "item_failed"]

    assert len(completed) == 2
    assert len(item_failures) == 1
    assert item_failures[0]["reason_code"] == "validation_failed"
    assert "error" not in item_failures[0]
    assert "error_message" in item_failures[0]

    final = events[-1]
    assert final["event"] == "completed"
    assert final["partial_failure"] is True
    assert final["failed_items_count"] == 1
    assert final["needs_attention"] is True
    assert final["total_items"] == 2


# ----------------------------------------------------------------------
# 12. Terminal Failure After >= 1 Successful Item Events
# ----------------------------------------------------------------------
def test_stream_failure_after_successful_item_preserves_emitted_and_fails():
    p1 = "===== PAGE 1 =====\n" + ("مشروع الإغاثة العاجلة بتكلفة 100000 ريال. " * 10)
    p2 = "===== PAGE 2 =====\n" + ("تفاصيل إضافية لمشروع آخر. " * 10)

    call_idx = 0

    def crashing_second_chunk(user_text: str, cancel_event: threading.Event | None = None):
        nonlocal call_idx
        _ = user_text, cancel_event
        call_idx += 1
        if call_idx == 1:
            yield ("item", {"type": "project", "name": "مشروع الإغاثة العاجلة", "budget": 100000})
            yield (
                "chunk_meta",
                "{}",
                {
                    "input_tokens": 100,
                    "generated_tokens": 40,
                    "stop_reason": "eos",
                    "retried": False,
                    "parser_state": "DONE",
                    "streamed_count": 1,
                    "parser_errors": 0,
                },
            )
            return
        raise RuntimeError("Simulated CUDA OOM on chunk 2")

    with (
        patch.object(config, "CHUNK_TARGET_CHARS", 250),
        patch.object(config, "CHUNK_OVERLAP_PAGES", 0),
        patch("handler.run_chunk_stream", side_effect=crashing_second_chunk),
    ):
        gen = handler.stream_handler({"input": {"run_id": "crash-2", "text": f"{p1}\n\n{p2}", "stream": True}})
        emitted: list[dict[str, Any]] = []
        with pytest.raises(streaming.StreamingExtractionError, match="CUDA OOM"):
            emitted.extend(gen)

    completed_before_crash = [e for e in emitted if e["event"] == "item_completed"]
    assert len(completed_before_crash) == 1
    assert completed_before_crash[0]["data"]["name"] == "مشروع الإغاثة العاجلة"

    terminal = emitted[-1]
    assert terminal["event"] == "failed"
    assert terminal["partial_results_emitted"] is True
    assert terminal["completed_items"] == 1
    assert terminal["projects_count"] == 1
    assert terminal["error_type"] == "RuntimeError"
    assert "error" not in terminal


# ----------------------------------------------------------------------
# 13. Retries & Idempotent Deterministic Event IDs
# ----------------------------------------------------------------------
def test_stream_retries_produce_idempotent_event_ids():
    doc = "برنامج التأهيل بتكلفة 50000 ريال ومشروع الحقيبة المدرسية بتكلفة 15000 ريال لعام 2027."
    entities = [
        {"type": "program", "name": "برنامج التأهيل", "budget": 50000, "year": 2027},
        {"type": "project", "name": "مشروع الحقيبة المدرسية", "budget": 15000, "year": 2027},
    ]

    with patch("handler.run_chunk_stream", side_effect=_make_mock_chunk_stream(entities)):
        run_a = list(handler.stream_handler({"input": {"run_id": "retry-42", "text": doc, "stream": True}}))
    with patch("handler.run_chunk_stream", side_effect=_make_mock_chunk_stream(entities)):
        run_b = list(handler.stream_handler({"input": {"run_id": "retry-42", "text": doc, "stream": True}}))
    with patch("handler.run_chunk_stream", side_effect=_make_mock_chunk_stream(entities)):
        run_c = list(handler.stream_handler({"input": {"run_id": "retry-99", "text": doc, "stream": True}}))

    ids_a = [e["event_id"] for e in run_a if e["event"] == "item_completed"]
    ids_b = [e["event_id"] for e in run_b if e["event"] == "item_completed"]
    ids_c = [e["event_id"] for e in run_c if e["event"] == "item_completed"]

    assert len(ids_a) == 2 and all(eid.startswith("evt_") for eid in ids_a)
    assert len(set(ids_a)) == 2
    assert ids_a == ids_b
    assert ids_a != ids_c


# ----------------------------------------------------------------------
# 14–16. Adaptive Recovery, Legacy Mode & Non-Regression on Health Routes
# ----------------------------------------------------------------------
def test_stream_adaptive_split_recovery_for_truncated_chunk():
    p1 = "===== PAGE 1 =====\n" + ("تقرير سردي للمبادرة الأولى بتكلفة 110000 ريال. " * 6)
    p2 = "===== PAGE 2 =====\n" + ("تقرير سردي للبرنامج الثاني بتكلفة 220000 ريال. " * 6)

    call_count = 0

    def adaptive_mock(user_text: str, cancel_event: threading.Event | None = None):
        nonlocal call_count
        _ = cancel_event
        call_count += 1
        if call_count == 1:
            yield (
                "chunk_meta",
                '{"programs_and_projects": [{"type": "project", "name": "مبادرة',
                {
                    "input_tokens": 400,
                    "generated_tokens": 6144,
                    "stop_reason": "max_new_tokens",
                    "retried": False,
                    "parser_state": "IN_ELEMENT",
                    "streamed_count": 0,
                    "parser_errors": 0,
                },
            )
            return
        if "110000" in user_text:
            yield from _make_mock_chunk_stream([{"type": "project", "name": "مبادرة الغذاء", "budget": 110000}])(
                user_text
            )
            return
        yield from _make_mock_chunk_stream([{"type": "program", "name": "برنامج التعليم", "budget": 220000}])(user_text)

    with patch("handler.run_chunk_stream", side_effect=adaptive_mock):
        events = list(handler.stream_handler({"input": {"run_id": "adapt-1", "text": f"{p1}\n\n{p2}", "stream": True}}))

    assert call_count == 3
    names = [e["data"]["name"] for e in events if e["event"] == "item_completed"]
    assert names == ["مبادرة الغذاء", "برنامج التعليم"]
    assert events[-1]["event"] == "completed"
    assert events[-1]["chunks"][0].get("adaptive_split_recovered") is True


def test_legacy_non_streaming_and_health_routes_preserved():
    pasted = "===== PAGE 8 =====\n2027\nبرنامج1.1.A\n08كفاية\n3,723,725\nوصف البرنامج\nالمستفيدين."
    # Direct synchronous handler
    sync_res = handler.handler({"input": {"run_id": "sync-1", "text": pasted}})
    assert isinstance(sync_res, dict)
    assert sync_res["programs"][0]["name"] == "كفاية"

    # Generator handler with stream omitted / False yields 1 legacy dict unchanged
    stream_off = list(handler.stream_handler({"input": {"run_id": "sync-2", "text": pasted, "stream": False}}))
    assert len(stream_off) == 1
    assert stream_off[0]["programs"][0]["name"] == "كفاية"
    assert "event" not in stream_off[0]

    # Health check routes preserve readiness status even if previous _MODEL_STATE['error'] was set
    for act in ("health", "health_check", "ping"):
        hc_sync = handler.handler({"input": {"action": act}})
        assert hc_sync["status"] in ("container_ready", "ready")

        with patch.dict(handler._MODEL_STATE, {"initialized": False, "error": "previous error"}, clear=False):
            hc_gen = list(handler.stream_handler({"input": {"action": act}}))
            assert len(hc_gen) == 1
            assert hc_gen[0].get("error") is None
            assert hc_gen[0]["model_error"] == "previous error"


# ----------------------------------------------------------------------
# 17. PROOFS OF REAL INCREMENTAL EMISSION (First Item Before Last Finishes)
# ----------------------------------------------------------------------
def test_proof_first_item_emitted_before_second_chunk_starts():
    p1 = "===== PAGE 1 =====\n" + ("مشروع أول بميزانية 10000 ريال. " * 5)
    p2 = "===== PAGE 2 =====\n" + ("مشروع ثاني بميزانية 20000 ريال. " * 5)

    chunk_calls: list[int] = []

    def tracking_chunk_stream(user_text: str, cancel_event: threading.Event | None = None):
        _ = user_text, cancel_event
        call_no = len(chunk_calls) + 1
        chunk_calls.append(call_no)
        name = "مشروع أول" if call_no == 1 else "مشروع ثاني"
        budget = 10000 if call_no == 1 else 20000
        yield from _make_mock_chunk_stream([{"type": "project", "name": name, "budget": budget}])(user_text)

    with (
        patch.object(config, "CHUNK_TARGET_CHARS", 250),
        patch.object(config, "CHUNK_OVERLAP_PAGES", 0),
        patch("handler.run_chunk_stream", side_effect=tracking_chunk_stream),
    ):
        gen = handler.stream_handler({"input": {"run_id": "inc-1", "text": f"{p1}\n\n{p2}", "stream": True}})

        first_item_event = None
        for ev in gen:
            if ev["event"] == "item_completed":
                first_item_event = ev
                break

        # Critical assertion: Chunk 2 MUST NOT have started when Item 1 is already emitted!
        assert first_item_event is not None
        assert first_item_event["data"]["name"] == "مشروع أول"
        assert chunk_calls == [1]

        rest = list(gen)
        assert chunk_calls == [1, 2]
        assert any(e["event"] == "item_completed" and e["data"]["name"] == "مشروع ثاني" for e in rest)
        assert rest[-1]["event"] == "completed"


def test_proof_token_level_streaming_emits_item1_before_item2_tokens_generated():
    doc = "برنامج أول 10000 ريال ومشروع ثاني 20000 ريال ومشروع ثالث 30000 ريال."
    token_batches_sent: list[int] = []

    def token_delta_generator(user_text: str, rp: float, cancel_event: threading.Event | None = None):
        _ = user_text, rp, cancel_event
        token_batches_sent.append(1)
        yield '{"association_name": null, "programs_and_projects": [', None
        token_batches_sent.append(2)
        yield '{"type": "program", "name": "برنامج أول", "budget": 10000}', None
        # At this exact instant, parser MUST emit item 1 BEFORE batch 3 or 4 runs!
        token_batches_sent.append(3)
        yield ', {"type": "project", "name": "مشروع ثاني", "budget": 20000}', None
        token_batches_sent.append(4)
        yield ', {"type": "project", "name": "مشروع ثالث", "budget": 30000}]}', None
        yield None, {"input_tokens": 150, "generated_tokens": 90, "stop_reason": "eos"}

    with patch("handler.generate_stream", side_effect=token_delta_generator):
        gen = handler.stream_handler({"input": {"run_id": "tok-1", "text": doc, "stream": True}})
        first_item = next(e for e in gen if e["event"] == "item_completed")
        assert first_item["data"]["name"] == "برنامج أول"
        assert 3 not in token_batches_sent
        assert 4 not in token_batches_sent

        remaining = list(gen)
        assert token_batches_sent == [1, 2, 3, 4]
        assert remaining[-1]["event"] == "completed"
        assert remaining[-1]["total_items"] == 3


def test_proof_structured_cards_and_markdown_stream_lazily_and_cancel_cleanly():
    with open(ATHAR_MD_PATH, encoding="utf-8") as f:
        athar_md = f.read()

    synth_count = 0
    orig_synth = pp._synthesize_card_fields

    def tracked_synth(card, doc_meta):
        nonlocal synth_count
        synth_count += 1
        return orig_synth(card, doc_meta)

    with patch("postprocess._synthesize_card_fields", side_effect=tracked_synth):
        gen = handler.stream_handler({"input": {"run_id": "lazy-cards", "text": athar_md, "stream": True}})
        first_item = next(e for e in gen if e["event"] == "item_completed")
        assert first_item["data"]["name"] == "كفاية"
        # Card 1 must be emitted as soon as Card 1 completes, long before all 33 cards are synthesized
        assert synth_count == 1
        gen.close()

    with open(ZAMZAM_MD_PATH, encoding="utf-8") as f:
        zamzam_md = f.read()

    md_parse_count = 0
    orig_parse_block = pp._parse_markdown_report_block

    def tracked_parse_block(block, year, umbrella_names):
        nonlocal md_parse_count
        md_parse_count += 1
        return orig_parse_block(block, year, umbrella_names)

    with patch("postprocess._parse_markdown_report_block", side_effect=tracked_parse_block):
        gen_md = handler.stream_handler({"input": {"run_id": "lazy-md", "text": zamzam_md, "stream": True}})
        first_md = next(e for e in gen_md if e["event"] == "item_completed")
        assert first_md["sequence"] == 1
        assert 1 <= md_parse_count <= 12  # Only initial 3-entity peek (covering early header blocks), not all 56 pages
        gen_md.close()


def test_cancellation_sets_cancel_event_on_generator_close():
    doc = "برنامج واحد 10000 ومشروع اثنان 20000"
    observed_cancel_events: list[threading.Event] = []

    def cancellable_chunk_stream(user_text: str, cancel_event: threading.Event | None = None):
        _ = user_text
        assert cancel_event is not None
        observed_cancel_events.append(cancel_event)
        yield ("item", {"type": "program", "name": "برنامج واحد", "budget": 10000})
        yield ("item", {"type": "project", "name": "مشروع اثنان", "budget": 20000})
        yield (
            "chunk_meta",
            "{}",
            {
                "input_tokens": 50,
                "generated_tokens": 20,
                "stop_reason": "eos",
                "retried": False,
                "parser_state": "DONE",
                "streamed_count": 2,
                "parser_errors": 0,
            },
        )

    with patch("handler.run_chunk_stream", side_effect=cancellable_chunk_stream):
        gen = handler.stream_handler({"input": {"run_id": "cancel-1", "text": doc, "stream": True}})
        first = next(e for e in gen if e["event"] == "item_completed")
        assert first["sequence"] == 1
        assert len(observed_cancel_events) == 1
        assert not observed_cancel_events[0].is_set()
        gen.close()
        assert observed_cancel_events[0].is_set()


def _drive_coro(coro: Any) -> Any:
    """Drive a coroutine without creating an asyncio event-loop self-pipe socketpair."""
    try:
        val = None
        while True:
            val = coro.send(val)
    except StopIteration as stop:
        return stop.value


# ----------------------------------------------------------------------
# 18. Real Installed RunPod SDK Simulation (`rp_job.handle_job`)
# ----------------------------------------------------------------------
def test_real_runpod_sdk_generator_and_handle_job_simulation():
    # 1. Verify RunPod SDK statically recognizes `stream_handler` as a generator handler
    assert rp_handler.is_generator(handler.stream_handler) is True
    assert rp_handler.is_generator(handler.handler) is False

    async def _run_handle_job(job: dict[str, Any], return_aggregate_stream: bool = True):
        streamed_chunks: list[dict[str, Any]] = []
        final_sent: dict[str, Any] = {}

        async def fake_stream_result(_session, stream_output, _job):
            streamed_chunks.append(stream_output)

        async def fake_send_result(_session, job_data, _job, is_stream=False):
            final_sent["data"] = job_data
            final_sent["is_stream"] = is_stream

        cfg = {"handler": handler.stream_handler, "return_aggregate_stream": return_aggregate_stream}
        with (
            patch.object(rp_job, "stream_result", side_effect=fake_stream_result),
            patch.object(rp_job, "send_result", side_effect=fake_send_result),
        ):
            job_result = await rp_job.handle_job(None, cfg, job)
        return streamed_chunks, final_sent, job_result

    doc = "برنامج الصحة بتكلفة 50000 ريال ومشروع الغذاء بتكلفة 25000 ريال."
    entities = [
        {"type": "program", "name": "برنامج الصحة", "budget": 50000},
        {"type": "project", "name": ""},  # Non-fatal item_failed must NOT abort RunPod handle_job!
        {"type": "project", "name": "مشروع الغذاء", "budget": 25000},
    ]

    # Case A: Streaming with non-fatal `item_failed` succeeds and streams all chunks
    with patch("handler.run_chunk_stream", side_effect=_make_mock_chunk_stream(entities)):
        streamed, final_sent, job_result = _drive_coro(
            _run_handle_job({"id": "rp-job-1", "input": {"run_id": "rp-1", "text": doc, "stream": True}})
        )

    assert "error" not in job_result
    assert final_sent["is_stream"] is True
    app_events = [c["output"] for c in streamed]
    assert [e["event"] for e in app_events if e["event"] in ("item_completed", "item_failed", "completed")] == [
        "item_completed",
        "item_failed",
        "item_completed",
        "completed",
    ]
    assert len(job_result["output"]) == len(app_events)

    # Case B: Terminal failure after >= 1 successful event streams earlier items + `failed`, AND fails RunPod job
    p1 = "===== PAGE 1 =====\n" + ("برنامج الصحة بتكلفة 50000 ريال. " * 10)
    p2 = "===== PAGE 2 =====\n" + ("مشروع فاشل. " * 10)
    call_n = 0

    def fail_on_2(user_text: str, cancel_event: threading.Event | None = None):
        nonlocal call_n
        _ = cancel_event
        call_n += 1
        if call_n == 1:
            yield from _make_mock_chunk_stream([{"type": "program", "name": "برنامج الصحة", "budget": 50000}])(
                user_text
            )
            return
        raise RuntimeError("GPU worker crash")

    with (
        patch.object(config, "CHUNK_TARGET_CHARS", 250),
        patch.object(config, "CHUNK_OVERLAP_PAGES", 0),
        patch("handler.run_chunk_stream", side_effect=fail_on_2),
    ):
        streamed_f, final_sent_f, job_result_f = _drive_coro(
            _run_handle_job({"id": "rp-job-2", "input": {"run_id": "rp-2", "text": f"{p1}\n\n{p2}", "stream": True}})
        )

    assert "error" in job_result_f
    assert "GPU worker crash" in job_result_f["error"]
    assert final_sent_f["data"] is job_result_f
    streamed_f_events = [c["output"] for c in streamed_f]
    assert any(e["event"] == "item_completed" for e in streamed_f_events)
    assert streamed_f_events[-1]["event"] == "failed"
    assert streamed_f_events[-1]["partial_results_emitted"] is True

    # Case C: Verify exact `return_aggregate_stream=True` behavior on legacy `stream=False` & `health_check`
    pasted = "===== PAGE 8 =====\n2027\nبرنامج1.1.A\n08كفاية\n3,723,725\nوصف البرنامج\nالمستفيدين."
    _, _, legacy_result = _drive_coro(
        _run_handle_job(
            {"id": "rp-job-3", "input": {"run_id": "rp-3", "text": pasted}},
            return_aggregate_stream=True,
        )
    )
    # Proven SDK limitation: return_aggregate_stream=True wraps even 1-yield responses inside a list
    assert isinstance(legacy_result["output"], list) and len(legacy_result["output"]) == 1
    assert legacy_result["output"][0]["programs"][0]["name"] == "كفاية"

    _, _, hc_result = _drive_coro(
        _run_handle_job(
            {"id": "rp-job-4", "input": {"action": "health_check"}},
            return_aggregate_stream=True,
        )
    )
    assert isinstance(hc_result["output"], list) and len(hc_result["output"]) == 1
    assert hc_result["output"][0]["status"] in ("container_ready", "ready")

    # Sanity check: runpod package version present
    assert hasattr(runpod, "__version__")
