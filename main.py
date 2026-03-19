from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

fitz = importlib.import_module("pymupdf")


FIELD_LINE_RE = re.compile(r"^(?P<raw_key>[^:=\n]{1,120}?)(?P<separator>[:=])(?P<raw_value>.*)$")
TIME_ONLY_RE = re.compile(r"^\s*\d{1,2}:\d{2}(?:\s*[APap][Mm])?\s*$")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Extract key-value fields from a PDF and save JSON output.",
    )
    parser.add_argument("input_pdf", help="Path to PDF file")
    parser.add_argument(
        "--output",
        help="Output JSON path. Defaults to <input filename>.json",
    )
    parser.add_argument(
        "--pages",
        help="Comma-separated page spec, e.g. '1,3-5'. Defaults to all pages.",
    )
    parser.add_argument(
        "--password",
        help="Password for encrypted PDFs",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output",
    )
    parser.add_argument(
        "--include-raw",
        action="store_true",
        help="Include raw extracted lines for each page",
    )
    return parser.parse_args(argv)


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


def extract_fields_from_lines(lines: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
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

    return field_items, fields


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v) for v in value]
    return str(value)


def resolve_output_path(input_pdf: Path, output: str | None) -> Path:
    if output:
        return Path(output)
    return input_pdf.with_suffix(".json")


def run(args: argparse.Namespace) -> dict[str, Any]:
    input_pdf = Path(args.input_pdf)
    if not input_pdf.exists():
        raise FileNotFoundError(f"Input PDF does not exist: {input_pdf}")

    with fitz.open(input_pdf) as doc:
        if doc.needs_pass:
            if not args.password:
                raise ValueError("PDF is encrypted. Provide --password.")
            if not doc.authenticate(args.password):
                raise ValueError("Invalid PDF password.")

        selected_pages = parse_pages_spec(args.pages, doc.page_count)

        pages_payload: list[dict[str, Any]] = []
        all_field_items: list[dict[str, Any]] = []
        all_fields: dict[str, Any] = {}
        ocr_required_pages: list[int] = []

        for page_number in selected_pages:
            page = doc[page_number - 1]
            lines, ocr_required = extract_lines(page, page_number)
            field_items, page_fields = extract_fields_from_lines(lines)

            for item in field_items:
                merge_duplicate_field(all_fields, item["key"], item["value"])
            all_field_items.extend(field_items)

            if ocr_required:
                ocr_required_pages.append(page_number)

            page_payload: dict[str, Any] = {
                "page_number": page_number,
                "width": float(page.rect.width),
                "height": float(page.rect.height),
                "field_items": field_items,
                "fields": page_fields,
                "ocr_required": ocr_required,
            }
            if args.include_raw:
                page_payload["raw_lines"] = lines
            pages_payload.append(page_payload)

        return {
            "source": str(input_pdf.resolve()),
            "parsed_at": datetime.now(timezone.utc).isoformat(),
            "library": {
                "name": "PyMuPDF",
                "version": getattr(fitz, "__version__", None) or getattr(fitz, "VersionBind", None),
            },
            "document_page_count": doc.page_count,
            "selected_page_count": len(selected_pages),
            "metadata": json_safe(doc.metadata),
            "pages": pages_payload,
            "field_items": all_field_items,
            "fields": all_fields,
            "ocr_required_pages": ocr_required_pages,
        }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])

    try:
        payload = run(args)
        output_path = resolve_output_path(Path(args.input_pdf), args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        indent = 2 if args.pretty else None
        with output_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=indent)
            if indent is not None:
                f.write("\n")

        print(f"Saved JSON to {output_path}")
        print(f"Extracted {len(payload['field_items'])} field item(s)")
        if payload["ocr_required_pages"]:
            page_list = ", ".join(str(p) for p in payload["ocr_required_pages"])
            print(f"OCR likely required for page(s): {page_list}")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
