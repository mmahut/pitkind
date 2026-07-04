from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .schemas import model_output_json_schema


class ModelEntry(BaseModel):
    provider: Literal["openrouter", "openai", "anthropic"] = "openrouter"
    openrouter_id: str
    decoding_params: dict[str, Any] = {"temperature": 0, "top_p": 1.0, "max_tokens": 16384}

    @model_validator(mode="after")
    def current_response_schema(self) -> ModelEntry:
        if self.decoding_params.get("response_format", {}).get("type") == "json_schema":
            self.decoding_params = {**self.decoding_params, "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "model_output", "strict": True, "schema": model_output_json_schema()},
            }}
        return self


class NCLConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit_lovelace: int = Field(strict=True, gt=0)


class Config(BaseModel):
    models: list[ModelEntry]
    ballot_categories: list[str]
    max_retries: int = Field(default=2, ge=0)
    max_api_retries: int = Field(default=3, ge=0)
    output_dir: str = "./output"
    ncl: NCLConfig | None = None

    @field_validator("models")
    @classmethod
    def odd_model_count(cls, v: list[ModelEntry]) -> list[ModelEntry]:
        if not v:
            raise ValueError("models list must not be empty")
        if len(v) % 2 == 0:
            raise ValueError(
                f"models list must have an odd number of entries as required by the protocol (got {len(v)})"
            )
        return v

    @field_validator("ballot_categories")
    @classmethod
    def fixed_ballot(cls, v: list[str]) -> list[str]:
        if v != ["YES", "NO", "ABSTAIN"]:
            raise ValueError('ballot_categories must be exactly ["YES", "NO", "ABSTAIN"]')
        return v


def load_config(path: Path) -> Config:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return Config.model_validate(raw)
