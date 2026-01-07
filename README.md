# PDF Form Filling Service

FastAPI backend powering the MVP flow. Upload requests immediately persist the
incoming bytes to S3 and forward the same payload to OpenAI's Files API so the
extraction pipeline can reference them without re-uploading. The form-fill
control plane remains stubbed (random statuses) until that piece of the pipeline
is implemented.

## Getting Started

```bash
cd backend/pdf-form-filling-service
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

- Default server runs on `http://127.0.0.1:8000`.
- CORS is wide open for now so the frontend can call the API from any origin.
- AWS credentials are read via the standard SDK chain (env vars, shared config,
  or IAM roles if deployed).

## Environment Configuration
- `S3_BUCKET_NAME` – bucket used to store uploads.
- `S3_BUCKET_REGION` – optional region hint for the S3 client.
- `S3_BUCKET_URL` – public base URL returned to the frontend for `View on S3`.
- `OPENAI_API_KEY` – required for forwarding uploads to OpenAI Files.
- `OPENAI_FILE_PURPOSE` – purpose passed to OpenAI (default `assistants`).
- `INFORMATION_EXTRACTION_MODEL` – OpenAI Responses model used for structured extraction (default `gpt-4.1-mini`).
- `FORM_FILL_MODEL` – OpenAI Chat Completions model that evaluates each form field (default `gpt-4o-mini`).
- `FORM_FILL_MAX_CONCURRENCY` – concurrent OpenAI prompts while filling (default `4`).
- Optional overrides: `OPENAI_API_BASE`, `OPENAI_ORG_ID`.
- Add any other AWS/OpenAI environment variables (profiles, endpoints, etc.) as
  needed; the service will pick them up automatically.

## API Surface (MVP)

### `POST /api/uploads`
Multipart body with `userId` and a single `file`. The
server copies the bytes to `s3://<bucket>/<userId>/<slug>/value.ext`, then sends
that same payload to OpenAI. Once the file exists in OpenAI Files the service
invokes the information-extraction prompt (see `openai/information_extraction`)
with structured outputs and uploads the resulting JSON to
`s3://<bucket>/<userId>/<slug>/info.json`. Success responses look like:

> Note: if a user uploads a non-PDF (PNG, JPG, etc.), the backend temporarily
> converts a PDF surrogate for extraction only. The original bytes still land in
> S3 and have a matching OpenAI file ID recorded in the manifest.

```json
{
  "status": "extracted",
  "slug": "supporting-doc",
  "fileName": "supporting-doc.pdf",
  "s3Url": "https://<bucket>.s3.amazonaws.com/user_id/supporting-doc/value.pdf",
  "openaiFileId": "file-abc123",
  "size": 123456
}
```

If either upload fails, the request returns `502`.

### `GET /api/uploads`
Query param `userId` returns the persisted manifest for that session so the
frontend can restore the file list after reloads. Response shape:

```json
{
  "updatedAt": "2026-01-07T12:34:56Z",
  "files": [
    {
      "status": "extracted",
      "slug": "supporting-doc",
      "fileName": "supporting-doc.pdf",
      "s3Url": "https://<bucket>.s3.amazonaws.com/user_id/supporting-doc/value.pdf",
      "openaiFileId": "file-abc123",
      "size": 123456
    }
  ]
}
```

### `DELETE /api/uploads/{slug}`
Query param `userId` identifies the visitor namespace. Removes the stored file
payload (`value.ext`) plus the derived `info.json` from S3 and deletes the
associated OpenAI file ID recorded in the user manifest. Responds with
`{ status: "deleted" | "missing", slug }`.

### `POST /api/form-fill`
JSON body `{ "userId": string, "formUrl": string }`. Launches the real filling
pipeline:

1. Downloads the blank form, stores it under `user_id/forms/<slug>/source.pdf`,
   and extracts text widgets with PyMuPDF (`schema.json`).
2. Combines every upload’s `info.json` facts into a context bundle.
3. Prompts OpenAI once per text field (bounded by `FORM_FILL_MAX_CONCURRENCY`)
   using the template in `openai/form_filling/`.
4. Applies the approved values to the PDF and uploads
   `user_id/forms/<slug>/filled.pdf`.

Response payload:

```json
{
  "jobId": "job-123",
  "status": "filling",
  "totalFields": 18,
  "filledFields": 4,
  "skippedFields": 2,
  "errorFields": 0,
  "fields": [
    { "fieldName": "GivenName", "status": "filled", "value": "Ada", "confidence": 0.93 },
    { "fieldName": "FamilyName", "status": "filled", "value": "Lovelace", "confidence": 0.91 }
  ]
}
```

`filledFormUrl` appears once the job reaches `complete`.

### `GET /api/form-fill/{jobId}`
Still accepts `userId` / `formUrl` query params for compatibility. Returns the
same payload as the POST response so the frontend can poll field-level progress.

### `GET /healthz`
Simple readiness check that returns `{ "ok": true }`.

## Notes
- Uploaded file bytes are read once and shared between S3 + OpenAI, matching the
  design doc requirement to avoid duplicate uploads.
- Returned S3 URLs assume the bucket is public (or at least readable by the
  caller). Adjust to presigned URLs later if bucket policies change.
- Per-user manifests live at `user_id/manifest.json` and back the upload list
  endpoint so the frontend can restore prior state if the page reloads.
- Form-fill endpoints now run the full pipeline end-to-end. Text widgets only
  are supported for this MVP; checkbox / radio handling can be layered on later.
