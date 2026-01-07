"""FastAPI service for the PDF form-filling MVP.

Uploads flow straight to S3 and OpenAI's Files API so later extraction/filling
steps can reference the raw assets without re-uploading.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
import logging
import os
import random
import uuid
from pathlib import Path
from typing import Any, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI
from pydantic import BaseModel, HttpUrl

load_dotenv()

logger = logging.getLogger(__name__)

S3_BUCKET_URL = os.getenv("S3_BUCKET_URL", "https://s3.amazonaws.com/mvp-form-fill").rstrip("/")
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME")
S3_BUCKET_REGION = os.getenv("S3_BUCKET_REGION")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_API_BASE = os.getenv("OPENAI_API_BASE")
OPENAI_ORG_ID = os.getenv("OPENAI_ORG_ID")
OPENAI_FILE_PURPOSE = os.getenv("OPENAI_FILE_PURPOSE", "assistants")
MANIFEST_FILENAME = "manifest.json"

_s3_client: Optional[Any] = None
_openai_client: Optional[OpenAI] = None

app = FastAPI(title="PDF Form Filling Service", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def ensure_setting(value: Optional[str], name: str) -> str:
    if not value:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Server missing required configuration: {name}",
        )
    return value


def get_s3_client():
    global _s3_client
    if _s3_client is None:
        session_kwargs = {"region_name": S3_BUCKET_REGION} if S3_BUCKET_REGION else {}
        _s3_client = boto3.client("s3", **session_kwargs)
    return _s3_client


def get_openai_client() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        ensure_setting(OPENAI_API_KEY, "OPENAI_API_KEY")
        client_kwargs: dict[str, str] = {"api_key": OPENAI_API_KEY}
        if OPENAI_API_BASE:
            client_kwargs["base_url"] = OPENAI_API_BASE
        if OPENAI_ORG_ID:
            client_kwargs["organization"] = OPENAI_ORG_ID
        _openai_client = OpenAI(**client_kwargs)
    return _openai_client

FILL_STATUS_WEIGHTS = (
    ("queued", 0.25),
    ("filling", 0.25),
    ("complete", 0.35),
    ("error", 0.15),
)


class UploadResponse(BaseModel):
    status: str
    slug: str
    s3Url: str
    fileName: str
    openaiFileId: str | None = None
    size: int | None = None


class DeleteResponse(BaseModel):
    status: str
    slug: str


class UploadListResponse(BaseModel):
    files: list[UploadResponse]
    updatedAt: str


class FormFillRequest(BaseModel):
    userId: str
    formUrl: HttpUrl


class FormFillResponse(BaseModel):
    jobId: str
    status: str
    filledFormUrl: str | None = None


async def simulate_latency(min_seconds: float = 0.5, max_seconds: float = 1.7) -> None:
    """Async nap to mimic network + processing time."""

    await asyncio.sleep(random.uniform(min_seconds, max_seconds))


def weighted_status(weights: tuple[tuple[str, float], ...]) -> str:
    labels, probabilities = zip(*weights)
    return random.choices(population=labels, weights=probabilities, k=1)[0]


def slugify(value: str | None) -> str:
    base = (value or "document").strip().lower()
    safe = "".join(ch if ch.isalnum() else "-" for ch in base)
    safe = "-".join(part for part in safe.split("-") if part)
    return safe or "document"


def sanitize_user_id(user_id: str | None) -> str:
    value = (user_id or "user").strip()
    cleaned = "".join(ch for ch in value if ch.isalnum() or ch in {"-", "_"})
    return cleaned or "user"


def ensure_safe_slug(slug: str) -> str:
    if not slug or any(ch in slug for ch in {"/", "\\"}):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid file slug.")
    return slug


def _now_iso() -> str:
    return dt.datetime.now(tz=dt.timezone.utc).isoformat()


def build_object_key(user_id: str, slug: str, extension: str) -> str:
    ext = extension if extension.startswith(".") else f".{extension}" if extension else ""
    return f"{sanitize_user_id(user_id)}/{slug}/value{ext}"


def build_info_key(user_id: str, slug: str) -> str:
    return f"{sanitize_user_id(user_id)}/{slug}/info.json"


def build_s3_url_from_key(key: str) -> str:
    return f"{S3_BUCKET_URL}/{key}"


def build_filled_form_url(user_id: str, form_url: HttpUrl) -> str:
    slug = slugify(str(form_url)) or "target-form"
    return f"{S3_BUCKET_URL}/{sanitize_user_id(user_id)}/forms/{slug}/filled.pdf"


def build_manifest_key(user_id: str) -> str:
    return f"{sanitize_user_id(user_id)}/{MANIFEST_FILENAME}"


def _default_manifest(user_id: str) -> dict[str, Any]:
    return {
        "userId": sanitize_user_id(user_id),
        "files": [],
        "forms": {},
        "updatedAt": _now_iso(),
    }


def _normalize_manifest(user_id: str, data: dict[str, Any] | None) -> dict[str, Any]:
    manifest = dict(data or {})
    manifest["userId"] = sanitize_user_id(manifest.get("userId") or user_id)

    files = manifest.get("files")
    if isinstance(files, list):
        manifest["files"] = [entry for entry in files if isinstance(entry, dict)]
    elif isinstance(files, dict):
        converted: list[dict[str, Any]] = []
        for slug, entry in files.items():
            if not isinstance(entry, dict):
                continue
            next_entry = {"slug": slug, **entry}
            converted.append(next_entry)
        manifest["files"] = converted
    else:
        manifest["files"] = []

    forms = manifest.get("forms")
    manifest["forms"] = forms if isinstance(forms, dict) else {}

    manifest["updatedAt"] = manifest.get("updatedAt") or _now_iso()
    return manifest


async def load_user_manifest(user_id: str) -> dict[str, Any]:
    bucket = ensure_setting(S3_BUCKET_NAME, "S3_BUCKET_NAME")
    client = get_s3_client()
    key = build_manifest_key(user_id)

    def _load_manifest():
        try:
            obj = client.get_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code")
            if error_code in {"NoSuchKey", "404"}:
                return _default_manifest(user_id)
            raise

        body = obj["Body"]
        try:
            payload = body.read()
        finally:
            body.close()

        if not payload:
            return _default_manifest(user_id)

        try:
            data = json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Stored manifest is invalid JSON.",
            ) from exc

        if not isinstance(data, dict):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Stored manifest has unexpected structure.",
            )

        return _normalize_manifest(user_id, data)

    try:
        return await asyncio.to_thread(_load_manifest)
    except HTTPException:
        raise
    except (ClientError, BotoCoreError) as exc:
        logger.exception("Failed to load manifest for user %s", user_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Unable to read manifest from storage.",
        ) from exc


async def save_user_manifest(user_id: str, manifest: dict[str, Any]) -> None:
    bucket = ensure_setting(S3_BUCKET_NAME, "S3_BUCKET_NAME")
    client = get_s3_client()
    key = build_manifest_key(user_id)
    normalized = _normalize_manifest(user_id, manifest)
    normalized["updatedAt"] = _now_iso()
    payload = json.dumps(normalized, separators=(",", ":"), sort_keys=True).encode("utf-8")

    def _put_manifest():
        client.put_object(Bucket=bucket, Key=key, Body=payload, ContentType="application/json")

    try:
        await asyncio.to_thread(_put_manifest)
    except (ClientError, BotoCoreError) as exc:
        logger.exception("Failed to write manifest for user %s", user_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Unable to update manifest.",
        ) from exc


async def upsert_manifest_file_entry(user_id: str, slug: str, entry: dict[str, Any]) -> None:
    manifest = await load_user_manifest(user_id)
    files: list[dict[str, Any]] = manifest.setdefault("files", [])
    stored_entry = {"slug": slug, **entry}
    for index, existing in enumerate(files):
        if existing.get("slug") == slug:
            files[index] = stored_entry
            break
    else:
        files.append(stored_entry)
    await save_user_manifest(user_id, manifest)


def resolve_object_key_from_manifest_entry(user_id: str, slug: str, entry: dict[str, Any]) -> str:
    key = entry.get("objectKey")
    if key:
        return key
    file_name = entry.get("fileName") or ""
    extension = Path(file_name).suffix
    return build_object_key(user_id, slug, extension)


def get_manifest_file_entry(manifest: dict[str, Any], slug: str) -> dict[str, Any] | None:
    for entry in manifest.get("files", []):
        if entry.get("slug") == slug:
            return entry
    return None


def remove_manifest_file_entry(manifest: dict[str, Any], slug: str) -> None:
    files = manifest.get("files", [])
    manifest["files"] = [entry for entry in files if entry.get("slug") != slug]


def _create_openai_file(filename: str, payload: bytes):
    client = get_openai_client()
    file_obj = io.BytesIO(payload)
    return client.files.create(file=(filename, file_obj), purpose=OPENAI_FILE_PURPOSE)


async def delete_openai_file(file_id: str) -> None:
    client = get_openai_client()

    def _delete():
        try:
            client.files.delete(file_id)
        except Exception as exc:  # pragma: no cover - network error path
            status_code = getattr(exc, "status_code", None)
            if status_code == 404:
                return
            raise

    try:
        await asyncio.to_thread(_delete)
    except Exception as exc:  # pragma: no cover - network error path
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to delete file from OpenAI.",
        ) from exc


async def upload_bytes_to_openai(filename: str, payload: bytes) -> str:
    response = await asyncio.to_thread(_create_openai_file, filename, payload)
    return response.id


async def upload_bytes_to_s3(key: str, payload: bytes, content_type: str) -> None:
    bucket = ensure_setting(S3_BUCKET_NAME, "S3_BUCKET_NAME")
    client = get_s3_client()
    await asyncio.to_thread(
        client.put_object,
        Bucket=bucket,
        Key=key,
        Body=payload,
        ContentType=content_type,
    )


async def delete_s3_object(key: str, *, raise_on_error: bool = False) -> None:
    bucket = ensure_setting(S3_BUCKET_NAME, "S3_BUCKET_NAME")
    client = get_s3_client()
    try:
        await asyncio.to_thread(client.delete_object, Bucket=bucket, Key=key)
    except (ClientError, BotoCoreError) as exc:  # pragma: no cover - best effort cleanup
        logger.warning("Failed to delete S3 object %s: %s", key, exc)
        if raise_on_error:
            raise


@app.post("/api/uploads", response_model=UploadResponse)
async def upload_file(
    user_id: str = Form(..., alias="userId"),
    file: UploadFile = File(...),
):
    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty.")
    payload_size = len(payload)

    file_path = Path(file.filename or "document.pdf")
    slug = slugify(file_path.stem)
    extension = file_path.suffix or ".pdf"
    object_key = build_object_key(user_id, slug, extension)
    s3_url = build_s3_url_from_key(object_key)
    content_type = file.content_type or "application/octet-stream"

    try:
        await upload_bytes_to_s3(object_key, payload, content_type)
    except (ClientError, BotoCoreError) as exc:
        logger.exception("Failed to upload %s to S3", object_key)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to upload file to storage.",
        ) from exc

    openai_file_id: str | None = None
    try:
        filename = file.filename or f"{slug}{extension}"
        openai_file_id = await upload_bytes_to_openai(filename, payload)
    except Exception as exc:  # pragma: no cover - network error path
        logger.exception("Failed to upload %s to OpenAI", file.filename)
        await delete_s3_object(object_key)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to register file with OpenAI.",
        ) from exc

    manifest_entry = {
        "slug": slug,
        "objectKey": object_key,
        "infoKey": build_info_key(user_id, slug),
        "fileName": file.filename or "document.pdf",
        "s3Url": s3_url,
        "contentType": content_type,
        "openaiFileId": openai_file_id,
        "uploadedAt": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "size": payload_size,
        "status": "uploaded",
    }
    try:
        await upsert_manifest_file_entry(user_id, slug, manifest_entry)
    except HTTPException:
        await delete_s3_object(object_key)
        if openai_file_id:
            try:
                await delete_openai_file(openai_file_id)
            except HTTPException:
                logger.warning("Failed to roll back OpenAI file %s after manifest error", openai_file_id)
        raise

    return UploadResponse(
        status="uploaded",
        slug=slug,
        s3Url=s3_url,
        fileName=file.filename or "document.pdf",
        openaiFileId=openai_file_id,
        size=payload_size,
    )


@app.get("/api/uploads", response_model=UploadListResponse)
async def list_uploads(user_id: str = Query(..., alias="userId")):
    manifest = await load_user_manifest(user_id)
    files: list[UploadResponse] = []
    for entry in manifest.get("files", []):
        slug = entry.get("slug")
        if not slug:
            continue
        object_key = entry.get("objectKey")
        s3_url = entry.get("s3Url") or (build_s3_url_from_key(object_key) if object_key else "")
        files.append(
            UploadResponse(
                status=entry.get("status") or "uploaded",
                slug=slug,
                s3Url=s3_url,
                fileName=entry.get("fileName") or slug,
                openaiFileId=entry.get("openaiFileId"),
                size=entry.get("size"),
            )
        )

    return UploadListResponse(files=files, updatedAt=manifest.get("updatedAt", _now_iso()))


@app.delete("/api/uploads/{slug}", response_model=DeleteResponse)
async def delete_file(slug: str, user_id: str = Query(..., alias="userId")):
    ensure_safe_slug(slug)
    manifest = await load_user_manifest(user_id)
    entry = get_manifest_file_entry(manifest, slug)

    if entry is None:
        return DeleteResponse(status="missing", slug=slug)

    object_key = resolve_object_key_from_manifest_entry(user_id, slug, entry)
    info_key = entry.get("infoKey") or build_info_key(user_id, slug)

    try:
        await delete_s3_object(object_key, raise_on_error=True)
        await delete_s3_object(info_key, raise_on_error=True)
    except (ClientError, BotoCoreError) as exc:
        logger.exception("Failed to delete S3 objects for %s/%s", user_id, slug)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to delete file from storage.",
        ) from exc

    openai_file_id = entry.get("openaiFileId")
    if openai_file_id:
        await delete_openai_file(openai_file_id)

    remove_manifest_file_entry(manifest, slug)
    await save_user_manifest(user_id, manifest)

    return DeleteResponse(status="deleted", slug=slug)


@app.post("/api/form-fill", response_model=FormFillResponse)
async def start_form_fill(request: FormFillRequest):
    await simulate_latency()
    job_id = str(uuid.uuid4())
    status = weighted_status(FILL_STATUS_WEIGHTS)
    filled_url = build_filled_form_url(request.userId, request.formUrl) if status == "complete" else None
    return FormFillResponse(jobId=job_id, status=status, filledFormUrl=filled_url)


@app.get("/api/form-fill/{job_id}", response_model=FormFillResponse)
async def poll_form_fill(
    job_id: str,
    user_id: str = Query(..., alias="userId"),
    form_url: HttpUrl = Query(..., alias="formUrl"),
):
    await simulate_latency(0.25, 1.0)
    status = weighted_status(FILL_STATUS_WEIGHTS)
    filled_url = build_filled_form_url(user_id, form_url) if status == "complete" else None
    return FormFillResponse(jobId=job_id, status=status, filledFormUrl=filled_url)


@app.get("/healthz")
async def healthcheck():
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
