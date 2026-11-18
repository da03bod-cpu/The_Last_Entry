"""
benchmark_report.py - Automated Golden Reference Evaluation & Comparison Generator.

Evaluates the extraction pipeline across all supplied Athar 2027 reference documents
(MD, DOCX, XLSX, and original PDF when available) against `athar_2027_programs_projects_contract.json`,
computing entity recall, precision, type accuracy, budget accuracy, beneficiary count accuracy,
and per-entity / per-field mismatch diagnostics.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import handler
import postprocess as pp

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "tests", "fixtures")
GOLD_JSON_PATH = os.path.join(FIXTURES_DIR, "athar_2027_programs_projects_contract.json")
SCRATCH_PDF_PATH = r"C:\Users\omari\.gemini\antigravity\brain\b49d2610-47b1-47df-845a-cf5b44f22c3b\scratch\athar_2027.pdf"


def evaluate_against_golden(extracted: dict[str, Any], gold: dict[str, Any]) -> dict[str, Any]:
    gold_items = gold["programs"] + gold["projects"]
    ext_items = extracted.get("programs", []) + extracted.get("projects", [])

    gold_by_key = {(item["type"], item["name"]): item for item in gold_items}
    ext_by_key = {(item["type"], item["name"]): item for item in ext_items}

    matched_keys = [k for k in gold_by_key if k in ext_by_key]
    missing_keys = [k for k in gold_by_key if k not in ext_by_key]
    false_positive_keys = [k for k in ext_by_key if k not in gold_by_key]

    field_matches = {f: 0 for f in pp.FIELDS}
    mismatches: list[dict[str, Any]] = []

    for k in matched_keys:
        g_ent = gold_by_key[k]
        e_ent = ext_by_key[k]
        ent_diffs = {}
        for f in pp.FIELDS:
            if e_ent.get(f) == g_ent.get(f):
                field_matches[f] += 1
            else:
                ent_diffs[f] = {"expected": g_ent.get(f), "actual": e_ent.get(f)}
        if ent_diffs:
            mismatches.append({"type": k[0], "name": k[1], "diffs": ent_diffs})

    total_gold = len(gold_items)
    total_ext = len(ext_items)
    recall = len(matched_keys) / total_gold if total_gold else 1.0
    precision = len(matched_keys) / total_ext if total_ext else 0.0

    return {
        "expected_programs": len(gold["programs"]),
        "extracted_programs": len(extracted.get("programs", [])),
        "expected_projects": len(gold["projects"]),
        "extracted_projects": len(extracted.get("projects", [])),
        "expected_total": total_gold,
        "extracted_total": total_ext,
        "matched_entities": len(matched_keys),
        "recall": round(recall * 100, 2),
        "precision": round(precision * 100, 2),
        "missing_entities": [{"type": t, "name": n} for t, n in missing_keys],
        "false_positives": [{"type": t, "name": n} for t, n in false_positive_keys],
        "field_accuracy_percent": {
            f: round((field_matches[f] / len(matched_keys)) * 100, 2) if matched_keys else 0.0 for f in pp.FIELDS
        },
        "mismatches": mismatches,
    }


def run_all_benchmarks() -> dict[str, Any]:
    with open(GOLD_JSON_PATH, encoding="utf-8") as f:
        gold = json.load(f)

    targets = [
        ("Markdown (.md)", os.path.join(FIXTURES_DIR, "الخطة_التشغيلية_والموازنة_التقديرية_2027.md")),
        ("Word (.docx)", os.path.join(FIXTURES_DIR, "الخطة_التشغيلية_والموازنة_التقديرية_2027.docx")),
        ("Excel (.xlsx)", os.path.join(FIXTURES_DIR, "الخطة_التشغيلية_والموازنة_التقديرية_2027.xlsx")),
    ]
    if os.path.isfile(SCRATCH_PDF_PATH):
        targets.append(("Original PDF (.pdf)", SCRATCH_PDF_PATH))

    results = {}
    for label, path in targets:
        t0 = time.time()
        resp = handler.handler(
            {
                "input": {
                    "run_id": f"eval-{label}",
                    "type": "program_autofill",
                    "input": json.dumps({"files": [os.path.basename(path)]}, ensure_ascii=False),
                    "files": [{"filename": os.path.basename(path), "path": path}],
                }
            }
        )
        elapsed = round(time.time() - t0, 3)
        report = evaluate_against_golden(resp, gold)
        report["elapsed_seconds"] = elapsed
        results[label] = report

    return results


if __name__ == "__main__":
    summary = run_all_benchmarks()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
