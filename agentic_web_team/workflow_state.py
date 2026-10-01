from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

OWNERS = frozenset({"ux", "frontend", "backend", "manager", "architect", "fullstack", "devops", "qa"})
PHASES = frozenset(
    {
        "plan",
        "design",
        "implement",
        "qa",
        "repair",
        "monitor",
        "blocked",
        "inventory",
        "architecture",
        "upgrade_implement",
        "upgrade_verify",
        "upgrade_repair",
        "upgrade_monitor",
    }
)
STATUSES = frozenset({"running", "monitoring", "blocked", "stopped"})


class QAIssue(BaseModel):
    model_config = ConfigDict(extra="ignore")

    owner: Literal["ux", "frontend", "backend", "architect", "fullstack", "devops", "qa"]
    summary: str = Field(min_length=1, max_length=500)
    evidence: str = Field(min_length=1, max_length=2000)
    severity: Literal["low", "medium", "high"] = "medium"


class QAReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    passed: bool
    issues: list[QAIssue] = Field(max_length=30)
    summary: str = Field(default="", max_length=2000)


@dataclass
class Issue:
    owner: str
    summary: str
    evidence: str
    severity: str = "medium"

    def __post_init__(self) -> None:
        if self.owner not in OWNERS or self.severity not in {"low", "medium", "high"}:
            raise ValueError("Issue has an invalid owner or severity")
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise ValueError("Issue summary is required")
        if not isinstance(self.evidence, str):
            raise ValueError("Issue evidence must be text")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Issue:
        if not isinstance(data, dict):
            raise ValueError("Issue must be an object")
        return cls(
            owner=data["owner"],
            summary=data["summary"],
            evidence=data["evidence"],
            severity=data.get("severity", "medium"),
        )


@dataclass
class WorkState:
    goal: str
    mode: str = "web"
    schema_version: int = 1
    status: str = "running"
    phase: str = "plan"
    phase_started_at: str = ""
    active_agents: dict[str, str] = field(default_factory=dict)
    cycle: int = 0
    plan: str = ""
    inventory: str = ""
    design: str = ""
    implementation: dict[str, str] = field(default_factory=dict)
    qa_report: str = ""
    issues: list[Issue] = field(default_factory=list)
    last_checks: list[dict[str, Any]] = field(default_factory=list)
    last_checked: str = ""
    last_signature: str = ""
    repeated_failures: int = 0
    issue_signature: str = ""
    feedback: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.goal, str) or not self.goal.strip() or len(self.goal) > 20_000:
            raise ValueError("Project goal must be between 1 and 20,000 characters")
        if self.schema_version != 1:
            raise ValueError(f"Unsupported work-state version: {self.schema_version}")
        if self.mode not in {"web", "upgrade"}:
            raise ValueError("Work state has an invalid mode")
        if self.phase not in PHASES or self.status not in STATUSES:
            raise ValueError("Work state has an invalid phase or status")
        if not isinstance(self.cycle, int) or self.cycle < 0:
            raise ValueError("Work-state cycle must be non-negative")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkState:
        if not isinstance(data, dict):
            raise ValueError("Work state must be an object")
        allowed = {item.name for item in fields(cls)}
        values = {key: value for key, value in data.items() if key in allowed}
        values["issues"] = [Issue.from_dict(item) for item in values.get("issues", [])]
        return cls(**values)
