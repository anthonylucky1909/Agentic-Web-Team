from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from openai import APITimeoutError
from pydantic import ValidationError

from .agent import ToolRoundLimitError
from .quality import QualityRunner
from .storage import JsonStateStore, StorageError, WorkflowLease
from .upgrade import UpgradeCycle
from .workflow_state import Issue, QAReport, WorkState

if TYPE_CHECKING:
    from .orchestrator import SoftwareTeam


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class ContinuousWorkflow:
    def __init__(self, team: SoftwareTeam):
        self.team = team
        self.path = team.state_dir / "work_state.json"
        self.store = JsonStateStore(self.path)
        self.lease = WorkflowLease(team.state_dir / "workflow.lock")
        self.state = self._load()
        self.task: asyncio.Task | None = None
        self.stop_event = asyncio.Event()
        self.wake_event = asyncio.Event()
        self.quality = QualityRunner(team.workspace.root, team.preview)
        self.upgrade = UpgradeCycle(self)

    @staticmethod
    def _now() -> str:
        return now()

    def _load(self) -> WorkState | None:
        try:
            data = self.store.load()
            return WorkState.from_dict(data) if data is not None else None
        except (ValueError, TypeError, KeyError) as exc:
            raise StorageError(f"Invalid work state at {self.path}: {exc}") from exc

    def _save(self) -> None:
        if not self.state:
            return
        self.store.save(asdict(self.state))

    def summary(self) -> str:
        if not self.state:
            return (
                "No project goal yet. Use /start <goal> for a web project or /upgrade <goal> for a repository upgrade."
            )
        state = self.state
        issues = "\n".join(f"- {issue.owner}: {issue.summary[:500]}" for issue in state.issues[:30])
        activity = ", ".join(
            f"{self.team.config.employees[key].name} since {started}"
            for key, started in sorted(state.active_agents.items())
            if key in self.team.config.employees
        )
        paused = (
            "\nRepair paused after repeated failures. Send /feedback after correcting the cause to retry."
            if state.status == "blocked"
            else ""
        )
        return (
            f"Goal: {state.goal}\nMode: {state.mode} | Status: {state.status} | Phase: {state.phase} | "
            f"Review cycle: {state.cycle}\nPhase started: {state.phase_started_at or 'not recorded'}"
            f"\nWorking now: {activity or 'no agent call in progress'}"
            f"\nLast checked: {state.last_checked or 'not yet'}"
            + (f"\nOpen issues:\n{issues}" if issues else "")
            + paused
        )

    def start(self, goal: str, mode: str = "web") -> None:
        if self.task and not self.task.done():
            raise ValueError("A project workflow is already running. Use /work or /stop first.")
        goal = goal.strip()
        if not goal:
            raise ValueError("Describe the project goal after /start.")
        candidate = WorkState(goal=goal, mode=mode, phase="inventory" if mode == "upgrade" else "plan")
        if self.state and self.state.status != "stopped":
            raise ValueError("A saved project is still active. Use /work or /stop before starting another.")
        if not self.lease.acquire():
            raise ValueError("Another process is running this workspace's workflow.")
        self.state = candidate
        self.stop_event.clear()
        self.wake_event.clear()
        try:
            self._save()
            self.task = asyncio.create_task(self.run())
            self.task.add_done_callback(self._finished)
        except Exception:
            self.lease.release()
            raise

    def start_upgrade(self, goal: str) -> None:
        missing = {"architect", "fullstack", "devops", "qa"} - self.team.agents.keys()
        if missing:
            raise ValueError(f"Upgrade workflow requires roles: {', '.join(sorted(missing))}")
        self.start(goal, mode="upgrade")

    def resume(self) -> None:
        if self.state and self.state.status in {"running", "monitoring", "blocked"}:
            if not self.task or self.task.done():
                if not self.lease.acquire():
                    self.team.emit("Another process owns this workspace's workflow; chat remains available.")
                    return
                self.stop_event.clear()
                self.task = asyncio.create_task(self.run())
                self.task.add_done_callback(self._finished)

    async def stop(self) -> None:
        if not self.lease.held and not self.lease.acquire():
            raise ValueError("Another process owns this workspace's workflow.")
        try:
            self.stop_event.set()
            if self.task and not self.task.done():
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
            if self.state:
                self.state.status = "stopped"
                self._save()
        finally:
            self.lease.release()

    def add_feedback(self, message: str) -> None:
        if not self.state or self.state.status == "stopped":
            raise ValueError("No active project goal. Use /start first.")
        if not message.strip() or len(message) > 4000:
            raise ValueError("Feedback must be between 1 and 4,000 characters.")
        if not self.lease.held:
            raise ValueError("Another process may own this workflow; connect to its service first.")
        self.state.feedback.append(message.strip())
        self.state.feedback = self.state.feedback[-20:]
        if self.state.phase == "blocked":
            self.state.repeated_failures = 0
            self.state.issue_signature = ""
            if any(issue.owner == "manager" for issue in self.state.issues):
                self.state.phase = "upgrade_verify" if self.state.mode == "upgrade" else "qa"
            else:
                self.state.phase = "upgrade_repair" if self.state.mode == "upgrade" else "repair"
            self.state.status = "running"
        elif self.state.phase in {"monitor", "upgrade_monitor"}:
            self.state.phase = "upgrade_implement" if self.state.mode == "upgrade" else "qa"
            self.state.status = "running"
        self._save()
        self.wake_event.set()
        if not self.task or self.task.done():
            self.resume()

    def _finished(self, task: asyncio.Task) -> None:
        if task.cancelled():
            if not self.stop_event.is_set():
                self.lease.release()
            return
        error = task.exception()
        try:
            if error:
                if self.state:
                    self.state.status = "blocked"
                    self.state.phase = "blocked"
                    self.state.issues = [Issue("manager", "Workflow error", str(error), "high")]
                    self._save()
                self.team.emit(f"Workflow error: {type(error).__name__}: {error}")
        finally:
            self.lease.release()

    async def _ask(self, key: str, prompt: str, use_tools: bool = True) -> str:
        agent = self.team.agents[key]
        async with self.team.locks[key]:
            if self.state:
                self.state.active_agents[key] = now()
                self._save()
            try:
                answer = await agent.ask(prompt, use_tools=use_tools)
            finally:
                if self.state:
                    self.state.active_agents.pop(key, None)
                    self._save()
        self.team._record(self.team.config.employees[key].name, answer)
        return answer

    async def _sleep_or_wake(self) -> None:
        try:
            await asyncio.wait_for(self.wake_event.wait(), self.team.config.monitor_interval_seconds)
        except TimeoutError:
            pass
        self.wake_event.clear()

    async def run(self) -> None:
        state = self.state
        if not state:
            return
        state.active_agents.clear()
        previous_phase: str | None = None
        last_announced: tuple[str, int] | None = None
        while not self.stop_event.is_set():
            phase = state.phase
            if not state.phase_started_at or (previous_phase is not None and previous_phase != phase):
                state.phase_started_at = now()
            previous_phase = phase
            self._save()
            marker = (phase, state.cycle)
            if marker != last_announced:
                self.team.emit(f"Workflow: {phase} (cycle {state.cycle})")
                last_announced = marker
            if state.mode == "upgrade":
                await self.upgrade.run_phase(state)
            elif phase == "plan":
                state.plan = await self._ask(
                    "manager",
                    f"""
Human project goal: {state.goal}
Make a concise, concrete product brief: scope, user flows, acceptance criteria,
database choice if specified, and shared frontend/backend contract. This is a real
project request, not an example to implement unless the human actually asked for it.
If no database was specified, use SQLite for local development. Do not invent a
PostgreSQL requirement. Ask the human only for a detail that blocks implementation;
otherwise state assumptions and continue.
""",
                    use_tools=False,
                )
                state.phase = "design"
            elif phase == "design":
                state.design = await self._ask(
                    "ux",
                    f"""
Goal: {state.goal}
Lead brief: {state.plan}
Create the UX specification in design/. Include pages, user flows, responsive
behavior, accessibility, and loading/empty/error states. Inspect existing work first.
Report the design file paths and handoff instructions for frontend/backend.
""",
                )
                state.phase = "implement"
            elif phase == "implement":

                async def developer(key: str) -> tuple[str, str]:
                    answer = await self._ask(
                        key,
                        f"""
Goal: {state.goal}
Lead brief: {state.plan}
UX handoff: {state.design}
Human feedback: {state.feedback[-5:]}
Implement your owned part of this website in the real workspace. Coordinate
interfaces by reading the other developer's files and the UX design. For a Django
request, backend owns Django code in backend/; frontend owns templates/static
assets in frontend/. Use the database the human specified, or SQLite for local
development if none was specified. Ensure every referenced app, URL, and page exists;
use dependencies compatible with the local Python version. Report real files changed,
tests run, and the contract the other developer needs.
""",
                    )
                    return key, answer

                results = await asyncio.gather(*(developer(key) for key in ("backend", "frontend")))
                state.implementation = dict(results)
                state.phase = "qa"
            elif phase == "qa":
                async with self.team.preview_lock:
                    previews = await asyncio.to_thread(self.team.preview.start_results)
                preview = "\n".join(item.render() for item in previews)
                checks = await asyncio.to_thread(self.quality.run, previews)
                state.last_checks = checks
                state.last_checked = now()
                state.last_signature = await asyncio.to_thread(self.team.workspace.signature)
                qa_prompt = f"""
Goal: {state.goal}
Acceptance criteria and architecture: {state.plan}
UX design: {state.design}
Developer reports: {state.implementation}
Preview: {preview}
Deterministic check results: {json.dumps(checks)}
Human feedback: {state.feedback[-5:]}
Inspect relevant real files with read tools. Do not edit. Return JSON only:
{{"passed": false, "issues": [
  {{"owner": "frontend", "summary": "...", "evidence": "file/check/behavior", "severity": "high"}}
], "summary": "..."}}
Only pass when acceptance criteria and checks have evidence. Do not invent browser testing.
Use the current check results and current files as evidence. Do not repeat a prior
missing-module, failed-build, or HTTP 404 finding when the current check for that
module, build, or URL succeeds. Report remaining gaps with specific current evidence.
"""
                try:
                    state.qa_report = await self._ask("qa", qa_prompt)
                except (APITimeoutError, ToolRoundLimitError):
                    self.team.emit("Detailed QA took too long; retrying with current check results.")
                    concise_checks = [
                        {"name": check["name"], "returncode": check["returncode"], "output": check["output"][-350:]}
                        for check in checks
                    ]
                    state.qa_report = await self._ask(
                        "qa",
                        "Assess the latest deterministic checks for this project. "
                        "Return one concise JSON object with passed, issues, and summary. "
                        "Do not repeat stale findings or claim browser testing. "
                        f"Goal: {state.goal[:500]}. Checks: {json.dumps(concise_checks)}",
                        use_tools=False,
                    )
                report: QAReport | None = None
                parse_error = None
                for attempt in range(2):
                    try:
                        report = QAReport.model_validate_json(state.qa_report)
                        break
                    except ValidationError as exc:
                        parse_error = exc
                        if attempt == 0:
                            state.qa_report = await self._ask(
                                "qa",
                                "Restate this assessment as one valid JSON object with passed, issues, and summary. "
                                f"No Markdown. Assessment: {state.qa_report[-4000:]}",
                                use_tools=False,
                            )
                try:
                    if report is None:
                        raise ValueError(f"Invalid QA report after retry: {parse_error}")
                    issues = [Issue(**item.model_dump()) for item in report.issues]
                except (ValueError, TypeError, KeyError) as exc:
                    state.issues = [Issue("manager", "QA report needs clarification", str(exc), "high")]
                    state.phase = "blocked"
                    state.status = "blocked"
                    self.team.emit("QA report could not be interpreted; workflow is waiting for feedback.")
                    self._save()
                    continue
                for check in checks:
                    if check["returncode"] != 0:
                        owner = "frontend" if check["name"].startswith("frontend") else "backend"
                        issues.append(Issue(owner, f"Check failed: {check['name']}", check["output"][-1200:], "high"))
                if not checks:
                    issues.append(Issue("backend", "No runnable application or tests detected", preview, "high"))
                elif not any(
                    check["name"].endswith(("_build", "_test", "_tests", "_check", "_migrations")) for check in checks
                ):
                    owner = (
                        "frontend" if (self.team.workspace.root / "frontend" / "package.json").is_file() else "backend"
                    )
                    issues.append(
                        Issue(
                            owner,
                            "No automated build or test check",
                            "Only HTTP or setup checks were available",
                            "medium",
                        )
                    )
                if not report.passed and not issues:
                    issues.append(
                        Issue("manager", "QA rejected without actionable issues", state.qa_report[-1200:], "high")
                    )
                state.issues = issues
                if report.passed and not issues:
                    state.status = "monitoring"
                    state.phase = "monitor"
                    self.team.emit(f"QA passed. Local preview: {preview}")
                else:
                    signature = json.dumps(sorted((item.owner, item.summary) for item in issues))
                    state.repeated_failures = state.repeated_failures + 1 if signature == state.issue_signature else 0
                    state.issue_signature = signature
                    if state.repeated_failures >= self.team.config.max_repair_attempts:
                        state.status = "blocked"
                        state.phase = "blocked"
                        self.team.emit(
                            "Same QA issues repeated; waiting for your direction instead of rewriting blindly."
                        )
                    else:
                        state.status = "running"
                        state.phase = "repair"
                        self.team.emit(f"QA found {len(issues)} issue(s); routing them to their owners.")
                state.cycle += 1
            elif phase == "repair":
                owners = tuple(dict.fromkeys(issue.owner for issue in state.issues))
                if "manager" in owners:
                    state.phase = "blocked"
                    state.status = "blocked"
                else:
                    before = await asyncio.to_thread(self.team.workspace.signature)
                    for owner in owners:
                        own_issues = [asdict(issue) for issue in state.issues if issue.owner == owner]
                        await self._ask(
                            owner,
                            f"""
Goal: {state.goal}
QA found these issues assigned to you: {json.dumps(own_issues)}
Lead brief: {state.plan}
UX design: {state.design}
Human feedback: {state.feedback[-5:]}
Inspect the real files and fix only your owned issues. Run relevant checks.
Report exactly what changed and what remains unresolved.
""",
                        )
                    if await asyncio.to_thread(self.team.workspace.signature) == before:
                        state.phase = "blocked"
                        state.status = "blocked"
                        self.team.emit("No files changed during repair; waiting for your direction.")
                    else:
                        state.phase = "qa"
            elif phase == "monitor":
                await self._sleep_or_wake()
                if not self.stop_event.is_set():
                    state.phase = "qa"
                    state.status = "running"
            elif phase == "blocked":
                await self._sleep_or_wake()
                if state.phase == "blocked":
                    checks = await asyncio.to_thread(self.quality.run)
                    previous = state.last_checks
                    signature = await asyncio.to_thread(self.team.workspace.signature)
                    state.last_checks = checks
                    state.last_checked = now()
                    check_status = [(check["name"], check["returncode"]) for check in checks]
                    previous_status = [(check["name"], check["returncode"]) for check in previous]
                    if check_status != previous_status or signature != state.last_signature:
                        state.phase = "qa"
                        state.status = "running"
                    state.last_signature = signature
            else:
                raise ValueError(f"Unknown workflow phase: {phase}")
            self._save()
