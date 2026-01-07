"""Pydantic schemas shared across routers."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, HttpUrl


class ManifestFileEntry(BaseModel):
    slug: str
    objectKey: str | None = None
    infoKey: str | None = None
    fileName: str | None = None
    s3Url: str | None = None
    contentType: str | None = None
    size: int | None = None
    openaiFileId: str | None = None
    uploadedAt: str | None = None
    status: str | None = "uploaded"


class Manifest(BaseModel):
    userId: str
    updatedAt: str
    files: list[ManifestFileEntry] = Field(default_factory=list)
    forms: dict[str, Any] = Field(default_factory=dict)


class UploadResponse(BaseModel):
    status: str
    slug: str
    s3Url: str
    fileName: str
    openaiFileId: str | None = None
    size: int | None = None


class UploadListResponse(BaseModel):
    files: list[UploadResponse]
    updatedAt: str


class DeleteResponse(BaseModel):
    status: str
    slug: str


class FormFillRequest(BaseModel):
    userId: str
    formUrl: HttpUrl


class FormFillResponse(BaseModel):
    jobId: str
    status: str
    filledFormUrl: str | None = None
