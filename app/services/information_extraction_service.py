"""Information extraction helpers powered by OpenAI structured outputs."""

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
from ..schemas import InformationExtractionResult
from ..services.openai_service import get_openai_client

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parents[2]
_PROMPT_PATH = _BASE_DIR / "openai" / "information_extraction" / "prompt.jinja2"
_OUTPUT_SCHEMA_PATH = _BASE_DIR / "openai" / "information_extraction" / "output_schema.json"


def _validate_output_schema(payload: dict[str, Any]) -> None:
    if payload.get("type") != "json_schema":
        raise RuntimeError("information extraction output_schema must declare response_format type json_schema.")

    json_schema = payload.get("json_schema", {})
    schema = json_schema.get("schema")
    if not isinstance(schema, dict):
        raise RuntimeError("information extraction output_schema is missing an embedded JSON schema object.")

    required = sorted(schema.get("required", []))
    if required != ["document_description", "structured_information"]:
        raise RuntimeError("information extraction output_schema must require document_description and structured_information.")

    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise RuntimeError("information extraction output_schema properties are missing.")

    document_description = properties.get("document_description")
    if not isinstance(document_description, dict) or document_description.get("type") != "string":
        raise RuntimeError("information extraction output_schema must describe document_description as a string.")

    structured_information = properties.get("structured_information")
    if not isinstance(structured_information, dict) or structured_information.get("type") != "array":
        raise RuntimeError("information extraction output_schema must describe structured_information as an array.")

    items = structured_information.get("items", {})
    if items.get("type") != "object":
        raise RuntimeError("information extraction output_schema structured_information items must be objects.")

    item_props = items.get("properties", {})
    for key in ("name", "value", "short_description"):
        if key not in item_props:
            raise RuntimeError(f"information extraction output_schema items must include a '{key}' property.")


@lru_cache
def _load_output_schema() -> dict[str, Any]:
    try:
        payload = json.loads(_OUTPUT_SCHEMA_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:  # pragma: no cover - configuration path
        raise RuntimeError("information extraction output_schema.json is missing.") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("information extraction output_schema.json is not valid JSON.") from exc

    _validate_output_schema(payload)
    return payload


@lru_cache
def _prompt_template() -> Template:
    try:
        contents = _PROMPT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError as exc:  # pragma: no cover - configuration path
        raise RuntimeError("information extraction prompt template is missing.") from exc

    if not contents.strip():
        raise RuntimeError("information extraction prompt template cannot be empty.")
    return Template(contents, trim_blocks=True, lstrip_blocks=True)


def _render_prompt(file_name: str | None) -> str:
    template = _prompt_template()
    rendered = template.render(file_name=file_name or "the uploaded document").strip()
    return rendered or "You are an information extraction assistant."


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
    # Fallback to dict dump if available
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


async def extract_document_information(openai_file_id: str, *, file_name: str | None = None) -> InformationExtractionResult:
    """Use the OpenAI chat completions API to extract structured document facts."""

    settings = get_settings()
    if not settings.information_extraction_model:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server missing configuration: INFORMATION_EXTRACTION_MODEL",
        )

    client = get_openai_client()
    prompt = _render_prompt(file_name)
    response_format = _load_output_schema()

    def _create_response():
        logger.info(
            "Invoking chat.completions for file_id=%s model=%s filename=%s",
            openai_file_id,
            settings.information_extraction_model,
            file_name,
        )
        return client.chat.completions.create(
            model=settings.information_extraction_model,
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
                            "text": "Audit the referenced document and extract distinct facts verbatim. "
                            "Respond exclusively using the requested JSON schema.",
                        },
                        {
                            "type": "file",
                            "file": {"file_id": openai_file_id},
                        },
                    ],
                },
            ],
            response_format=response_format,  # noqa
        )

    try:
        response = await asyncio.to_thread(_create_response)
    except Exception as exc:  # pragma: no cover - network path
        logger.exception("OpenAI information extraction failed for %s", openai_file_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to extract structured information from the document.",
        ) from exc

    text_payload = _response_text(response)
    if not text_payload:
        logger.error("OpenAI extraction response missing text output for file %s", openai_file_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Information extraction response was empty.",
        )

    try:
        result = InformationExtractionResult.model_validate_json(text_payload)
        logger.info(
            "Structured extraction parsed for file_id=%s facts=%d",
            openai_file_id,
            len(result.structured_information),
        )
        return result
    except ValidationError as exc:
        logger.exception("OpenAI extraction response did not match the expected schema.")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Information extraction response was invalid.",
        ) from exc
