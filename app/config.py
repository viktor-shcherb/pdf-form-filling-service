"""Application settings and helpers."""

from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()


class Settings(BaseModel):
    s3_bucket_url: str = "https://s3.amazonaws.com/mvp-form-fill"
    s3_bucket_name: str | None = None
    s3_bucket_region: str | None = None
    openai_api_key: str | None = None
    openai_api_base: str | None = None
    openai_org_id: str | None = None
    openai_file_purpose: str = "assistants"
    manifest_filename: str = "manifest.json"
    information_extraction_model: str = "gpt-5-mini"


@lru_cache
def get_settings() -> Settings:
    return Settings(
        s3_bucket_url=os.getenv("S3_BUCKET_URL", Settings.model_fields["s3_bucket_url"].default).rstrip("/"),
        s3_bucket_name=os.getenv("S3_BUCKET_NAME"),
        s3_bucket_region=os.getenv("S3_BUCKET_REGION"),
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        openai_api_base=os.getenv("OPENAI_API_BASE"),
        openai_org_id=os.getenv("OPENAI_ORG_ID"),
        openai_file_purpose=os.getenv(
            "OPENAI_FILE_PURPOSE",
            Settings.model_fields["openai_file_purpose"].default,
        ),
        manifest_filename=os.getenv(
            "MANIFEST_FILENAME",
            Settings.model_fields["manifest_filename"].default,
        ),
        information_extraction_model=os.getenv(
            "INFORMATION_EXTRACTION_MODEL",
            Settings.model_fields["information_extraction_model"].default,
        ),
    )
