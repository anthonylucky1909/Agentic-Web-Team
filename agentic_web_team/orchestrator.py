from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any

from .agent import EmployeeAgent
from .llm import LocalLLM
from .models import CompanyConfig
from .preview import PreviewManager
from .service import ServiceClient, TeamService
from .storage import HistoryStore
from .terminal import TerminalSession
from .workflow import ContinuousWorkflow
from .workspace import Workspace

logger = logging.getLogger(__name__)


def _is_conversation(message: str) -> bool:
    text = message.strip().lower()
    if re.search(
        r"\b(build|create|make|implement|add|edit|change|update|fix|run|test|show|check|inspect|buat|bangun|tambahkan|ubah|perbaiki|jalankan|tunjukkan|cek|bikin)\b",
        text,
    ):
        return False
    return bool(
        re.match(r"^(hi|hello|hey|halo|hai|pagi|siang|sore|malam)\b", text)
        or re.search(
            r"what is (?:you|your) (?:task|role)|what are you (?:doing|working on)|"
            r"apa tugas|ngapain|sedang apa|lagi apa|introduce yourself|perkenalkan dirimu|"
            r"saya mau diskusi|ingin ngobrol|want to talk|let's discuss",
            text,
        )
    )


class SoftwareTeam:
    def __init__(
        self,
        config: CompanyConfig,
        workspace_path: str | Path,
        create_workspace: bool = False,
        allow_shell: bool = False,
    ):
        self.config = config
        self.workspace = Workspace(
            workspace_path, checks=config.checks, create=create_workspace, allow_shell=allow_shell
        )
        self.llm = LocalLLM(config.base_url, config.api_key, config.model_name, config.temperature)
        self.agents = {
            key: EmployeeAgent(config, spec, self.llm, self.workspace) for key, spec in config.employees.items()
        }
        self.conversations: dict[str, list[dict[str, Any]]] = {}
        self.locks = {key: asyncio.Lock() for key in self.agents}
        self.preview_lock = asyncio.Lock()
        self.preview = PreviewManager(self.workspace.root)
        self.state_dir = self.workspace.root / config.artifact_dir
        self.history_path = self.state_dir / "team_history.jsonl"
        self.history_store = HistoryStore(self.history_path)
        self.notes_path = self.state_dir / "team_notes.md"
        self.history = self._load_history()
        self.pending: set[asyncio.Task] = set()
        self.task_info: dict[asyncio.Task, tuple[str, float]] = {}
        self.active_targets: tuple[str, ...] = ("team",)
        self.workflow = ContinuousWorkflow(self)
        self.service_client = ServiceClient(self.workspace.root)
        self.socket_path = self.service_client.socket_path
        self.service_connected = False
        self.terminal = TerminalSession(self)

    async def _service_request(self, command: str, argument: str = "") -> str:
        return await self.service_client.request(command, argument)

    async def serve(self) -> None:
        await TeamService(self).serve()

    def emit(self, message: str) -> None:
        logger.info("workflow_event: %s", message)
        self.terminal.notice(message)
        self._record("Workflow", message)

    @property
    def active_target(self) -> str:
        return "+".join(self.active_targets)

    def _load_history(self) -> list[dict[str, str]]:
        return self.history_store.recent()

    def _record(self, speaker: str, message: str) -> None:
        event = self.history_store.append(speaker, message)
        self.history.append(event)
        self.history = self.history[-80:]

    def resolve_employee(self, selector: str) -> str:
        normalized = selector.strip().lower()
        matches = [key for key, spec in self.config.employees.items() if normalized in {key.lower(), spec.name.lower()}]
        if len(matches) == 1:
            return matches[0]
        raise ValueError(f"Unknown teammate '{selector}'. Use /team to see names.")

    def _shared_context(self) -> str:
        self.history = self.history_store.recent()
        if self.service_connected:
            self.workflow.state = self.workflow._load()
        notes = self.notes_path.read_text(errors="replace")[-1500:] if self.notes_path.is_file() else "(none)"
        conversation = [event for event in self.history if event["speaker"] not in {"Workflow", "Local preview"}]
        recent = "\n".join(f"{event['speaker']}: {event['message'][:300]}" for event in conversation[-6:])
        return (
            f"Project notes:\n{notes}\n\nCurrent work:\n{self.workflow.summary()}\n\n"
            f"Recent team conversation:\n{recent or '(none)'}"
        )

    async def _answer(
        self,
        key: str,
        message: str,
        auto_preview: bool = True,
        addressed: tuple[str, ...] | None = None,
    ) -> None:
        async with self.locks[key]:
            spec = self.config.employees[key]
            fast = _is_conversation(message)
            self.terminal.agent_started(spec.name)
            try:
                messages = self.conversations.setdefault(key, self.agents[key].new_conversation())
                revision_before = self.workspace.revision
                guidance = (
                    "Reply naturally in 1-3 short sentences. Ask at most one question."
                    if fast
                    else "Use project tools when needed and report actual work."
                )
                group_context = (
                    (
                        "This message also addresses "
                        + ", ".join(self.config.employees[other].name for other in addressed if other != key)
                        + ". Answer for your own role only.\n\n"
                    )
                    if addressed and len(addressed) > 1
                    else ""
                )
                prompt = (
                    f"{self._shared_context()}\n\n{group_context}"
                    f"The human is addressing you now:\n{message}\n\n{guidance}"
                )
                answer = await self.agents[key].chat(messages, prompt, use_tools=not fast, fast=fast)
                if not answer.strip():
                    self.terminal.notice(f"{spec.name} returned an empty reply; retrying briefly...")
                    retry_prompt = (
                        f"The human asks: {message[:1000]}\n"
                        f"Current project: {self.workflow.summary()[:700]}\n"
                        "Reply directly in Indonesian. Return a short, non-empty answer."
                    )
                    answer = await self.agents[key].chat(
                        self.agents[key].new_conversation(), retry_prompt, use_tools=False, fast=True
                    )
                if not answer.strip():
                    answer = (
                        "Model lokal tidak mengirim jawaban. Coba /status untuk melihat permintaan aktif, "
                        "lalu tanyakan lagi setelah QA selesai."
                    )
            except asyncio.CancelledError:
                self.conversations[key] = self.agents[key].new_conversation()
                self.terminal.notice(f"{spec.name}: request canceled.")
                raise
            except Exception as exc:
                self.conversations[key] = self.agents[key].new_conversation()
                logger.exception("Agent reply failed for role %s", key)
                self.terminal.notice(f"{spec.name}: {type(exc).__name__}: {exc}")
                return
            self._record(spec.name, answer)
            self.terminal.agent_reply(spec.name, spec.role, answer)
            if auto_preview and self.workspace.revision > revision_before:
                await self._start_preview()

    async def _start_preview(self) -> None:
        async with self.preview_lock:
            report = (
                (await self._service_request("preview"))
                if self.service_connected
                else await asyncio.to_thread(self.preview.start)
            )
            report = report or "Team service unavailable"
            self._record("Local preview", report)
            self.terminal.preview(report)

    async def _group_turn(self, keys: tuple[str, ...], message: str) -> None:
        before = self.workspace.revision
        await asyncio.gather(*(self._answer(key, message, auto_preview=False, addressed=keys) for key in keys))
        if self.workspace.revision > before:
            await self._start_preview()

    async def _team_turn(self, message: str) -> None:
        if self.workflow.state and self.workflow.state.mode == "upgrade" and self.workflow.state.status != "stopped":
            await self._group_turn(("architect", "fullstack", "devops", "qa"), message)
            return
        await self._group_turn(tuple(key for key in ("ux", "backend", "frontend") if key in self.agents), message)
        if "manager" in self.agents:
            await self._answer("manager", message, auto_preview=False)
        if "qa" in self.agents:
            await self._answer("qa", message, auto_preview=False)

    def submit(self, line: str) -> None:
        message = line.strip()
        selectors = []
        while message.startswith("@"):
            part, _, rest = message.partition(" ")
            selectors.append(part[1:])
            message = rest.strip()
        targets = self.active_targets
        if selectors:
            try:
                targets = tuple(
                    dict.fromkeys(
                        "team" if selector.lower() in {"team", "all"} else self.resolve_employee(selector)
                        for selector in selectors
                    )
                )
            except ValueError as exc:
                self.terminal.notice(str(exc))
                return
            if "team" in targets and len(targets) > 1:
                self.terminal.notice("Use @team by itself, or name individual teammates.")
                return
        self.active_targets = targets
        if not message:
            self.terminal.notice(f"Now chatting with {self._target_name(targets)}.")
            return
        label = " ".join(f"@{target}" for target in targets)
        self._record("You", f"{label} {message}")
        if self.service_connected:
            self.workflow.state = self.workflow._load()
        if (
            self.workflow.state
            and self.workflow.state.status in {"running", "monitoring", "blocked"}
            and not _is_conversation(message)
        ):
            if self.service_connected:
                self._track(asyncio.create_task(self._service_request("feedback", message)), "/feedback")
            else:
                self.workflow.add_feedback(message)
        task = asyncio.create_task(
            self._team_turn(message)
            if targets == ("team",)
            else self._answer(targets[0], message)
            if len(targets) == 1
            else self._group_turn(targets, message)
        )
        self._track(task, label)
        self.terminal.notice(f"Sent to {label}. You can keep typing.")

    def _target_name(self, targets: tuple[str, ...]) -> str:
        return " + ".join("Team" if target == "team" else self.config.employees[target].name for target in targets)

    def _track(self, task: asyncio.Task, label: str) -> None:
        self.pending.add(task)
        self.task_info[task] = (label, time.monotonic())
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        self.pending.discard(task)
        self.task_info.pop(task, None)
        if not task.cancelled():
            error = task.exception()
            if error:
                self.terminal.notice(f"Request failed: {type(error).__name__}: {error}")

    async def _command(self, line: str) -> bool:
        return await self.terminal.command(line)

    async def chat(self, initial_message: str | None = None) -> None:
        await self.terminal.chat(initial_message)

    async def _input_loop(self, read_line) -> None:
        await self.terminal._input_loop(read_line)
