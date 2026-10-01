from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import TYPE_CHECKING

from pydantic import ValidationError

from .inventory import scan_repository
from .upgrade_quality import UpgradeQualityRunner
from .workflow_state import Issue, QAReport, WorkState

if TYPE_CHECKING:
    from .workflow import ContinuousWorkflow


class UpgradeCycle:
    def __init__(self, workflow: ContinuousWorkflow):
        self.workflow = workflow
        self.team = workflow.team
        self.quality = UpgradeQualityRunner(self.team.workspace)

    @staticmethod
    def _check_status(checks: list[dict]) -> list[tuple[str, int]]:
        return [(check["name"], check["returncode"]) for check in checks]

    async def run_phase(self, state: WorkState) -> None:
        phase = state.phase
        if phase == "inventory":
            inventory = await asyncio.to_thread(scan_repository, self.team.workspace)
            inventory["files"] = inventory["files"][:300]
            inventory["manifests"] = inventory["manifests"][:30]
            inventory["python_imports"] = inventory["python_imports"][:100]
            state.inventory = json.dumps(inventory, ensure_ascii=False)
            if inventory["truncated"]:
                state.issues = [
                    Issue("architect", "Repository inventory truncated", "More than 20,000 files", "medium")
                ]
            state.last_checks = await asyncio.to_thread(self.quality.run)
            state.phase = "architecture"
        elif phase == "architecture":
            state.plan = await self.workflow._ask(
                "architect",
                f"""Upgrade goal: {state.goal}
Repository inventory: {state.inventory}
Baseline checks: {json.dumps(state.last_checks)}
Human feedback: {state.feedback[-5:]}
Inspect real entry points, configuration, dependency manifests, source, and tests with tools.
Map architecture and dependency relationships, identify evidence-backed debt and risks.
Inspect lockfiles and verify suspected outdated/deprecated dependencies with the project
package manager when shell access is enabled; distinguish unknown from verified status,
then propose prioritized, incremental changes and acceptance criteria. State what was not
inspected. Do not invent deprecated packages or claim a security audit from green tests.
""",
            )
            state.phase = "upgrade_implement"
        elif phase == "upgrade_implement":
            state.implementation["fullstack"] = await self.workflow._ask(
                "fullstack",
                f"""Upgrade goal: {state.goal}
Architect plan: {state.plan}
Baseline checks: {json.dumps(state.last_checks)}
Human feedback: {state.feedback[-5:]}
Inspect actual files before editing. Implement the highest-priority changes completely in
the selected workspace, preserving behavior and existing user changes. Add focused tests.
Use terminal for dependencies/builds/tests only when available. Diagnose and correct errors.
Report exact files changed, checks and remaining work. No placeholders or truncated snippets.
""",
            )
            state.implementation["devops"] = await self.workflow._ask(
                "devops",
                f"""Upgrade goal: {state.goal}
Architect plan: {state.plan}
Developer handoff: {state.implementation["fullstack"]}
Baseline checks: {json.dumps(state.last_checks)}
Inspect project configuration and build scripts. Complete necessary CI, environment,
dependency, and deployment improvements within your owned paths. Run relevant checks
when terminal is enabled; document real results and remaining environmental blockers.
""",
            )
            state.phase = "upgrade_verify"
        elif phase == "upgrade_verify":
            checks = await asyncio.to_thread(self.quality.run)
            state.last_checks = checks
            state.last_checked = self.workflow._now()
            state.last_signature = await asyncio.to_thread(self.team.workspace.signature)
            prompt = f"""Upgrade goal: {state.goal}
Architect plan: {state.plan}
Implementation reports: {json.dumps(state.implementation)}
Deterministic checks: {json.dumps(checks)}
Human feedback: {state.feedback[-5:]}
Inspect relevant real files with tools. Do not edit. Assess functionality, tests,
configuration, security risks, regressions, and whether the stated upgrade goal is done.
Return one JSON object only: {{"passed": false, "issues": [{{"owner": "fullstack",
"summary": "...", "evidence": "file/check/behavior", "severity": "high"}}], "summary": "..."}}.
Use owner fullstack, devops, architect, or qa. Only pass with concrete evidence.
Never claim an unrun check, browser test, or security audit passed.
"""
            state.qa_report = await self.workflow._ask("qa", prompt)
            report = None
            for attempt in range(2):
                try:
                    report = QAReport.model_validate_json(state.qa_report)
                    break
                except ValidationError:
                    if attempt == 0:
                        state.qa_report = await self.workflow._ask(
                            "qa",
                            "Restate as one valid JSON object with passed, issues, summary. No Markdown. "
                            + state.qa_report[-4000:],
                            use_tools=False,
                        )
            if report is None:
                state.issues = [Issue("qa", "Invalid QA report", state.qa_report[-1200:], "high")]
                state.status = "blocked"
                state.phase = "blocked"
                return
            issues = [Issue(**item.model_dump()) for item in report.issues]
            for check in checks:
                if check["returncode"] != 0:
                    owner = "devops" if check["returncode"] in {126, 127} else "fullstack"
                    issues.append(Issue(owner, f"Check failed: {check['name']}", check["output"][-1200:], "high"))
            if json.loads(state.inventory).get("truncated"):
                issues.append(
                    Issue(
                        "architect", "Repository exceeds inventory limit", "More than 20,000 accessible files", "high"
                    )
                )
            if not any(any(word in item["name"] for word in ("test", "build", "lint", "types")) for item in checks):
                issues.append(
                    Issue(
                        "devops",
                        "No automated test/build/lint checks detected",
                        "Verification coverage is insufficient",
                        "high",
                    )
                )
            if not report.passed and not issues:
                issues.append(Issue("qa", "QA rejected without actionable evidence", state.qa_report[-1200:], "high"))
            state.issues = issues
            state.cycle += 1
            if report.passed and not issues:
                state.status = "monitoring"
                state.phase = "upgrade_monitor"
                state.repeated_failures = 0
                self.team.emit("Upgrade checks and QA passed; monitoring for new changes.")
            else:
                signature = json.dumps(sorted((issue.owner, issue.summary) for issue in issues))
                state.repeated_failures = state.repeated_failures + 1 if signature == state.issue_signature else 0
                state.issue_signature = signature
                if state.repeated_failures >= self.team.config.max_repair_attempts:
                    state.status = "blocked"
                    state.phase = "blocked"
                    self.team.emit("Repeated identical upgrade failures; paused repair until evidence changes.")
                else:
                    state.status = "running"
                    state.phase = "upgrade_repair"
                    self.team.emit(f"Upgrade QA found {len(issues)} issue(s); assigning repairs.")
        elif phase == "upgrade_repair":
            before = await asyncio.to_thread(self.team.workspace.signature)
            for owner in dict.fromkeys(issue.owner for issue in state.issues):
                assigned = "fullstack" if owner in {"qa", "architect", "frontend", "backend", "ux"} else owner
                own_issues = [asdict(issue) for issue in state.issues if issue.owner == owner]
                await self.workflow._ask(
                    assigned,
                    f"Upgrade goal: {state.goal}\nArchitect plan: {state.plan}\n"
                    f"Repair these evidence-backed findings: {json.dumps(own_issues)}\n"
                    f"Human feedback: {state.feedback[-5:]}\n"
                    "Inspect actual files, fix root causes within your owned paths, run relevant checks, "
                    "and report exact results. Preserve unrelated changes.",
                )
            after = await asyncio.to_thread(self.team.workspace.signature)
            state.phase = "upgrade_verify" if after != before else "blocked"
            if state.phase == "blocked":
                state.status = "blocked"
                self.team.emit("Repair made no file changes; waiting for new evidence or feedback.")
        elif phase == "upgrade_monitor":
            await self.workflow._sleep_or_wake()
            if not self.workflow.stop_event.is_set():
                state.phase = "upgrade_verify"
                state.status = "running"
        elif phase == "blocked":
            await self.workflow._sleep_or_wake()
            if state.phase == "blocked":
                checks = await asyncio.to_thread(self.quality.run)
                signature = await asyncio.to_thread(self.team.workspace.signature)
                if (
                    self._check_status(checks) != self._check_status(state.last_checks)
                    or signature != state.last_signature
                ):
                    state.last_checks = checks
                    state.last_signature = signature
                    state.phase = "upgrade_verify"
                    state.status = "running"
        else:
            raise ValueError(f"Unknown upgrade phase: {phase}")
