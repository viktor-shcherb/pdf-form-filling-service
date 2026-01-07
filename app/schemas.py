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


class ManifestFormEntry(BaseModel):
    formSlug: str
    formUrl: str
    sourceKey: str | None = None
    schemaKey: str | None = None
    filledKey: str | None = None
    filledFormUrl: str | None = None
    status: str | None = None
    totalFields: int | None = None
    filledFields: int | None = None
    skippedFields: int | None = None
    errorFields: int | None = None
    lastJobId: str | None = None
    message: str | None = None
    updatedAt: str | None = None


class Manifest(BaseModel):
    userId: str
    updatedAt: str
    files: list[ManifestFileEntry] = Field(default_factory=list)
    forms: dict[str, ManifestFormEntry] = Field(default_factory=dict)


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
    totalFields: int | None = None
    filledFields: int | None = None
    skippedFields: int | None = None
    errorFields: int | None = None
    message: str | None = None
    fields: list["FieldFillStatus"] | None = None


class ExtractedFact(BaseModel):
    name: str
    value: str
    short_description: str | None = None


class InformationExtractionResult(BaseModel):
    document_description: str
    structured_information: list[ExtractedFact] = Field(default_factory=list)


class FormFieldSchema(BaseModel):
    name: str
    page: int
    rect: list[float]
    label: str | None = None
    placeholder: str | None = None
    maxLength: int | None = None
    required: bool | None = None
    slug: str | None = None
    decision: str | None = None
    filledValue: str | None = None


class FormSchema(BaseModel):
    formSlug: str
    fields: list[FormFieldSchema] = Field(default_factory=list)
    totalFields: int
    extractedAt: str


class FieldFillStatus(BaseModel):
    fieldName: str
    status: str
    value: str | None = None
    confidence: float | None = None
    reason: str | None = None


class FieldFillDecision(BaseModel):
    field_name: str
    action: str
    value: str | None = None
    confidence: float | None = None
    reason: str | None = None
