"""
streaming.py - Incremental JSON parsing, deterministic event building, and
per-run entity validation/deduplication for RunPod Serverless streaming.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from typing import Any

import config
import postprocess as pp


class StreamingExtractionError(RuntimeError):
    """Raised on fatal streaming extraction errors so RunPod marks the job FAILED."""

    def __init__(
        self,
        message: str,
        *,
        error_type: str = "ExtractionError",
        run_id: str | None = None,
        completed_items: int = 0,
        partial_results_emitted: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.run_id = run_id
        self.completed_items = completed_items
        self.partial_results_emitted = partial_results_emitted


def _parse_nested_input(job_input: dict[str, Any]) -> dict[str, Any]:
    raw_nested = job_input.get("input")
    if isinstance(raw_nested, dict):
        return raw_nested
    if isinstance(raw_nested, str) and raw_nested.strip():
        try:
            parsed = json.loads(raw_nested)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            pass
    return {}


def _to_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val != 0
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on")
    return False


def is_streaming_requested(job_input: dict[str, Any]) -> bool:
    """Return True when the caller opted into streaming via `stream: true`."""
    if not isinstance(job_input, dict):
        return False
    if "stream" in job_input and job_input["stream"] is not None:
        return _to_bool(job_input["stream"])
    nested = _parse_nested_input(job_input)
    if "stream" in nested and nested["stream"] is not None:
        return _to_bool(nested["stream"])
    return False


def extract_run_id(job_input: dict[str, Any]) -> str | None:
    """Extract `run_id` from top-level `job_input` or nested `input` JSON."""
    if not isinstance(job_input, dict):
        return None
    rid = job_input.get("run_id")
    if rid in (None, ""):
        rid = _parse_nested_input(job_input).get("run_id")
    return str(rid) if rid not in (None, "") else None


def make_item_event_id(run_id: str | None, item: dict[str, Any], disambiguator: int = 0) -> str:
    """Generate a deterministic, idempotent event identifier for an extracted item."""
    canonical_parts = [
        str(run_id or ""),
        str(item.get("type") or ""),
        pp.norm(item.get("name")),
        str(item.get("year") if item.get("year") is not None else ""),
        str(item.get("budget") if item.get("budget") is not None else ""),
        str(item.get("beneficiaries_count") if item.get("beneficiaries_count") is not None else ""),
    ]
    if disambiguator > 0:
        canonical_parts.append(str(disambiguator))
    digest = hashlib.sha256("|".join(canonical_parts).encode("utf-8")).hexdigest()[:16]
    return f"evt_{digest}"


def make_failure_event_id(
    run_id: str | None,
    chunk_index: int | None,
    item_index: int | None,
    item_name: str | None,
    reason_code: str,
) -> str:
    """Generate a deterministic identifier for an item or chunk failure event."""
    canonical = "|".join(
        [
            str(run_id or ""),
            reason_code,
            str(chunk_index if chunk_index is not None else ""),
            str(item_index if item_index is not None else ""),
            pp.norm(item_name),
        ]
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"err_{digest}"


def _truncate_msg(msg: Any, max_len: int = 500) -> str:
    text = str(msg or "").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def make_progress_event(
    run_id: str | None,
    stage: str,
    completed_items: int,
    programs_count: int,
    projects_count: int,
    *,
    message: str | None = None,
    chunk_index: int | None = None,
    total_chunks: int | None = None,
) -> dict[str, Any]:
    """Build a versioned `progress` event without fabricating totals or percentages."""
    payload: dict[str, Any] = {
        "schema_version": config.STREAM_SCHEMA_VERSION,
        "event": "progress",
        "run_id": str(run_id) if run_id is not None else None,
        "stage": stage,
        "completed_items": int(completed_items),
        "programs_count": int(programs_count),
        "projects_count": int(projects_count),
    }
    if chunk_index is not None:
        payload["chunk_index"] = int(chunk_index)
    if total_chunks is not None:
        payload["total_chunks"] = int(total_chunks)
    if message:
        payload["message"] = _truncate_msg(message, 250)
    return payload


def make_item_completed_event(
    run_id: str | None,
    event_id: str,
    sequence: int,
    item: dict[str, Any],
) -> dict[str, Any]:
    """Build a versioned `item_completed` event enforcing the exact 10-field schema."""
    data = {k: item.get(k) for k in pp.FIELDS}
    return {
        "schema_version": config.STREAM_SCHEMA_VERSION,
        "event": "item_completed",
        "run_id": str(run_id) if run_id is not None else None,
        "event_id": event_id,
        "sequence": int(sequence),
        "item_type": data["type"],
        "data": data,
    }


def make_item_failed_event(
    run_id: str | None,
    event_id: str,
    error_message: str,
    *,
    item_name: str | None = None,
    chunk_index: int | None = None,
    reason_code: str = "extraction_failed",
) -> dict[str, Any]:
    """Build a non-fatal `item_failed` event.

    Uses `error_message` instead of top-level `error` because RunPod's `rp_job.handle_job`
    aborts the entire generator stream if any yielded dict contains a truthy `error` key.
    """
    payload: dict[str, Any] = {
        "schema_version": config.STREAM_SCHEMA_VERSION,
        "event": "item_failed",
        "run_id": str(run_id) if run_id is not None else None,
        "event_id": event_id,
        "reason_code": reason_code,
        "error_message": _truncate_msg(error_message),
        "item_name": _truncate_msg(item_name, 255) if item_name else None,
    }
    if chunk_index is not None:
        payload["chunk_index"] = int(chunk_index)
    return payload


def make_completed_event(
    run_id: str | None,
    *,
    total_items: int,
    programs_count: int,
    projects_count: int,
    failed_items_count: int,
    partial_failure: bool,
    needs_attention: bool,
    association_name: str | None,
    year: int | None,
    period: str | None,
    review: list[dict[str, str]],
    chunks: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    json_repaired: bool,
) -> dict[str, Any]:
    """Build the final `completed` event after all units have finished."""
    return {
        "schema_version": config.STREAM_SCHEMA_VERSION,
        "event": "completed",
        "run_id": str(run_id) if run_id is not None else None,
        "total_items": int(total_items),
        "programs_count": int(programs_count),
        "projects_count": int(projects_count),
        "failed_items_count": int(failed_items_count),
        "partial_failure": bool(partial_failure),
        "needs_attention": bool(needs_attention),
        "association_name": association_name,
        "year": year,
        "period": period,
        "review": review,
        "chunks": chunks,
        "sources": sources,
        "json_repaired": bool(json_repaired),
    }


def make_failed_event(
    run_id: str | None,
    *,
    error_message: str,
    error_type: str,
    completed_items: int = 0,
    programs_count: int = 0,
    projects_count: int = 0,
    partial_results_emitted: bool = False,
) -> dict[str, Any]:
    """Build a terminal `failed` event emitted immediately before raising to RunPod."""
    return {
        "schema_version": config.STREAM_SCHEMA_VERSION,
        "event": "failed",
        "run_id": str(run_id) if run_id is not None else None,
        "error_message": _truncate_msg(error_message),
        "error_type": error_type,
        "completed_items": int(completed_items),
        "programs_count": int(programs_count),
        "projects_count": int(projects_count),
        "partial_results_emitted": bool(partial_results_emitted),
    }


class IncrementalProgramArrayParser:
    """Incremental parser extracting complete objects inside `"programs_and_projects": [...]`
    from streamed LLM text deltas as soon as each object's closing brace arrives.
    """

    def __init__(self) -> None:
        self.full_text_parts: list[str] = []
        self.state: str = "BEFORE_KEY"
        self.emitted_count: int = 0
        self.parse_errors: int = 0

        self._in_string: bool = False
        self._escaped: bool = False
        self._capturing_key: bool = False
        self._key_buf: list[str] = []
        self._outer_depth: int = 0
        self._elem_depth: int = 0
        self._elem_buf: list[str] = []

    @property
    def full_text(self) -> str:
        return "".join(self.full_text_parts)

    def _parse_object_fragment(self, raw_elem: str) -> dict[str, Any] | None:
        try:
            obj = json.loads(raw_elem)
            if isinstance(obj, dict):
                return obj
        except (ValueError, TypeError):
            pass
        with contextlib.suppress(Exception):
            from json_repair import repair_json

            repaired = repair_json(raw_elem, return_objects=True)
            if isinstance(repaired, dict):
                return repaired
        return None

    def feed(self, delta: str) -> list[dict[str, Any]]:
        """Consume a text delta and return any newly completed element dicts."""
        if not delta:
            return []
        self.full_text_parts.append(delta)
        completed: list[dict[str, Any]] = []

        for ch in delta:
            if self.state == "IN_ELEMENT":
                self._elem_buf.append(ch)

            if self._in_string:
                if self._escaped:
                    self._escaped = False
                elif ch == "\\":
                    self._escaped = True
                elif ch == '"':
                    self._in_string = False
                    if self._capturing_key:
                        key_name = "".join(self._key_buf)
                        self._capturing_key = False
                        self._key_buf = []
                        if self.state == "BEFORE_KEY" and key_name == "programs_and_projects":
                            self.state = "AFTER_KEY_EXPECT_COLON"
                elif self._capturing_key:
                    self._key_buf.append(ch)
                continue

            if ch == '"':
                self._in_string = True
                if self.state == "BEFORE_KEY" and self._outer_depth == 1:
                    self._capturing_key = True
                    self._key_buf = []
                else:
                    self._capturing_key = False
                continue

            if self.state == "BEFORE_KEY":
                if ch == "{":
                    self._outer_depth += 1
                elif ch == "}" and self._outer_depth > 0:
                    self._outer_depth -= 1
            elif self.state == "AFTER_KEY_EXPECT_COLON":
                if ch.isspace():
                    continue
                if ch == ":":
                    self.state = "AFTER_COLON_EXPECT_ARRAY"
                else:
                    self.state = "BEFORE_KEY"
                    if ch == "{":
                        self._outer_depth += 1
            elif self.state == "AFTER_COLON_EXPECT_ARRAY":
                if ch.isspace():
                    continue
                if ch == "[":
                    self.state = "IN_ARRAY"
                else:
                    self.state = "BEFORE_KEY"
                    if ch == "{":
                        self._outer_depth += 1
            elif self.state == "IN_ARRAY":
                if ch == "{":
                    self.state = "IN_ELEMENT"
                    self._elem_depth = 1
                    self._elem_buf = ["{"]
                elif ch == "]":
                    self.state = "DONE"
            elif self.state == "IN_ELEMENT":
                if ch == "{":
                    self._elem_depth += 1
                elif ch == "}":
                    self._elem_depth -= 1
                    if self._elem_depth == 0:
                        raw_elem = "".join(self._elem_buf)
                        self._elem_buf = []
                        self.state = "IN_ARRAY"
                        parsed = self._parse_object_fragment(raw_elem)
                        if parsed is not None:
                            completed.append(parsed)
                            self.emitted_count += 1
                        else:
                            self.parse_errors += 1

        return completed


class IncrementalEntityTracker:
    """Per-run entity validator, deduplicator, and streaming event emitter."""

    def __init__(
        self,
        *,
        run_id: str | None,
        pages: list[tuple[int, str]],
        default_year: int | None,
        review: list[dict[str, str]],
        reference_entities: list[dict[str, Any]] | None = None,
    ) -> None:
        self.run_id = run_id
        self.pages = pages
        self.default_year = default_year
        self.review = review
        self.reference_entities = reference_entities or []
        self.used_ref_indices: set[int] = set()

        self.full_doc_text = "\n\n".join(body for _, body in pages)
        self.full_doc_norm = pp.norm(self.full_doc_text)
        self.verbatim_headings = pp._collect_verbatim_document_headings(pages)

        self.items: list[dict[str, Any]] = []
        self.used_event_ids: set[str] = set()
        self.sequence: int = 0
        self.programs_count: int = 0
        self.projects_count: int = 0
        self.failed_items_count: int = 0

    @property
    def total_items(self) -> int:
        return self.programs_count + self.projects_count

    def _allocate_event_id(self, entity: dict[str, Any]) -> str:
        disamb = 0
        while True:
            cand = make_item_event_id(self.run_id, entity, disambiguator=disamb)
            if cand not in self.used_event_ids:
                self.used_event_ids.add(cand)
                return cand
            disamb += 1

    def process_candidate(
        self,
        raw_item: Any,
        *,
        already_validated: bool = False,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Validate, ground, deduplicate, and package one entity into `item_completed`.

        Returns `(event_dict, None)` if accepted, or `(None, status_reason)` otherwise.
        `status_reason` is one of `"duplicate"`, `"non_entity"`, or `"invalid:..."`.
        """
        if not isinstance(raw_item, dict):
            return None, "invalid:extracted_item_not_object"

        raw_name = str(raw_item.get("name") or "").strip()
        if not raw_name:
            return None, "invalid:missing_entity_name"
        if pp.is_non_entity_heading(raw_name):
            return None, "non_entity"

        if already_validated:
            validated = pp.validate_entity_schema(raw_item, default_year=self.default_year)
        else:
            validated = pp.reconcile_single_entity(
                raw_item,
                verbatim_headings=self.verbatim_headings,
                reference_entities=self.reference_entities,
                full_doc_text=self.full_doc_text,
                full_doc_norm=self.full_doc_norm,
                default_year=self.default_year,
                review=self.review,
                used_ref_indices=self.used_ref_indices,
            )

        if validated is None:
            return None, "non_entity" if pp.is_non_entity_heading(raw_name) else "invalid:schema_validation_failed"

        if pp.find_matching_entity(validated, self.items) is not None:
            return None, "duplicate"

        event_id = self._allocate_event_id(validated)
        self.sequence += 1
        if validated["type"] == "program":
            self.programs_count += 1
        else:
            self.projects_count += 1
        self.items.append(validated)

        return make_item_completed_event(self.run_id, event_id, self.sequence, validated), None
