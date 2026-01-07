"""OpenAI Files API helpers."""

from __future__ import annotations

import asyncio
import io
import logging

from fastapi import HTTPException, status
from openai import OpenAI

from ..config import get_settings

logger = logging.getLogger(__name__)

_client: OpenAI | None = None


def get_openai_client() -> OpenAI:
    global _client
    if _client is None:
        settings = get_settings()
        if not settings.openai_api_key:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Server missing required configuration: OPENAI_API_KEY",
            )

        client_kwargs: dict[str, str] = {"api_key": settings.openai_api_key}
        if settings.openai_api_base:
            client_kwargs["base_url"] = settings.openai_api_base
        if settings.openai_org_id:
            client_kwargs["organization"] = settings.openai_org_id

        _client = OpenAI(**client_kwargs)
    return _client


async def upload_bytes_to_openai(filename: str, payload: bytes) -> str:
    settings = get_settings()
    client = get_openai_client()

    def _upload():
        file_obj = io.BytesIO(payload)
        return client.files.create(file=(filename, file_obj), purpose=settings.openai_file_purpose)

    response = await asyncio.to_thread(_upload)
    return response.id


async def delete_openai_file(file_id: str) -> None:
    client = get_openai_client()

    def _delete():
        try:
            client.files.delete(file_id)
        except Exception as exc:  # pragma: no cover - network path
            status_code = getattr(exc, "status_code", None)
            if status_code == 404:
                return
            raise

    try:
        await asyncio.to_thread(_delete)
    except Exception as exc:  # pragma: no cover - network path
        logger.warning("Failed to delete OpenAI file %s: %s", file_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to delete file from OpenAI.",
        ) from exc
