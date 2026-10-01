from __future__ import annotations

from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EmployeeSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    name: str = Field(min_length=1)
    role: str = Field(min_length=1)
    job_description: str = Field(min_length=1)
    skills: list[str] = Field(default_factory=list)
    permissions: set[str] = Field(default_factory=lambda: {"read"})
    write_paths: list[str] = Field(default_factory=list)

    @field_validator("write_paths", "skills")
    @classmethod
    def safe_paths(cls, values: list[str]) -> list[str]:
        for value in values:
            path = PurePosixPath(value)
            if not value or path.is_absolute() or any(part in {".", ".."} for part in value.split("/")):
                raise ValueError(f"Expected a relative path without traversal: {value!r}")
        return values

    @model_validator(mode="after")
    def validate_write_access(self) -> EmployeeSpec:
        if not self.permissions or not self.permissions <= {"read", "write", "check", "terminal"}:
            raise ValueError("permissions must contain only read, write, check, or terminal")
        if self.write_paths and "write" not in self.permissions:
            raise ValueError("write_paths requires the write permission")
        if "write" in self.permissions and not self.write_paths:
            raise ValueError("write permission requires at least one write_paths entry")
        return self


class CompanyConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    root: Path
    skill_root: Path
    model_name: str = Field(min_length=1)
    base_url: str
    api_key: str = Field(min_length=1)
    temperature: float = Field(ge=0, le=2)
    max_tool_rounds: int = Field(ge=1, le=100)
    artifact_dir: str
    monitor_interval_seconds: int = Field(ge=10)
    max_repair_attempts: int = Field(ge=1, le=20)
    checks: dict[str, list[str]]
    employees: dict[str, EmployeeSpec]

    @field_validator("base_url")
    @classmethod
    def secure_model_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("model base_url must be an HTTP(S) URL")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("non-local model endpoints must use HTTPS")
        return value.rstrip("/")

    @field_validator("artifact_dir")
    @classmethod
    def safe_artifact_dir(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not value or path.is_absolute() or any(part in {".", ".."} for part in value.split("/")):
            raise ValueError("artifact_dir must be a relative directory inside the workspace")
        return value

    @field_validator("checks")
    @classmethod
    def valid_checks(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        if any(
            not name or not command or any(not isinstance(arg, str) or not arg for arg in command)
            for name, command in value.items()
        ):
            raise ValueError("each check must have a name and a non-empty argv list")
        return value

    @model_validator(mode="after")
    def required_roles(self) -> CompanyConfig:
        missing = {"manager", "ux", "frontend", "backend", "qa"} - self.employees.keys()
        if missing:
            raise ValueError(f"missing required roles: {', '.join(sorted(missing))}")
        return self
