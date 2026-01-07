"""Form fill endpoints that drive the actual pipeline."""

from __future__ import annotations

from fastapi import APIRouter, Query

from ..schemas import FormFillRequest, FormFillResponse
from ..services.form_fill_service import get_form_fill_job, start_form_fill_job

router = APIRouter(prefix="/api/form-fill", tags=["form-fill"])


@router.post("", response_model=FormFillResponse)
async def start_form_fill(request: FormFillRequest):
    return await start_form_fill_job(request.userId, str(request.formUrl))


@router.get("/{job_id}", response_model=FormFillResponse)
async def poll_form_fill(
    job_id: str,
    user_id: str = Query(..., alias="userId"),  # kept for backward compatibility
    form_url: str = Query(..., alias="formUrl"),
):
    _ = (user_id, form_url)
    return await get_form_fill_job(job_id)
