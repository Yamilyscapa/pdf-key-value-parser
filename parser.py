from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Any

fitz = importlib.import_module("pymupdf")

FIELD_LINE_RE = re.compile(r"^(?P<raw_key>[^:=\n]{1,120}?)(?P<separator>[:=])(?P<raw_value>.*)$")
TIME_ONLY_RE = re.compile(r"^\s*\d{1,2}:\d{2}(?:\s*[APap][Mm])?\s*$")
UUID_RE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$", re.IGNORECASE)
FILENAME_UUID_RE = re.compile(r"([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})@.+\.pdf", re.IGNORECASE)


def parse_pages_spec(spec: str | None, page_count: int) -> list[int]:
    if spec is None:
        return list(range(1, page_count + 1))

    selected: list[int] = []
    seen: set[int] = set()

    for token in spec.split(","):
        part = token.strip()
        if not part:
            continue

        if "-" in part:
            raw_start, raw_end = part.split("-", 1)
            try:
                start = int(raw_start)
                end = int(raw_end)
            except ValueError as exc:
                raise ValueError(f"Invalid page range '{part}'") from exc
            if start > end:
                raise ValueError(f"Invalid page range '{part}': start > end")
            candidates = range(start, end + 1)
        else:
            try:
                candidates = [int(part)]
            except ValueError as exc:
                raise ValueError(f"Invalid page number '{part}'") from exc

        for page_no in candidates:
            if page_no < 1 or page_no > page_count:
                raise ValueError(
                    f"Page {page_no} is out of bounds (valid range: 1-{page_count})"
                )
            if page_no not in seen:
                seen.add(page_no)
                selected.append(page_no)

    if not selected:
        raise ValueError("No valid pages selected")

    return selected


def normalize_bbox(bbox: Any) -> list[float]:
    if not bbox:
        return []
    try:
        return [float(v) for v in bbox]
    except Exception:
        return []


def extract_lines(page: Any, page_number: int) -> tuple[list[dict[str, Any]], bool]:
    text_dict = page.get_text("dict")
    blocks = text_dict.get("blocks", [])

    lines: list[dict[str, Any]] = []
    has_image_block = False

    for block_index, block in enumerate(blocks):
        block_type = block.get("type")
        if block_type == 1:
            has_image_block = True
            continue
        if block_type != 0:
            continue

        for line_index, line in enumerate(block.get("lines", [])):
            text_fragments: list[str] = []
            for span in line.get("spans", []):
                text_fragments.append(span.get("text", ""))
            line_text = "".join(text_fragments)
            if not line_text:
                continue

            lines.append(
                {
                    "page_number": page_number,
                    "block_index": block_index,
                    "line_index": line_index,
                    "bbox": normalize_bbox(line.get("bbox")),
                    "text": line_text,
                }
            )

    lines.sort(
        key=lambda entry: (
            entry["bbox"][1] if len(entry["bbox"]) >= 2 else 0.0,
            entry["bbox"][0] if len(entry["bbox"]) >= 1 else 0.0,
            entry["block_index"],
            entry["line_index"],
        )
    )

    for ordinal, line in enumerate(lines, start=1):
        line["line_ordinal"] = ordinal

    ocr_required = has_image_block and not any(line["text"].strip() for line in lines)
    return lines, ocr_required


def parse_field_line(text: str) -> dict[str, str] | None:
    if "://" in text or TIME_ONLY_RE.match(text):
        return None

    match = FIELD_LINE_RE.match(text)
    if not match:
        return None

    key = match.group("raw_key").strip()
    value = match.group("raw_value").strip()
    separator = match.group("separator")

    if not key:
        return None
    if len(key) > 120:
        return None
    if not re.search(r"[A-Za-z]", key):
        return None

    return {
        "key": key,
        "value": value,
        "separator": separator,
    }


def should_append_continuation(line_text: str, existing_value: str) -> bool:
    stripped = line_text.strip()
    if not stripped:
        return False
    if line_text[:1].isspace():
        return True
    if not existing_value:
        return True
    return existing_value.endswith(("-", "/", ",", ";"))


def merge_duplicate_field(fields: dict[str, Any], key: str, value: str) -> None:
    if key not in fields:
        fields[key] = value
        return

    current = fields[key]
    if isinstance(current, list):
        current.append(value)
    else:
        fields[key] = [current, value]


