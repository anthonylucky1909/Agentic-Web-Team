from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import ValidationError

from .models import CompanyConfig, EmployeeSpec


class ConfigError(ValueError):
    pass


def default_config_path() -> Path:
    package = Path(__file__).resolve().parent
    checkout = package.parent / "config" / "team.yaml"
    return checkout if checkout.is_file() else package / "defaults" / "team.yaml"


def load_config(path: str | Path | None = None, env: Mapping[str, str] | None = None) -> CompanyConfig:
    config_path = Path(path) if path is not None else default_config_path()
    config_path = config_path.expanduser().resolve()
    environment = os.environ if env is None else env
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("model"), dict)
            or not isinstance(raw.get("employees"), dict)
        ):
            raise ConfigError("Expected top-level model and employees mappings")
        model = raw["model"]
        runtime = raw.get("runtime", {})
        if not isinstance(runtime, dict):
            raise ConfigError("runtime must be a mapping")
        skill_root = config_path.parent.parent / "skills"
        if not skill_root.is_dir():
            skill_root = config_path.parent / "skills"
        employees = {key: EmployeeSpec.model_validate({"key": key, **item}) for key, item in raw["employees"].items()}
        return CompanyConfig.model_validate(
            {
                "root": config_path.parent.parent,
                "skill_root": skill_root,
                "model_name": environment.get("AGENTIC_WEB_TEAM_MODEL", model["name"]),
                "base_url": environment.get(
                    "AGENTIC_WEB_TEAM_BASE_URL", model.get("base_url", "http://localhost:11434/v1")
                ),
                "api_key": environment.get("AGENTIC_WEB_TEAM_API_KEY", model.get("api_key", "ollama")),
                "temperature": environment.get("AGENTIC_WEB_TEAM_TEMPERATURE", model.get("temperature", 0.2)),
                "max_tool_rounds": runtime.get("max_tool_rounds", 12),
                "artifact_dir": runtime.get("artifact_dir", ".agentic_web_team"),
                "monitor_interval_seconds": environment.get(
                    "AGENTIC_WEB_TEAM_MONITOR_SECONDS", runtime.get("monitor_interval_seconds", 300)
                ),
                "max_repair_attempts": runtime.get("max_repair_attempts", 3),
                "checks": raw.get("checks", {}),
                "employees": employees,
            }
        )
    except (OSError, yaml.YAMLError, KeyError, TypeError, ValidationError) as exc:
        raise ConfigError(f"Invalid team configuration at {config_path}: {exc}") from exc


def load_skill_text(config: CompanyConfig, skill_names: list[str]) -> str:
    chunks: list[str] = []
    for skill in skill_names:
        path = (config.skill_root / skill / "SKILL.md").resolve()
        if not path.is_relative_to(config.skill_root.resolve()) or not path.is_file():
            raise ConfigError(f"Missing or invalid skill {skill!r} in {config.skill_root}")
        chunks.append(path.read_text(encoding="utf-8"))
    return "\n\n".join(chunks)
