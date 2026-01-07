"""Shared helper utilities."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from fastapi import HTTPException, status


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


def now_iso() -> str:
    return dt.datetime.now(tz=dt.timezone.utc).isoformat()


def build_object_key(user_id: str, slug: str, extension: str) -> str:
    ext = extension if extension.startswith(".") else f".{extension}" if extension else ""
    return f"{sanitize_user_id(user_id)}/{slug}/value{ext}"


def build_info_key(user_id: str, slug: str) -> str:
    return f"{sanitize_user_id(user_id)}/{slug}/info.json"


def build_manifest_key(user_id: str, filename: str) -> str:
    return f"{sanitize_user_id(user_id)}/{filename}"


def build_s3_url(base_url: str, key: str) -> str:
    return f"{base_url.rstrip('/')}/{key}"


def build_filled_form_url(base_url: str, user_id: str, form_slug: str) -> str:
    return f"{base_url.rstrip('/')}/{sanitize_user_id(user_id)}/forms/{form_slug}/filled.pdf"


def extension_from_name(file_name: str | None) -> str:
    path = Path(file_name or "")
    return path.suffix or ".pdf"


def build_form_source_key(user_id: str, form_slug: str) -> str:
    return f"{sanitize_user_id(user_id)}/forms/{form_slug}/source.pdf"


def build_form_schema_key(user_id: str, form_slug: str) -> str:
    return f"{sanitize_user_id(user_id)}/forms/{form_slug}/schema.json"


def build_form_filled_key(user_id: str, form_slug: str) -> str:
    return f"{sanitize_user_id(user_id)}/forms/{form_slug}/filled.pdf"
