"""End-to-end orchestration of the form filling pipeline."""

from __future__ import annotations

import asyncio
import io
import logging
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import pymupdf
from fastapi import HTTPException, status
from pydantic import ValidationError

from ..config import get_settings
from ..schemas import (
    FieldFillDecision,
    FieldFillStatus,
    FormFillResponse,
    FormFieldSchema,
    FormSchema,
    InformationExtractionResult,
    Manifest,
    ManifestFormEntry,
)
from ..services.form_field_decision_service import decide_field_value
from ..services.storage_service import download_s3_object, load_manifest, save_manifest, upload_bytes_to_s3
from ..utils import (
    build_form_filled_key,
    build_form_schema_key,
    build_form_source_key,
    build_filled_form_url,
    now_iso,
    sanitize_user_id,
    slugify,
)

logger = logging.getLogger(__name__)

_form_jobs: dict[str, dict[str, Any]] = {}
_job_tasks: dict[str, asyncio.Task[Any]] = {}
_jobs_lock = asyncio.Lock()


@dataclass
class FactRecord:
    name: str
    value: str
    description: str | None
    source: str | None


def _ensure_http_url(form_url: str) -> str:
    try:
        parsed = httpx.URL(form_url)
    except Exception as exc:  # pragma: no cover - defensive
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Form URL is invalid.") from exc
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Form URL must be HTTP or HTTPS.")
    return form_url


def _job_response(job: dict[str, Any]) -> FormFillResponse:
    fields_payload = None
    if job.get("fieldOrder"):
        fields_payload = [
            job["fields"][name]
            for name in job["fieldOrder"]
            if name in job["fields"]
        ]
    return FormFillResponse(
        jobId=job["jobId"],
        status=job["status"],
        filledFormUrl=job.get("filledFormUrl"),
        totalFields=job.get("totalFields"),
        filledFields=job.get("filledFields"),
        skippedFields=job.get("skippedFields"),
        errorFields=job.get("errorFields"),
        message=job.get("message"),
        fields=fields_payload,
    )


def _create_job(job_id: str, user_id: str, form_slug: str, form_url: str) -> dict[str, Any]:
    now = now_iso()
    return {
        "jobId": job_id,
        "userId": sanitize_user_id(user_id),
        "formSlug": form_slug,
        "formUrl": form_url,
        "status": "queued",
        "message": None,
        "filledFormUrl": None,
        "totalFields": 0,
        "filledFields": 0,
        "skippedFields": 0,
        "errorFields": 0,
        "fields": {},
        "fieldOrder": [],
        "createdAt": now,
        "updatedAt": now,
    }


async def _persist_form_schema(schema: FormSchema, schema_key: str) -> None:
    await upload_bytes_to_s3(
        schema_key,
        schema.model_dump_json(indent=2).encode("utf-8"),
        "application/json",
    )


def _update_job_counts(job: dict[str, Any]) -> None:
    field_statuses = job["fields"].values()
    job["filledFields"] = sum(1 for status in field_statuses if status.status == "filled")
    job["skippedFields"] = sum(1 for status in field_statuses if status.status == "skipped")
    job["errorFields"] = sum(1 for status in field_statuses if status.status == "error")
    job["updatedAt"] = now_iso()


def _set_job_status(job: dict[str, Any], status_label: str, message: str | None = None) -> None:
    job["status"] = status_label
    if message is not None:
        job["message"] = message
    job["updatedAt"] = now_iso()


def _set_field_status(job: dict[str, Any], field_name: str, **updates: Any) -> None:
    if field_name not in job["fields"]:
        job["fields"][field_name] = FieldFillStatus(fieldName=field_name, status="pending")
    job["fields"][field_name] = job["fields"][field_name].model_copy(update=updates)
    _update_job_counts(job)


async def start_form_fill_job(user_id: str, form_url: str) -> FormFillResponse:
    sanitized_url = _ensure_http_url(form_url)
    form_slug = slugify(sanitized_url)
    manifest = await load_manifest(user_id)
    if not manifest.files:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No uploads available for this user.")

    job_id = str(uuid.uuid4())
    job = _create_job(job_id, user_id, form_slug, sanitized_url)

    async with _jobs_lock:
        _form_jobs[job_id] = job
        task = asyncio.create_task(_run_form_fill_job(job_id, user_id, sanitized_url, form_slug))
        _job_tasks[job_id] = task

    return _job_response(job)


async def get_form_fill_job(job_id: str) -> FormFillResponse:
    async with _jobs_lock:
        job = _form_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown form fill job.")
    return _job_response(job)


