"""Business logic for upload endpoints."""

from __future__ import annotations

import logging
from pathlib import Path

import fitz  # type: ignore
from fastapi import HTTPException, UploadFile, status

from ..config import get_settings
from ..schemas import Manifest, ManifestFileEntry, UploadResponse
from ..services.information_extraction_service import extract_document_information
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
    sanitize_user_id,
    slugify,
)

logger = logging.getLogger(__name__)


async def _persist_manifest(user_id: str, manifest: Manifest) -> None:
    await save_manifest(user_id, manifest)


async def _cleanup_failed_upload(
    object_key: str,
    info_key: str,
    openai_file_id: str | None,
    extra_openai_file_ids: list[str] | None = None,
) -> None:
    await delete_s3_object(object_key)
    await delete_s3_object(info_key)
    ids = [openai_file_id] + (extra_openai_file_ids or [])
    for oid in ids:
        if not oid:
            continue
        try:
            await delete_openai_file(oid)
        except HTTPException:
            logger.warning("Failed to roll back OpenAI file %s during upload cleanup", oid)


def _needs_pdf_conversion(content_type: str | None, file_name: str | None) -> bool:
    if content_type and content_type.lower() == "application/pdf":
        return False
    suffix = (Path(file_name or "").suffix or "").lower()
    return suffix != ".pdf"


def _convert_document_to_pdf(payload: bytes, file_name: str | None) -> bytes:
    suffix = (Path(file_name or "").suffix or "").lower().lstrip(".") or "png"
    try:
        doc = fitz.open(stream=payload, filetype=suffix)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Unsupported document format for extraction.",
        ) from exc
    try:
        return doc.convert_to_pdf()
    finally:
        doc.close()


async def _delete_temp_openai_files(file_ids: list[str]) -> None:
    for file_id in file_ids:
        try:
            await delete_openai_file(file_id)
        except HTTPException:
            logger.warning("Failed to remove temporary OpenAI file %s", file_id)


async def handle_upload(user_id: str, file: UploadFile) -> UploadResponse:
    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty.")

    file_path = Path(file.filename or "document.pdf")
    slug = slugify(file_path.stem)
    extension = file_path.suffix or ".pdf"
    logger.info(
        "Starting upload for user=%s slug=%s name=%s size=%d bytes",
        sanitize_user_id(user_id),
        slug,
        file.filename,
        len(payload),
    )
    settings = get_settings()
    object_key = build_object_key(user_id, slug, extension)
    s3_url = build_s3_url(settings.s3_bucket_url, object_key)
    content_type = file.content_type or "application/octet-stream"
    info_key = build_info_key(user_id, slug)

    await upload_bytes_to_s3(object_key, payload, content_type)

    openai_file_id: str | None = None
    temp_openai_files: list[str] = []
    try:
        filename = file.filename or f"{slug}{extension}"
        openai_file_id = await upload_bytes_to_openai(filename, payload)
        logger.info("Uploaded slug=%s to OpenAI file_id=%s", slug, openai_file_id)
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
        infoKey=info_key,
        fileName=file.filename or "document.pdf",
        s3Url=s3_url,
        contentType=content_type,
        openaiFileId=openai_file_id,
        uploadedAt=now_iso(),
        size=len(payload),
        status="uploaded",
    )

    if not openai_file_id:  # pragma: no cover - defensive
        await _cleanup_failed_upload(object_key, info_key, None)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to capture OpenAI file reference for extraction.",
        )

    extraction_file_id = openai_file_id
    if _needs_pdf_conversion(content_type, file.filename):
        try:
            converted_pdf = _convert_document_to_pdf(payload, file.filename)
            extraction_file_id = await upload_bytes_to_openai(f"{slug}-converted.pdf", converted_pdf)
            temp_openai_files.append(extraction_file_id)
            logger.info("Converted %s to PDF for extraction (%s)", file.filename, extraction_file_id)
        except HTTPException:
            await _cleanup_failed_upload(object_key, info_key, openai_file_id, temp_openai_files)
            temp_openai_files.clear()
            raise
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Failed to convert %s to PDF for extraction", file.filename)
            await _cleanup_failed_upload(object_key, info_key, openai_file_id, temp_openai_files)
            temp_openai_files.clear()
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail="Unable to process this document format for extraction.",
            ) from exc

    try:
        extraction = await extract_document_information(extraction_file_id, file_name=file.filename)
        info_payload = extraction.model_dump_json(indent=2).encode("utf-8")
        await upload_bytes_to_s3(info_key, info_payload, "application/json")
        manifest_entry.status = "extracted"
        logger.info(
            "Extraction complete for slug=%s facts=%d description_len=%d",
            slug,
            len(extraction.structured_information),
            len(extraction.document_description),
        )
    except HTTPException:
        await _cleanup_failed_upload(object_key, info_key, openai_file_id, temp_openai_files)
        temp_openai_files.clear()
        raise
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Failed to persist extracted information for %s", slug)
        await _cleanup_failed_upload(object_key, info_key, openai_file_id, temp_openai_files)
        temp_openai_files.clear()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to persist extracted information.",
        ) from exc
    finally:
        if temp_openai_files:
            await _delete_temp_openai_files(temp_openai_files)

    manifest = await load_manifest(user_id)
    manifest = upsert_manifest_entry(manifest, manifest_entry)

    try:
        await _persist_manifest(user_id, manifest)
    except HTTPException:
        await _cleanup_failed_upload(object_key, info_key, openai_file_id, temp_openai_files)
        temp_openai_files.clear()
        raise

    return UploadResponse(
        status=manifest_entry.status or "uploaded",
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
