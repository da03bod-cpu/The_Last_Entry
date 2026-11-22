"""
ingestion.py - Multi-format Arabic NGO document ingestion pipeline.

Supports:
  - PDF (native selectable text via PyMuPDF + automatic Arabic OCR fallback for scanned pages)
  - XLSX / Excel (all worksheets, merged cell resolution, numeric cell preservation, table structure)
  - DOCX / Microsoft Word (paragraphs, headings, tables in document reading order)
  - MD / Markdown (headings, lists, tables, page sections)
  - Plain text
  - Multiple files + pasted text with source traceability

Preserves `===== PAGE N =====` markers expected by the chunker and trained model.
"""

from __future__ import annotations

import base64
import io
import os
import re
import urllib.request
from typing import Any

# Regex patterns for detecting existing page markers in MD, DOCX, or plain text
EXISTING_PAGE_MARKER_RE = re.compile(r"^===== PAGE (\d+) =====\s*$", re.MULTILINE)
MD_PAGE_HEADING_RE = re.compile(
    r"^(?:#{1,6}\s*)?(?:صفحة|صفحه|Page)\s+(\d+)(?:\s*(?:من|/|of)\s*\d+)?\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Corrupted font-encoding artifact lines (single ASCII punctuation/digits from broken PDF exports)
CORRUPTED_GLYPH_LINE_RE = re.compile(r'^[\s!"#$%&\'()*+,\-./:;<=>?@\[\\\]^_`{|}~\x00-\x1f]+$')

SUPPORTED_EXTENSIONS = {".pdf", ".xlsx", ".docx", ".md", ".markdown", ".txt", ".csv"}


class IngestionError(ValueError):
    """Raised when document input format is invalid, unsupported, or unreadable."""


def _clean_overlapping_lines(lines: list[str]) -> list[str]:
    """Deduplicate consecutive lines where a PDF renderer emitted a truncated line
    followed immediately by the wrapped tail (or repeated suffix).
    """
    cleaned: list[str] = []
    for raw_line in lines:
        # Remove control chars except tab/newline
        line = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", raw_line).strip()
        if not line:
            continue
        # Skip pure ASCII symbol artifacts from broken font maps (unless it's a dash or percentage)
        if CORRUPTED_GLYPH_LINE_RE.match(line) and line not in ("-", "%"):
            continue
        if cleaned:
            prev = cleaned[-1]
            # Exact duplicate consecutive line
            if line == prev:
                continue
            # Check if `line` is a continuation/overlap of `prev`:
            # e.g. prev="...والصحية والسكنية، وفق معايير ... وحفظ كرام"
            #      line="والصحية والسكنية، وفق معايير ... وحفظ كرامة المستفيدين"
            if len(prev) >= 20 and len(line) >= 10:
                max_overlap = min(len(prev), len(line))
                merged = False
                for k in range(max_overlap, 12, -1):
                    # Prefix of `line` matches suffix of `prev` (possibly missing 1-2 clipped chars at end of prev)
                    for trim in (0, 1, 2):
                        prev_trimmed = prev[: len(prev) - trim] if trim else prev
                        if len(prev_trimmed) >= k and prev_trimmed.endswith(line[:k]):
                            cleaned[-1] = prev_trimmed[: len(prev_trimmed) - k] + line
                            merged = True
                            break
                    if merged:
                        break
                if merged:
                    continue
                # Or `line` is simply the last word(s) already at the end of `prev`
                norm_prev = prev.rstrip(".")
                norm_line = line.rstrip(".")
                if len(norm_line) >= 4 and norm_prev.endswith(norm_line):
                    if line.endswith(".") and not prev.endswith("."):
                        cleaned[-1] = prev + "."
                    continue
        cleaned.append(line)
    return cleaned


def clean_extracted_text(text: str) -> str:
    """Clean control characters and consecutive duplicate/overlapping PDF lines while keeping structure."""
    lines = text.splitlines()
    cleaned = _clean_overlapping_lines(lines)
    return "\n".join(cleaned)


def _needs_ocr(page_text: str, image_count: int) -> bool:
    """Determine if a PDF page requires OCR.
    Avoids expensive OCR when usable native Arabic/Latin text already exists.
    """
    stripped = re.sub(r"\s+", "", page_text or "")
    # Count meaningful Arabic or Latin letters
    letters = re.findall(r"[\u0600-\u06FFa-zA-Z]", stripped)
    if len(letters) >= 40:
        return False
    if image_count > 0 and len(letters) < 40:
        return True
    return len(letters) < 15 and len(stripped) < 30


def _ocr_pdf_page(page: Any, lang: str = "ara+eng") -> str:
    """Run Arabic+English OCR on a PyMuPDF page if an OCR engine (pytesseract or rapidocr) is available."""
    try:
        pix = page.get_pixmap(dpi=200)
        img_bytes = pix.tobytes("png")
    except (RuntimeError, ValueError, AttributeError):
        return ""

    # Try pytesseract first (standard for Arabic + English with Tesseract ara.traineddata)
    try:
        import pytesseract
        from PIL import Image

        img = Image.open(io.BytesIO(img_bytes))
        ocr_text = pytesseract.image_to_string(img, lang=lang)
        if ocr_text and ocr_text.strip():
            return ocr_text.strip()
    except Exception as exc:  # noqa: BLE001
        _ = exc

    # Try PyMuPDF built-in Tesseract OCR if configured
    try:
        tp = page.get_textpage_ocr(language=lang, dpi=200, full=True)
        ocr_text = page.get_text("text", textpage=tp)
        if ocr_text and ocr_text.strip():
            return ocr_text.strip()
    except Exception as exc:  # noqa: BLE001
        _ = exc

    return ""


def ingest_pdf_bytes(data: bytes, filename: str = "document.pdf", enable_ocr: bool = True) -> dict[str, Any]:
    """Extract text page-by-page from a PDF file.
    Uses native selectable text when available and falls back to Arabic OCR on scanned pages.
    """
    if not data or not data.lstrip().startswith(b"%PDF"):
        raise IngestionError(f"Invalid or corrupted PDF file: '{filename}'")

    try:
        import pymupdf
    except ImportError:
        import fitz as pymupdf  # type: ignore[no-redef]

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise IngestionError(f"Failed to open PDF '{filename}': {exc}") from exc

    pages_out: list[tuple[int, str]] = []
    ocr_pages: list[int] = []

    for idx in range(len(doc)):
        page_num = idx + 1
        page = doc[idx]
        native_text = page.get_text("text") or ""
        imgs = page.get_images()

        if enable_ocr and _needs_ocr(native_text, len(imgs)):
            ocr_text = _ocr_pdf_page(page)
            if ocr_text and len(ocr_text.strip()) > len(native_text.strip()):
                native_text = ocr_text
                ocr_pages.append(page_num)

        cleaned = clean_extracted_text(native_text)
        pages_out.append((page_num, cleaned))

    doc.close()
    rendered = "\n\n".join(f"===== PAGE {no} =====\n{body}" for no, body in pages_out if body)
    return {
        "filename": filename,
        "format": "pdf",
        "page_count": len(pages_out),
        "ocr_pages": ocr_pages,
        "pages": pages_out,
        "text": rendered,
    }


def _resolve_xlsx_merged_cells(ws: Any) -> dict[tuple[int, int], Any]:
    """Build a lookup of merged cell coordinates to their top-left value."""
    merged_lookup: dict[tuple[int, int], Any] = {}
    for merged_range in ws.merged_cells.ranges:
        top_val = ws.cell(merged_range.min_row, merged_range.min_col).value
        if top_val is not None:
            for r in range(merged_range.min_row, merged_range.max_row + 1):
                for c in range(merged_range.min_col, merged_range.max_col + 1):
                    if (r, c) != (merged_range.min_row, merged_range.min_col):
                        merged_lookup[(r, c)] = top_val
    return merged_lookup


def _format_cell_value(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, bool):
        return str(val)
    if isinstance(val, float):
        if val.is_integer():
            return str(int(val))
        return f"{val:.2f}".rstrip("0").rstrip(".")
    if isinstance(val, int):
        return str(val)
    return str(val).strip()


def ingest_xlsx_bytes(data: bytes, filename: str = "document.xlsx") -> dict[str, Any]:
    """Parse all relevant worksheets in an Excel workbook.
    - Detects page-text sheets (e.g. 'نص الصفحات' / columns 'رقم الصفحة' + text) and restores page structure.
    - Preserves structured tables, project rows, numeric values, and worksheet context.
    """
    if not data:
        raise IngestionError(f"Empty Excel file: '{filename}'")

    try:
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    except Exception as exc:
        raise IngestionError(f"Failed to parse XLSX '{filename}': {exc}") from exc

    extracted_pages: dict[int, list[str]] = {}
    table_sections: list[str] = []
    sheet_summaries: list[dict[str, Any]] = []

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        merged_map = _resolve_xlsx_merged_cells(ws)

        rows_data: list[list[Any]] = []
        for r in range(1, ws.max_row + 1):
            row_vals = []
            for c in range(1, ws.max_column + 1):
                v = ws.cell(r, c).value
                if v is None and (r, c) in merged_map:
                    v = merged_map[(r, c)]
                row_vals.append(v)
            if any(v is not None and str(v).strip() != "" for v in row_vals):
                rows_data.append(row_vals)

        if not rows_data:
            continue

        headers = [_format_cell_value(h) for h in rows_data[0]]
        sheet_summaries.append({"sheet": sheet_name, "rows": len(rows_data), "headers": headers})

        # Check if this worksheet is a page-by-page text dump (like 'نص الصفحات')
        is_page_sheet = (
            len(headers) >= 2
            and any(k in headers[0] for k in ("صفحة", "صفحه", "page", "Page"))
            and any(k in headers[1] for k in ("نص", "text", "Text", "محتوى"))
        )

        if is_page_sheet:
            for row in rows_data[1:]:
                p_raw = row[0]
                body_raw = row[1] if len(row) > 1 else ""
                try:
                    p_no = int(float(str(p_raw).strip()))
                except (ValueError, TypeError):
                    p_no = len(extracted_pages) + 1
                cleaned_body = clean_extracted_text(str(body_raw or ""))
                if cleaned_body:
                    extracted_pages.setdefault(p_no, []).append(cleaned_body)
        else:
            # Structured data / budget / projects worksheet
            lines = [f"[ورقة عمل Excel: {sheet_name}]"]
            if headers:
                lines.append(" | ".join(h for h in headers if h))
            for row in rows_data[1:]:
                pairs = []
                for idx, val in enumerate(row):
                    val_s = _format_cell_value(val)
                    if not val_s:
                        continue
                    col_name = headers[idx] if idx < len(headers) and headers[idx] else f"عمود {idx + 1}"
                    pairs.append(f"{col_name}: {val_s}")
                if pairs:
                    lines.append(" ؛ ".join(pairs))
            table_sections.append("\n".join(lines))

    pages_out: list[tuple[int, str]] = []
    if extracted_pages:
        for p_no in sorted(extracted_pages.keys()):
            pages_out.append((p_no, "\n\n".join(extracted_pages[p_no])))
        if table_sections:
            # Append structured worksheets as an additional reference page or enrich existing pages
            next_page = max(extracted_pages.keys()) + 1
            pages_out.append((next_page, "\n\n".join(table_sections)))
    else:
        for idx, sec in enumerate(table_sections, 1):
            pages_out.append((idx, sec))

    rendered = "\n\n".join(f"===== PAGE {no} =====\n{body}" for no, body in pages_out if body)
    return {
        "filename": filename,
        "format": "xlsx",
        "page_count": len(pages_out),
        "sheets": sheet_summaries,
        "pages": pages_out,
        "text": rendered,
    }


def _iter_docx_block_items(doc: Any):
    """Iterate through paragraphs and tables in a DOCX document in exact reading order."""
    from docx.oxml.table import CT_Tbl
    from docx.oxml.text.paragraph import CT_P
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    body = doc.element.body
    for child in body.iterchildren():
        if isinstance(child, CT_P):
            yield ("paragraph", Paragraph(child, doc))
        elif isinstance(child, CT_Tbl):
            yield ("table", Table(child, doc))


def ingest_docx_bytes(data: bytes, filename: str = "document.docx") -> dict[str, Any]:
    """Parse DOCX paragraphs, headings, and tables in reading order while preserving page markers."""
    if not data:
        raise IngestionError(f"Empty DOCX file: '{filename}'")

    try:
        import docx

        doc = docx.Document(io.BytesIO(data))
    except Exception as exc:
        raise IngestionError(f"Failed to parse DOCX '{filename}': {exc}") from exc

    lines: list[str] = []
    for kind, item in _iter_docx_block_items(doc):
        if kind == "paragraph":
            txt = (item.text or "").strip()
            if txt:
                lines.append(txt)
        elif kind == "table":
            for row in item.rows:
                # Deduplicate adjacent merged cells in DOCX row
                row_cells: list[str] = []
                for cell in row.cells:
                    c_text = " ".join(p.strip() for p in (cell.text or "").splitlines() if p.strip())
                    if c_text and (not row_cells or row_cells[-1] != c_text):
                        row_cells.append(c_text)
                if row_cells:
                    lines.append(" | ".join(row_cells))

    full_text = "\n".join(lines)
    pages_out = split_text_into_pages(full_text)
    rendered = "\n\n".join(f"===== PAGE {no} =====\n{body}" for no, body in pages_out if body)
    return {
        "filename": filename,
        "format": "docx",
        "page_count": len(pages_out),
        "pages": pages_out,
        "text": rendered,
    }


def ingest_markdown_or_text(text: str, filename: str = "document.md") -> dict[str, Any]:
    """Ingest Markdown or plain text, preserving headings, tables, and page boundaries."""
    if not isinstance(text, str):
        raise IngestionError(f"Expected string text for '{filename}'")
    pages_out = split_text_into_pages(text)
    rendered = "\n\n".join(f"===== PAGE {no} =====\n{body}" for no, body in pages_out if body)
    ext = os.path.splitext(filename)[1].lower()
    fmt = "markdown" if ext in (".md", ".markdown") else "text"
    return {
        "filename": filename,
        "format": fmt,
        "page_count": len(pages_out),
        "pages": pages_out,
        "text": rendered,
    }


def split_text_into_pages(text: str) -> list[tuple[int, str]]:
    """Split text into `(page_number, cleaned_body)` tuples using either `===== PAGE N =====`
    or Arabic/English page headings like `## صفحة 1` or `صفحة 1 من 72`.
    """
    text = text or ""
    # 1. Check for existing ===== PAGE N ===== markers
    explicit_matches = list(EXISTING_PAGE_MARKER_RE.finditer(text))
    if explicit_matches:
        pages: list[tuple[int, str]] = []
        for i, m in enumerate(explicit_matches):
            end = explicit_matches[i + 1].start() if i + 1 < len(explicit_matches) else len(text)
            body = clean_extracted_text(text[m.end() : end])
            if body:
                pages.append((int(m.group(1)), body))
        return pages if pages else [(1, clean_extracted_text(text))]

    # 2. Check for Markdown / DOCX page headings: `## صفحة 1` or `صفحة 1 من 72`
    md_matches = list(MD_PAGE_HEADING_RE.finditer(text))
    if md_matches:
        pages = []
        for i, m in enumerate(md_matches):
            end = md_matches[i + 1].start() if i + 1 < len(md_matches) else len(text)
            body = clean_extracted_text(text[m.end() : end])
            if body:
                pages.append((int(m.group(1)), body))
        return pages if pages else [(1, clean_extracted_text(text))]

    # 3. Single continuous document without page markers
    cleaned = clean_extracted_text(text)
    return [(1, cleaned)] if cleaned else []


def load_attachment_bytes(attachment: Any) -> tuple[str, bytes, str | None]:
    """Normalize an attachment specification into `(filename, raw_bytes, mime_type)`.

    Supported attachment shapes:
      - `{"filename": "a.pdf", "content_base64": "..."}` (or `"data"`, `"base64"`, `"content"`)
      - `{"filename": "a.pdf", "path": "/path/to/a.pdf"}`
      - `{"filename": "a.pdf", "url": "https://..."}`
      - `{"filename": "a.md", "text": "..."}`
    """
    if not isinstance(attachment, dict):
        raise IngestionError("Each file attachment must be a JSON object/dict.")

    filename = str(
        attachment.get("filename")
        or attachment.get("name")
        or attachment.get("original_name")
        or "attachment"
    ).strip()
    mime_type = attachment.get("mime_type") or attachment.get("content_type") or attachment.get("mimetype")

    # Direct text field
    if isinstance(attachment.get("text"), str) and not attachment.get("content_base64") and not attachment.get("data"):
        return filename, attachment["text"].encode("utf-8"), mime_type

    # Raw bytes
    if isinstance(attachment.get("bytes"), (bytes, bytearray)):
        return filename, bytes(attachment["bytes"]), mime_type

    # Base64 encoded content
    b64_val = (
        attachment.get("content_base64")
        or attachment.get("base64")
        or attachment.get("data")
        or attachment.get("content")
    )
    if isinstance(b64_val, str) and b64_val.strip():
        s = b64_val.strip()
        # Strip data URI prefix if present: data:application/pdf;base64,...
        if s.startswith("data:") and "," in s:
            header, s = s.split(",", 1)
            if not mime_type and ";" in header:
                mime_type = header[5:].split(";", 1)[0]
        try:
            raw = base64.b64decode(s, validate=False)
            if raw:
                return filename, raw, mime_type
        except Exception as exc:
            raise IngestionError(f"Invalid base64 encoding for file '{filename}': {exc}") from exc

    # Local path
    file_path = attachment.get("path") or attachment.get("file_path")
    if isinstance(file_path, str) and file_path.strip():
        if not os.path.isfile(file_path):
            raise IngestionError(f"File path not found: '{file_path}'")
        with open(file_path, "rb") as f:
            return filename, f.read(), mime_type

    # Remote URL
    url = attachment.get("url") or attachment.get("file_url")
    if isinstance(url, str) and url.strip():
        try:
            req = urllib.request.Request(url.strip(), headers={"User-Agent": "Athar-Extractor/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return filename, resp.read(), mime_type
        except Exception as exc:
            raise IngestionError(f"Failed to download attachment from URL '{url}': {exc}") from exc

    raise IngestionError(f"Attachment '{filename}' has no readable content (expected content_base64, path, url, or text).")


def detect_file_format(filename: str, data: bytes, mime_type: str | None = None) -> str:
    """Detect file format from magic bytes, extension, or MIME type."""
    ext = os.path.splitext(filename.lower())[1]
    if data.lstrip().startswith(b"%PDF") or ext == ".pdf" or (mime_type and "pdf" in mime_type.lower()):
        return "pdf"
    if ext == ".xlsx" or (mime_type and ("spreadsheet" in mime_type.lower() or "excel" in mime_type.lower())):
        return "xlsx"
    if ext == ".docx" or (mime_type and "wordprocessingml" in mime_type.lower()):
        return "docx"
    # Check ZIP magic bytes if extension is ambiguous
    if data.startswith(b"PK\x03\x04"):
        import zipfile

        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = set(zf.namelist())
                if "xl/workbook.xml" in names:
                    return "xlsx"
                if "word/document.xml" in names:
                    return "docx"
        except (zipfile.BadZipFile, OSError) as exc:
            _ = exc
    if ext in (".md", ".markdown") or (mime_type and "markdown" in mime_type.lower()):
        return "markdown"
    if ext in (".txt", ".csv") or (mime_type and mime_type.lower().startswith("text/")):
        return "text"
    # Try UTF-8 decode fallback for extensionless text inputs
    try:
        data.decode("utf-8")
        return "text"
    except UnicodeDecodeError as exc:
        raise IngestionError(
            f"Unsupported file format for '{filename}' (ext='{ext}', mime='{mime_type}'). "
            f"Supported formats: PDF, XLSX, DOCX, MD, TXT."
        ) from exc


def ingest_single_attachment(attachment: Any, enable_ocr: bool = True) -> dict[str, Any]:
    """Ingest a single attachment dict and return structured page text and metadata."""
    filename, data, mime_type = load_attachment_bytes(attachment)
    if not data:
        raise IngestionError(f"Attachment '{filename}' is empty (0 bytes).")

    fmt = detect_file_format(filename, data, mime_type)
    if fmt == "pdf":
        return ingest_pdf_bytes(data, filename=filename, enable_ocr=enable_ocr)
    if fmt == "xlsx":
        return ingest_xlsx_bytes(data, filename=filename)
    if fmt == "docx":
        return ingest_docx_bytes(data, filename=filename)
    if fmt in ("markdown", "text"):
        try:
            text_str = data.decode("utf-8")
        except UnicodeDecodeError:
            text_str = data.decode("utf-8", errors="replace")
        return ingest_markdown_or_text(text_str, filename=filename)

    raise IngestionError(f"Unsupported format '{fmt}' for file '{filename}'.")


def ingest_request_inputs(job_input: dict[str, Any], enable_ocr: bool = True) -> dict[str, Any]:
    """Unified ingestion entry point for RunPod / n8n / backend requests.

    Supports:
      1. Direct `text` and/or `files` (list of attachment objects or filenames).
      2. Backend/n8n contract shape where `input` is a JSON-encoded string:
         `{"text": "...", "files": ["brief.pdf"]}` alongside binary attachments in `files` / `attachments` / `files_binary`.
      3. Multiple attachments (including duplicate filenames matched by position).
    """
    if not isinstance(job_input, dict):
        raise IngestionError("Request input must be a JSON object.")

    # Parse nested backend/n8n `input` JSON string if present
    nested_input_raw = job_input.get("input")
    nested_input: dict[str, Any] = {}
    if isinstance(nested_input_raw, str) and nested_input_raw.strip():
        import json

        try:
            parsed = json.loads(nested_input_raw)
            if isinstance(parsed, dict):
                nested_input = parsed
            else:
                raise IngestionError("Field 'input' JSON string must decode to a JSON object.")
        except json.JSONDecodeError as exc:
            raise IngestionError(f"Invalid JSON string in 'input' field: {exc}") from exc
    elif isinstance(nested_input_raw, dict):
        nested_input = nested_input_raw

    # Extract pasted text (check top-level `text` and nested `input.text`)
    pasted_text = job_input.get("text")
    if pasted_text in (None, "") and isinstance(nested_input.get("text"), str):
        pasted_text = nested_input.get("text")
    pasted_text = (pasted_text or "").strip()

    # Collect filenames from nested `input.files` if provided
    ordered_filenames: list[str] = []
    if isinstance(nested_input.get("files"), list):
        ordered_filenames = [str(fn) for fn in nested_input["files"]]

    # Collect binary/structured attachments from `files`, `files[]`, or `attachments`
    raw_attachments = (
        job_input.get("attachments")
        or job_input.get("files_data")
        or job_input.get("files[]")
        or job_input.get("files")
        or []
    )
    # If `job_input["files"]` is just a list of filename strings (from `input.files`), check if binaries are in `files[]` or `attachments`
    if isinstance(raw_attachments, list) and all(isinstance(x, str) for x in raw_attachments):
        if not ordered_filenames:
            ordered_filenames = list(raw_attachments)
        raw_attachments = job_input.get("files[]") or job_input.get("attachments") or []

    if not isinstance(raw_attachments, list):
        raise IngestionError("Field 'files' or 'attachments' must be a list.")

    ingested_docs: list[dict[str, Any]] = []
    for idx, att in enumerate(raw_attachments):
        if isinstance(att, dict):
            att_copy = dict(att)
            # Align positional filename from `input.files[idx]` if not already set on the binary object
            if idx < len(ordered_filenames) and not att_copy.get("filename") and not att_copy.get("name"):
                att_copy["filename"] = ordered_filenames[idx]
            elif idx < len(ordered_filenames) and ordered_filenames[idx]:
                # Keep original positional filename from input.files as authoritative
                att_copy["filename"] = ordered_filenames[idx]
            ingested_docs.append(ingest_single_attachment(att_copy, enable_ocr=enable_ocr))
        else:
            raise IngestionError(f"Attachment at index {idx} must be a dict with file data.")

    if not pasted_text and not ingested_docs:
        raise IngestionError("Request contains neither pasted 'text' nor readable 'files' attachments.")

    # Merge pages across pasted text and attachments while preserving page numbers per document
    combined_pages: list[tuple[int, str]] = []
    sources_meta: list[dict[str, Any]] = []

    if pasted_text:
        pasted_doc = ingest_markdown_or_text(pasted_text, filename="pasted_text.txt")
        sources_meta.append(
            {
                "source": "pasted_text",
                "format": "text",
                "page_count": pasted_doc["page_count"],
            }
        )
        combined_pages.extend(pasted_doc["pages"])

    # Deduplicate identical page bodies if the same document is submitted in multiple derived formats (e.g. MD + DOCX + XLSX)
    seen_page_signatures: set[tuple[int, str]] = set()
    for p_no, p_body in combined_pages:
        sig = (p_no, re.sub(r"\s+", "", p_body)[:300])
        seen_page_signatures.add(sig)

    for doc_idx, doc_info in enumerate(ingested_docs):
        sources_meta.append(
            {
                "index": doc_idx,
                "filename": doc_info["filename"],
                "format": doc_info["format"],
                "page_count": doc_info["page_count"],
                "ocr_pages": doc_info.get("ocr_pages", []),
            }
        )
        for p_no, p_body in doc_info["pages"]:
            norm_prefix = re.sub(r"\s+", "", p_body)[:300]
            sig = (p_no, norm_prefix)
            if sig in seen_page_signatures and len(norm_prefix) > 80:
                # If an existing page has shorter/corrupted text and the new one (e.g. PDF) is richer, upgrade it
                for existing_idx, (ep_no, ep_body) in enumerate(combined_pages):
                    if ep_no == p_no and len(p_body) > len(ep_body) * 1.15:
                        combined_pages[existing_idx] = (p_no, p_body)
                        break
                continue
            seen_page_signatures.add(sig)
            combined_pages.append((p_no, p_body))

    combined_text = "\n\n".join(f"===== PAGE {no} =====\n{body}" for no, body in combined_pages if body)
    return {
        "text": combined_text,
        "pages": combined_pages,
        "sources": sources_meta,
        "pasted_text": pasted_text,
        "ingested_documents": ingested_docs,
    }