async def _run_form_fill_job(job_id: str, user_id: str, form_url: str, form_slug: str) -> None:
    job = _form_jobs[job_id]
    settings = get_settings()
    try:
        manifest = await load_manifest(user_id)
        document_description, facts = await _collect_structured_facts(manifest)
        facts_text = _format_facts_text(facts)

        pdf_bytes, content_type = await _download_form_pdf(form_url)
        source_key = build_form_source_key(user_id, form_slug)
        await upload_bytes_to_s3(source_key, pdf_bytes, content_type)

        schema = _extract_form_schema(pdf_bytes, form_slug)
        schema_key = build_form_schema_key(user_id, form_slug)
        await _persist_form_schema(schema, schema_key)

        job["totalFields"] = schema.totalFields
        job["fieldOrder"] = [field.name for field in schema.fields]
        for field in schema.fields:
            job["fields"][field.name] = FieldFillStatus(fieldName=field.name, status="pending")
        _update_job_counts(job)

        manifest = _update_manifest_form_entry(
            manifest,
            user_id,
            form_slug,
            form_url,
            source_key=source_key,
            schema_key=schema_key,
            status="queued",
            total_fields=schema.totalFields,
            last_job_id=job_id,
        )

        await save_manifest(user_id, manifest)

        if schema.totalFields == 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No text fields detected in the form.")

        _set_job_status(job, "filling")

        await _fill_fields_concurrently(
            job,
            schema.fields,
            document_description=document_description,
            facts_text=facts_text,
        )

        await _persist_form_schema(schema, schema_key)

        filled_values = {
            status.fieldName: status.value or ""
            for status in job["fields"].values()
            if status.status == "filled" and status.value
        }

        filled_bytes = _apply_field_values(pdf_bytes, filled_values) if filled_values else pdf_bytes
        filled_key = build_form_filled_key(user_id, form_slug)
        await upload_bytes_to_s3(filled_key, filled_bytes, "application/pdf")

        filled_url = build_filled_form_url(settings.s3_bucket_url, user_id, form_slug)
        job["filledFormUrl"] = filled_url
        _set_job_status(job, "complete")

        manifest = _update_manifest_form_entry(
            manifest,
            user_id,
            form_slug,
            form_url,
            source_key=source_key,
            schema_key=schema_key,
            filled_key=filled_key,
            filled_form_url=filled_url,
            status="complete",
            total_fields=schema.totalFields,
            filled_fields=job.get("filledFields", 0),
            skipped_fields=job.get("skippedFields", 0),
            error_fields=job.get("errorFields", 0),
            last_job_id=job_id,
            message="Form filling complete.",
        )
        await save_manifest(user_id, manifest)
    except HTTPException as exc:
        logger.error("Form fill job %s failed: %s", job_id, exc.detail)
        _set_job_status(job, "error", exc.detail)
    except Exception as exc:  # pragma: no cover - unexpected path
        logger.exception("Form fill job %s encountered an unexpected error", job_id)
        _set_job_status(job, "error", "Unexpected error during form filling.")
    finally:
        async with _jobs_lock:
            task = _job_tasks.pop(job_id, None)
            if task:
                task.cancelled()


async def _download_form_pdf(form_url: str) -> tuple[bytes, str]:
    timeout = httpx.Timeout(30.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(form_url)
        if response.status_code >= 400:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to download form ({response.status_code}).",
            )
        content_type = response.headers.get("content-type") or "application/pdf"
        if "pdf" not in content_type:
            logger.warning("Downloaded form does not advertise PDF content-type: %s", content_type)
        return response.content, content_type


def _extract_form_schema(pdf_bytes: bytes, form_slug: str) -> FormSchema:
    doc = pymupdf.open(stream=io.BytesIO(pdf_bytes), filetype="pdf")
    try:
        fields: list[FormFieldSchema] = []
        seen_names: set[str] = set()
        for page_index, page in enumerate(doc):
            for widget in page.widgets() or []:
                if (widget.field_type_string or "").lower() != "text":
                    continue
                field_name = widget.field_name or f"field-{page_index}-{len(fields)}"
                if field_name in seen_names:
                    continue
                seen_names.add(field_name)
                rect = widget.rect
                record = FormFieldSchema(
                    name=field_name,
                    page=page_index,
                    rect=[float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)],
                    label=widget.field_label or field_name,
                    placeholder=widget.field_value or None,
                    maxLength=getattr(widget, "maxlen", None) or getattr(widget, "max_len", None),
                    required=bool(getattr(widget, "field_flags", 0) & 2),
                )
                fields.append(record)
        return FormSchema(formSlug=form_slug, fields=fields, totalFields=len(fields), extractedAt=now_iso())
    finally:
        doc.close()


async def _collect_structured_facts(manifest: Manifest) -> tuple[str, list[FactRecord]]:
    facts: list[FactRecord] = []
    descriptions: list[str] = []
    for entry in manifest.files:
        if not entry.infoKey:
            continue
        try:
            payload = await download_s3_object(entry.infoKey)
        except HTTPException as exc:
            logger.warning("Unable to load info.json for slug=%s: %s", entry.slug, exc.detail)
            continue
        try:
            info = InformationExtractionResult.model_validate_json(payload.decode("utf-8"))
        except ValidationError as exc:
            logger.warning("info.json invalid for slug=%s: %s", entry.slug, exc)
            continue
        if info.document_description:
            descriptions.append(info.document_description)
        for fact in info.structured_information:
            facts.append(
                FactRecord(
                    name=fact.name,
                    value=fact.value,
                    description=fact.short_description,
                    source=entry.slug or entry.fileName or "upload",
                )
            )

    if not facts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No structured facts available. Upload documents again to extract data.",
        )

    document_description = "; ".join(descriptions) if descriptions else "No supporting description."
    return document_description, facts


