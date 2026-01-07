"""S3 storage + manifest helpers."""

from __future__ import annotations

import asyncio
import json
import logging

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException, status

from ..config import get_settings
from ..schemas import Manifest, ManifestFileEntry
from ..utils import build_manifest_key, now_iso, sanitize_user_id

logger = logging.getLogger(__name__)

_s3_client = None


def get_s3_client():
    global _s3_client
    if _s3_client is None:
        settings = get_settings()
        session_kwargs = {"region_name": settings.s3_bucket_region} if settings.s3_bucket_region else {}
        _s3_client = boto3.client("s3", **session_kwargs)
    return _s3_client


def _require_bucket_name() -> str:
    bucket = get_settings().s3_bucket_name
    if not bucket:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server missing required configuration: S3_BUCKET_NAME",
        )
    return bucket


async def upload_bytes_to_s3(key: str, payload: bytes, content_type: str) -> None:
    bucket = _require_bucket_name()
    client = get_s3_client()
    try:
        await asyncio.to_thread(
            client.put_object,
            Bucket=bucket,
            Key=key,
            Body=payload,
            ContentType=content_type,
        )
    except (ClientError, BotoCoreError) as exc:
        logger.exception("Failed to upload %s to S3", key)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to upload file to storage.",
        ) from exc


async def delete_s3_object(key: str, *, raise_on_error: bool = False) -> None:
    bucket = _require_bucket_name()
    client = get_s3_client()
    try:
        await asyncio.to_thread(client.delete_object, Bucket=bucket, Key=key)
    except (ClientError, BotoCoreError) as exc:
        if raise_on_error:
            logger.exception("Failed to delete S3 object %s", key)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to delete file from storage.",
            ) from exc
        logger.warning("Failed to delete S3 object %s: %s", key, exc)


def _default_manifest(user_id: str) -> Manifest:
    return Manifest(
        userId=sanitize_user_id(user_id),
        updatedAt=now_iso(),
        files=[],
        forms={},
    )


async def load_manifest(user_id: str) -> Manifest:
    bucket = _require_bucket_name()
    client = get_s3_client()
    key = build_manifest_key(user_id, get_settings().manifest_filename)

    def _load():
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

        return Manifest.model_validate(data)

    try:
        return await asyncio.to_thread(_load)
    except HTTPException:
        raise
    except (ClientError, BotoCoreError) as exc:
        logger.exception("Failed to load manifest for user %s", user_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Unable to read manifest from storage.",
        ) from exc


async def save_manifest(user_id: str, manifest: Manifest) -> None:
    bucket = _require_bucket_name()
    client = get_s3_client()
    key = build_manifest_key(user_id, get_settings().manifest_filename)
    manifest.userId = sanitize_user_id(user_id)
    manifest.updatedAt = now_iso()
    payload = json.dumps(manifest.model_dump(), separators=(",", ":"), sort_keys=True).encode("utf-8")

    def _persist():
        client.put_object(Bucket=bucket, Key=key, Body=payload, ContentType="application/json")

    try:
        await asyncio.to_thread(_persist)
    except (ClientError, BotoCoreError) as exc:
        logger.exception("Failed to write manifest for user %s", user_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Unable to update manifest.",
        ) from exc


def upsert_manifest_entry(manifest: Manifest, entry: ManifestFileEntry) -> Manifest:
    manifest.files = [existing for existing in manifest.files if existing.slug != entry.slug]
    manifest.files.append(entry)
    return manifest


def remove_manifest_entry(manifest: Manifest, slug: str) -> Manifest:
    manifest.files = [existing for existing in manifest.files if existing.slug != slug]
    return manifest


def find_manifest_entry(manifest: Manifest, slug: str) -> ManifestFileEntry | None:
    for entry in manifest.files:
        if entry.slug == slug:
            return entry
    return None
