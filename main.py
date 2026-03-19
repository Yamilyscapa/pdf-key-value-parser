from __future__ import annotations
# pyright: reportMissingImports=false

import os
from typing import Any, Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from parser import parse_pdf_bytes


MAX_FILES_PER_REQUEST = int(os.getenv("MAX_FILES_PER_REQUEST", "20"))
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "15"))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024


class ParsedPdfPayload(BaseModel):
    source: str
    page_count: int
    fields: dict[str, Any]
    duplicate_keys: list[str]
    ocr_required_pages: list[int]
    debug: dict[str, Any] | None = None


class ParseResult(BaseModel):
    filename: str
    status: Literal["success", "error"]
    data: ParsedPdfPayload | None = None
    error: str | None = None


class BulkParseResponse(BaseModel):
    processed: int
    failures: int
    results: list[ParseResult]


def is_upload_like(value: object) -> bool:
    return all(hasattr(value, attr) for attr in ("filename", "read", "close"))


def looks_like_pdf(upload: UploadFile, content: bytes) -> bool:
    name = (upload.filename or "").lower()
    mime = (upload.content_type or "").lower()
    by_name = name.endswith(".pdf")
    by_mime = "pdf" in mime
    by_signature = content.startswith(b"%PDF")
    return by_name or by_mime or by_signature


def combine_uploads(
    file: UploadFile | None,
    files: list[UploadFile] | None,
) -> list[UploadFile]:
    combined: list[UploadFile] = []
    if is_upload_like(file):
        combined.append(file)
    if files:
        combined.extend(item for item in files if is_upload_like(item))
    return combined


app = FastAPI(title="PDF Parser Service", version="1.0.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/parse-pdf", response_model=BulkParseResponse)
async def parse_pdf_endpoint(
    file: UploadFile | None = File(default=None),
    files: list[UploadFile] | None = File(default=None),
    pages: str | None = Form(default=None),
    include_raw: bool = Form(default=False),
    password: str | None = Form(default=None),
) -> BulkParseResponse:
    uploads = combine_uploads(file, files)
    pages_value = pages if isinstance(pages, str) else None
    include_raw_value = include_raw if isinstance(include_raw, bool) else False
    password_value = password if isinstance(password, str) else None

    if not uploads:
        raise HTTPException(
            status_code=400,
            detail="No PDF files uploaded. Use multipart form-data field 'file' or 'files'.",
        )

    if len(uploads) > MAX_FILES_PER_REQUEST:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files. Maximum allowed is {MAX_FILES_PER_REQUEST}.",
        )

    results: list[ParseResult] = []

    for upload in uploads:
        filename = upload.filename or "uploaded.pdf"

        try:
            content = await upload.read()

            if not content:
                results.append(
                    ParseResult(
                        filename=filename,
                        status="error",
                        error="Uploaded file is empty.",
                    )
                )
                continue

            if len(content) > MAX_FILE_SIZE_BYTES:
                results.append(
                    ParseResult(
                        filename=filename,
                        status="error",
                        error=f"File exceeds size limit of {MAX_FILE_SIZE_MB} MB.",
                    )
                )
                continue

            if not looks_like_pdf(upload, content):
                results.append(
                    ParseResult(
                        filename=filename,
                        status="error",
                        error="Invalid file. Only PDF files are supported.",
                    )
                )
                continue

            payload = parse_pdf_bytes(
                pdf_bytes=content,
                source=filename,
                pages=pages_value,
                password=password_value,
                include_raw=include_raw_value,
            )

            results.append(
                ParseResult(
                    filename=filename,
                    status="success",
                    data=ParsedPdfPayload(**payload),
                )
            )

        except Exception as exc:
            results.append(
                ParseResult(
                    filename=filename,
                    status="error",
                    error=str(exc),
                )
            )
        finally:
            await upload.close()

    failures = sum(1 for item in results if item.status == "error")
    return BulkParseResponse(
        processed=len(results),
        failures=failures,
        results=results,
    )