def _format_facts_text(facts: list[FactRecord], limit: int = 80) -> str:
    lines: list[str] = []
    for index, fact in enumerate(facts):
        if index >= limit:
            break
        detail_parts: list[str] = []
        if fact.description:
            detail_parts.append(fact.description)
        if fact.source:
            detail_parts.append(f"source={fact.source}")
        detail_suffix = f" ({'; '.join(detail_parts)})" if detail_parts else ""
        lines.append(f"- {fact.name}: {fact.value}{detail_suffix}")
    if not lines:
        return "No supporting facts provided."
    if len(facts) > limit:
        lines.append(f"- ... {len(facts) - limit} additional facts truncated ...")
    return "\n".join(lines)


async def _fill_fields_concurrently(
    job: dict[str, Any],
    fields: list[FormFieldSchema],
    *,
    document_description: str,
    facts_text: str,
) -> None:
    settings = get_settings()
    semaphore = asyncio.Semaphore(max(1, settings.form_fill_max_concurrency))

    async def _worker(field: FormFieldSchema) -> None:
        async with semaphore:
            _set_field_status(job, field.name, status="prompting")
            try:
                decision: FieldFillDecision = await decide_field_value(
                    field,
                    document_description=document_description,
                    facts_text=facts_text,
                )
                field.decision = decision.action.lower()
            except HTTPException as exc:
                field.decision = "error"
                field.filledValue = None
                _set_field_status(job, field.name, status="error", reason=exc.detail)
                return

            action = decision.action.lower()
            if action == "fill" and decision.value:
                cleaned_value = decision.value.strip()
                field.filledValue = cleaned_value
                _set_field_status(
                    job,
                    field.name,
                    status="filled",
                    value=cleaned_value,
                    confidence=decision.confidence,
                    reason=decision.reason,
                )
            elif action == "skip":
                field.filledValue = None
                _set_field_status(
                    job,
                    field.name,
                    status="skipped",
                    reason=decision.reason or "Model skipped field.",
                    confidence=decision.confidence,
                )
            else:
                field.decision = "error"
                field.filledValue = None
                _set_field_status(
                    job,
                    field.name,
                    status="error",
                    reason="Model response missing required value.",
                )

    await asyncio.gather(*(_worker(field) for field in fields))


def _apply_field_values(pdf_bytes: bytes, fills: dict[str, str]) -> bytes:
    if not fills:
        return pdf_bytes
    doc = pymupdf.open(stream=io.BytesIO(pdf_bytes), filetype="pdf")
    try:
        for page in doc:
            for widget in page.widgets() or []:
                field_name = widget.field_name or ""
                if field_name in fills and (widget.field_type_string or "").lower() == "text":
                    widget.field_value = fills[field_name]
                    widget.update()
        return doc.tobytes()
    finally:
        doc.close()


def _update_manifest_form_entry(
    manifest: Manifest,
    user_id: str,
    form_slug: str,
    form_url: str,
    *,
    source_key: str | None = None,
    schema_key: str | None = None,
    filled_key: str | None = None,
    filled_form_url: str | None = None,
    status: str | None = None,
    total_fields: int | None = None,
    filled_fields: int | None = None,
    skipped_fields: int | None = None,
    error_fields: int | None = None,
    last_job_id: str | None = None,
    message: str | None = None,
) -> Manifest:
    entry = manifest.forms.get(form_slug)
    base_data = entry.model_dump() if isinstance(entry, ManifestFormEntry) else {}
    base_data.update(
        {
            "formSlug": form_slug,
            "formUrl": form_url,
            "sourceKey": source_key or base_data.get("sourceKey"),
            "schemaKey": schema_key or base_data.get("schemaKey"),
            "filledKey": filled_key or base_data.get("filledKey"),
            "filledFormUrl": filled_form_url or base_data.get("filledFormUrl"),
            "status": status or base_data.get("status"),
            "totalFields": total_fields if total_fields is not None else base_data.get("totalFields"),
            "filledFields": filled_fields if filled_fields is not None else base_data.get("filledFields"),
            "skippedFields": skipped_fields if skipped_fields is not None else base_data.get("skippedFields"),
            "errorFields": error_fields if error_fields is not None else base_data.get("errorFields"),
            "lastJobId": last_job_id or base_data.get("lastJobId"),
            "message": message or base_data.get("message"),
            "updatedAt": now_iso(),
        }
    )
    manifest.forms[form_slug] = ManifestFormEntry(**base_data)
    return manifest
