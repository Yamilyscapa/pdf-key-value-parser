# PDF Parser Service

Standalone FastAPI service that parses one or many PDF files and returns simplified JSON.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Run

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

## Run with Docker

Build image:

```bash
docker build -t pdf-parser-service .
```

Run container:

```bash
docker run --rm -p 8000:8000 pdf-parser-service
```

Run with custom limits:

```bash
docker run --rm -p 8000:8000 \
  -e MAX_FILES_PER_REQUEST=30 \
  -e MAX_FILE_SIZE_MB=25 \
  pdf-parser-service
```

## Endpoints

- `GET /health`
- `POST /parse-pdf` (multipart form-data)

### Single file

```bash
curl -X POST "http://localhost:8000/parse-pdf" \
  -F "file=@/path/to/file.pdf"
```

### Bulk files (partial success)

```bash
curl -X POST "http://localhost:8000/parse-pdf" \
  -F "files=@/path/to/a.pdf" \
  -F "files=@/path/to/b.pdf"
```

### Optional form fields

- `pages` (example: `1,3-5`)
- `password` (for encrypted PDFs)
- `include_raw` (`true` or `false`)

## Response shape

```json
{
  "processed": 2,
  "failures": 1,
  "results": [
    {
      "filename": "a.pdf",
      "status": "success",
      "data": {
        "source": "a.pdf",
        "page_count": 2,
        "fields": {
          "id": "123"
        },
        "duplicate_keys": [],
        "ocr_required_pages": []
      }
    },
    {
      "filename": "b.pdf",
      "status": "error",
      "error": "Invalid file. Only PDF files are supported."
    }
  ]
}
```

## Limits

- `MAX_FILES_PER_REQUEST` (default `20`)
- `MAX_FILE_SIZE_MB` (default `15`)
