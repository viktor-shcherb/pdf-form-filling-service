"""OpenAI helper that decides how to fill individual form fields."""

from __future__ import annotations

import asyncio
import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status
from jinja2 import Template
from pydantic import ValidationError

from ..config import get_settings
from ..schemas import FieldFillDecision, FormFieldSchema
from ..services.openai_service import get_openai_client

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parents[2]
_PROMPT_PATH = _BASE_DIR / "openai" / "form_filling" / "prompt.jinja2"
_OUTPUT_SCHEMA_PATH = _BASE_DIR / "openai" / "form_filling" / "output_schema.json"


@lru_cache
def _prompt_template() -> Template:
    try:
        contents = _PROMPT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError as exc:  # pragma: no cover - configuration path
        raise RuntimeError("form filling prompt template is missing.") from exc

    if not contents.strip():
        raise RuntimeError("form filling prompt template cannot be empty.")
    return Template(contents, trim_blocks=True, lstrip_blocks=True)


@lru_cache
def _response_format() -> dict[str, Any]:
    try:
        payload = json.loads(_OUTPUT_SCHEMA_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:  # pragma: no cover - configuration path
        raise RuntimeError("form filling output_schema.json is missing.") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("form filling output_schema.json is invalid JSON.") from exc
    return payload


def _render_prompt(field: FormFieldSchema, document_description: str, facts_text: str) -> str:
    template = _prompt_template()
    rendered = template.render(
        field=field.model_dump(),
        document_description=document_description or "No document summary available.",
        facts_text=facts_text or "No supporting facts provided.",
    ).strip()
    return rendered or "You are a meticulous form assistant."


def _response_text(response: Any) -> str | None:
    choices = getattr(response, "choices", None)
    if choices:
        for choice in choices:
            message = getattr(choice, "message", None)
            if not message:
                continue
            content = getattr(message, "content", None)
            if isinstance(content, str) and content.strip():
                return content.strip()
            if isinstance(content, list):
                for part in content:
                    text_value = getattr(part, "text", None)
                    if isinstance(text_value, str) and text_value.strip():
                        return text_value.strip()
    try:
        data = response.model_dump()  # type: ignore[attr-defined]
    except AttributeError:  # pragma: no cover - defensive fallback
        return None

    for choice in data.get("choices", []):
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            for part in content:
                text_value = part.get("text") if isinstance(part, dict) else None
                if isinstance(text_value, str) and text_value.strip():
                    return text_value.strip()
    return None


async def decide_field_value(
    field: FormFieldSchema,
    *,
    document_description: str,
    facts_text: str,
) -> FieldFillDecision:
    settings = get_settings()
    if not settings.form_fill_model:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server missing configuration: FORM_FILL_MODEL",
        )

    prompt = _render_prompt(field, document_description, facts_text)
    client = get_openai_client()
    response_format = _response_format()

    def _create_response():
        logger.info(
            "Prompting field %s with model=%s",
            field.name,
            settings.form_fill_model,
        )
        return client.chat.completions.create(
            model=settings.form_fill_model,
            messages=[  # noqa
                {
                    "role": "developer",
                    "content": prompt,
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Return your decision for this form field.",
                        }
                    ],
                },
            ],
            response_format=response_format,  # noqa
        )

    try:
        response = await asyncio.to_thread(_create_response)
    except Exception as exc:  # pragma: no cover - network path
        logger.exception("OpenAI field decision failed for %s", field.name)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to decide form field value.",
        ) from exc

    text_payload = _response_text(response)
    if not text_payload:
        logger.error("OpenAI field decision returned empty output for %s", field.name)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Field decision response was empty.",
        )

    try:
        return FieldFillDecision.model_validate_json(text_payload)
    except ValidationError as exc:
        logger.exception("OpenAI field decision invalid for %s", field.name)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Field decision response was invalid.",
        ) from exc
