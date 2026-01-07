"""Form fill endpoints (still stubbed)."""

from __future__ import annotations

import asyncio
import random
import uuid

from fastapi import APIRouter, Query

from ..config import get_settings
from ..schemas import FormFillRequest, FormFillResponse
from ..utils import build_filled_form_url, slugify

router = APIRouter(prefix="/api/form-fill", tags=["form-fill"])

FILL_STATUS_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("queued", 0.25),
    ("filling", 0.25),
    ("complete", 0.35),
    ("error", 0.15),
)


async def simulate_latency(min_seconds: float = 0.5, max_seconds: float = 1.7) -> None:
    await asyncio.sleep(random.uniform(min_seconds, max_seconds))


def weighted_status(weights: tuple[tuple[str, float], ...]) -> str:
    labels, probabilities = zip(*weights)
    return random.choices(population=labels, weights=probabilities, k=1)[0]


def _filled_form_url(user_id: str, form_url: str) -> str:
    settings = get_settings()
    slug = slugify(form_url)
    return build_filled_form_url(settings.s3_bucket_url, user_id, slug)


@router.post("", response_model=FormFillResponse)
async def start_form_fill(request: FormFillRequest):
    await simulate_latency()
    job_id = str(uuid.uuid4())
    status = weighted_status(FILL_STATUS_WEIGHTS)
    filled_url = _filled_form_url(request.userId, str(request.formUrl)) if status == "complete" else None
    return FormFillResponse(jobId=job_id, status=status, filledFormUrl=filled_url)


@router.get("/{job_id}", response_model=FormFillResponse)
async def poll_form_fill(
    job_id: str,
    user_id: str = Query(..., alias="userId"),
    form_url: str = Query(..., alias="formUrl"),
):
    await simulate_latency(0.25, 1.0)
    status = weighted_status(FILL_STATUS_WEIGHTS)
    filled_url = _filled_form_url(user_id, form_url) if status == "complete" else None
    return FormFillResponse(jobId=job_id, status=status, filledFormUrl=filled_url)