def extract_fields_from_lines(lines: list[dict[str, Any]], source: str = "") -> tuple[list[dict[str, Any]], dict[str, Any]]:
    field_items: list[dict[str, Any]] = []
    active_index: int | None = None

    for line in lines:
        text = line["text"]
        if not text.strip():
            active_index = None
            continue

        parsed = parse_field_line(text)
        if parsed is not None:
            item = {
                "key": parsed["key"],
                "value": parsed["value"],
                "separator": parsed["separator"],
                "page_number": line["page_number"],
                "line_ordinal": line["line_ordinal"],
                "bbox": line["bbox"],
                "raw_lines": [text],
            }
            field_items.append(item)
            active_index = len(field_items) - 1
            continue

        if active_index is None:
            continue

        existing_value = field_items[active_index]["value"]
        if not should_append_continuation(text, existing_value):
            active_index = None
            continue

        continuation = text.strip()
        field_items[active_index]["raw_lines"].append(text)
        field_items[active_index]["value"] = (
            f"{existing_value}\n{continuation}" if existing_value else continuation
        )

    for item in field_items:
        item["raw"] = "\n".join(item["raw_lines"])

    fields: dict[str, Any] = {}
    for item in field_items:
        merge_duplicate_field(fields, item["key"], item["value"])

    folio_value = fields.get("Folio Fiscal") or fields.get("Folio fiscal")
    needs_recovery = not folio_value or not UUID_RE.match(folio_value)

    if needs_recovery:
        recovered_from_same_row = False
        for idx, item in enumerate(field_items):
            if item["key"] == "Folio Fiscal":
                same_row_uuid = find_uuid_on_same_row(lines, item["bbox"])
                if same_row_uuid:
                    fields["Folio Fiscal"] = same_row_uuid
                    recovered_from_same_row = True
                    break

        if not recovered_from_same_row:
            for idx, item in enumerate(field_items):
                if item["key"].lower() == "folio fiscal":
                    next_uuid = get_next_line_uuid(field_items, idx)
                    if next_uuid:
                        fields["Folio Fiscal"] = next_uuid["uuid"]
                        del field_items[next_uuid["index"]]
                        break
            else:
                filename_uuid = extract_uuid_from_filename(source)
                if not filename_uuid:
                    fields["foliofiscal"] = None

    return field_items, fields


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v) for v in value]
    return str(value)


def duplicate_keys(fields: dict[str, Any]) -> list[str]:
    return [key for key, value in fields.items() if isinstance(value, list)]


def get_next_line_uuid(field_items: list[dict[str, Any]], folio_fiscal_index: int) -> dict[str, Any] | None:
    next_index = folio_fiscal_index + 1
    if next_index >= len(field_items):
        return None
    next_item = field_items[next_index]
    last_line = next_item.get("raw_lines", [])[-1] if next_item.get("raw_lines") else None
    if last_line and UUID_RE.match(last_line.strip()):
        return {"uuid": last_line.strip(), "index": next_index}
    return None


def are_same_row(bbox1: list[float], bbox2: list[float], tolerance: float = 5.0) -> bool:
    if not bbox1 or not bbox2:
        return False
    y1_min, y1_max = bbox1[1], bbox1[3]
    y2_min, y2_max = bbox2[1], bbox2[3]
    return (min(y1_max, y2_max) - max(y1_min, y2_min)) > -tolerance


def find_uuid_on_same_row(lines: list[dict[str, Any]], field_bbox: list[float]) -> str | None:
    for line in lines:
        text = line["text"].strip()
        if UUID_RE.match(text) and are_same_row(line["bbox"], field_bbox):
            return text
    return None


def extract_uuid_from_filename(source: str) -> str | None:
    match = FILENAME_UUID_RE.search(source)
    if match:
        return match.group(1)
    return None


def parse_pdf_bytes(
    pdf_bytes: bytes,
    source: str,
    pages: str | None = None,
    password: str | None = None,
    include_raw: bool = False,
) -> dict[str, Any]:
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        if doc.needs_pass:
            if not password:
                raise ValueError("PDF is encrypted. Provide password.")
            if not doc.authenticate(password):
                raise ValueError("Invalid PDF password.")

        selected_pages = parse_pages_spec(pages, doc.page_count)

        all_fields: dict[str, Any] = {}
        ocr_required_pages: list[int] = []
        debug_pages: list[dict[str, Any]] = []

        for page_number in selected_pages:
            page = doc[page_number - 1]
            lines, ocr_required = extract_lines(page, page_number)
            field_items, page_fields = extract_fields_from_lines(lines, source)

            for item in field_items:
                merge_duplicate_field(all_fields, item["key"], item["value"])

            for folio_key in ("Folio Fiscal", "Folio fiscal", "foliofiscal"):
                if folio_key in page_fields:
                    all_fields[folio_key] = page_fields[folio_key]

            if ocr_required:
                ocr_required_pages.append(page_number)

            if include_raw:
                debug_pages.append(
                    {
                        "page_number": page_number,
                        "width": float(page.rect.width),
                        "height": float(page.rect.height),
                        "ocr_required": ocr_required,
                        "fields": page_fields,
                        "field_items": field_items,
                        "raw_lines": lines,
                    }
                )

        payload: dict[str, Any] = {
            "source": source,
            "page_count": len(selected_pages),
            "fields": all_fields,
            "duplicate_keys": duplicate_keys(all_fields),
            "ocr_required_pages": ocr_required_pages,
        }

        if include_raw:
            payload["debug"] = {
                "library": {
                    "name": "PyMuPDF",
                    "version": getattr(fitz, "__version__", None)
                    or getattr(fitz, "VersionBind", None),
                },
                "document_page_count": doc.page_count,
                "selected_pages": selected_pages,
                "metadata": json_safe(doc.metadata),
                "pages": debug_pages,
            }

        return payload


def parse_pdf_file(
    input_pdf: Path,
    pages: str | None = None,
    password: str | None = None,
    include_raw: bool = False,
) -> dict[str, Any]:
    if not input_pdf.exists():
        raise FileNotFoundError(f"Input PDF does not exist: {input_pdf}")

    return parse_pdf_bytes(
        pdf_bytes=input_pdf.read_bytes(),
        source=str(input_pdf.resolve()),
        pages=pages,
        password=password,
        include_raw=include_raw,
    )
