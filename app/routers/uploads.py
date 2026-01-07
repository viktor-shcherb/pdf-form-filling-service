"""Upload endpoints."""

from __future__ import annotations

from fastapi import APIRouter, File, Form, Query, UploadFile

from ..schemas import DeleteResponse, UploadListResponse, UploadResponse
from ..services.upload_service import delete_upload, handle_upload, list_uploads

router = APIRouter(prefix="/api/uploads", tags=["uploads"])


@router.post("", response_model=UploadResponse)
async def upload_file(
    user_id: str = Form(..., alias="userId"),
    file: UploadFile = File(...),
):
    return await handle_upload(user_id, file)


@router.get("", response_model=UploadListResponse)
async def list_files(user_id: str = Query(..., alias="userId")):
    manifest = await list_uploads(user_id)
    files = [
        UploadResponse(
            status=entry.status or "uploaded",
            slug=entry.slug,
            s3Url=entry.s3Url or "",
            fileName=entry.fileName or entry.slug,
            openaiFileId=entry.openaiFileId,
            size=entry.size,
        )
        for entry in manifest.files
        if entry.slug
    ]
    return UploadListResponse(files=files, updatedAt=manifest.updatedAt)


@router.delete("/{slug}", response_model=DeleteResponse)
async def delete_file(slug: str, user_id: str = Query(..., alias="userId")):
    deleted = await delete_upload(user_id, slug)
    return DeleteResponse(status="deleted" if deleted else "missing", slug=slug)
