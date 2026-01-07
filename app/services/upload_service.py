"""Business logic for upload endpoints."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from boto3.exceptions import Boto3Error
from fastapi import HTTPException, UploadFile, status

from ..config import get_settings
from ..schemas import Manifest, ManifestFileEntry, UploadResponse
from ..services.openai_service import delete_openai_file, upload_bytes_to_openai
from ..services.storage_service import (
    delete_s3_object,
    find_manifest_entry,
    load_manifest,
    remove_manifest_entry,
    save_manifest,
    upsert_manifest_entry,
    upload_bytes_to_s3,
)
from ..utils import (
    build_info_key,
    build_object_key,
    build_s3_url,
    ensure_safe_slug,
    extension_from_name,
    now_iso,
    slugify,
)

logger = logging.getLogger(__name__)


async def _persist_manifest(user_id: str, manifest: Manifest) -> None:
    await save_manifest(user_id, manifest)


async def handle_upload(user_id: str, file: UploadFile) -> UploadResponse:
    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty.")

    file_path = Path(file.filename or "document.pdf")
    slug = slugify(file_path.stem)
    extension = file_path.suffix or ".pdf"
    settings = get_settings()
    object_key = build_object_key(user_id, slug, extension)
    s3_url = build_s3_url(settings.s3_bucket_url, object_key)
    content_type = file.content_type or "application/octet-stream"

    await upload_bytes_to_s3(object_key, payload, content_type)

    openai_file_id: str | None = None
    try:
        filename = file.filename or f"{slug}{extension}"
        openai_file_id = await upload_bytes_to_openai(filename, payload)
    except Exception as exc:  # pragma: no cover - network path
        logger.exception("Failed to upload %s to OpenAI", file.filename)
        await delete_s3_object(object_key)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to register file with OpenAI.",
        ) from exc

    manifest_entry = ManifestFileEntry(
        slug=slug,
        objectKey=object_key,
        infoKey=build_info_key(user_id, slug),
        fileName=file.filename or "document.pdf",
        s3Url=s3_url,
        contentType=content_type,
        openaiFileId=openai_file_id,
        uploadedAt=now_iso(),
        size=len(payload),
        status="uploaded",
    )

    manifest = await load_manifest(user_id)
    manifest = upsert_manifest_entry(manifest, manifest_entry)

    try:
        await _persist_manifest(user_id, manifest)
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
        fileName=manifest_entry.fileName,
        openaiFileId=openai_file_id,
        size=len(payload),
    )


async def list_uploads(user_id: str) -> Manifest:
    return await load_manifest(user_id)


async def delete_upload(user_id: str, slug: str) -> bool:
    ensure_safe_slug(slug)
    manifest = await load_manifest(user_id)
    entry = find_manifest_entry(manifest, slug)

    if entry is None:
        return False

    object_key = entry.objectKey or build_object_key(user_id, slug, extension_from_name(entry.fileName))
    info_key = entry.infoKey or build_info_key(user_id, slug)

    await delete_s3_object(object_key, raise_on_error=True)
    await delete_s3_object(info_key, raise_on_error=True)

    if entry.openaiFileId:
        await delete_openai_file(entry.openaiFileId)

    manifest = remove_manifest_entry(manifest, slug)
    await save_manifest(user_id, manifest)
    return True
