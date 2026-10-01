from __future__ import annotations

import asyncio
import sys
import time
from typing import TYPE_CHECKING

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from .service import ServiceCommandError, ServiceUnavailable

if TYPE_CHECKING:
    from .orchestrator import SoftwareTeam


COMMANDS = (
    "/start <goal>, /upgrade <goal>, /work, /feedback <note>, /team, /status, /cancel, "
    "/files, /show, /run, /stop, /stop-preview, /remember, /exit"
)


class TerminalSession:
    def __init__(self, team: SoftwareTeam, console: Console | None = None):
        self.team = team
        self.console = console or Console()

    def notice(self, message: str) -> None:
        self.console.print(message)

    def agent_started(self, name: str) -> None:
        self.notice(f"{name} is replying...")

    def agent_reply(self, name: str, role: str, answer: str) -> None:
        self.console.print(Panel(Markdown(answer), title=f"{name} | {role}"))

    def preview(self, report: str) -> None:
        self.console.print(Panel(report, title="Local preview"))

    async def command(self, line: str) -> bool:
        name, _, argument = line.partition(" ")
        name = name.lower()
        team = self.team
        if name in {"/exit", "/quit"}:
            return False
        if team.service_connected and name in {"/start", "/upgrade", "/work", "/feedback", "/stop", "/stop-preview"}:
            try:
                self.notice(await team._service_request(name[1:], argument))
                return True
            except ServiceCommandError as exc:
                self.notice(str(exc))
                return True
            except ServiceUnavailable:
                team.service_connected = False
                self.notice("Team service disconnected; continuing in this terminal.")
                team.workflow.state = team.workflow._load()
                team.workflow.resume()
        if name in {"/team", "/employees"}:
            for key, spec in team.config.employees.items():
                self.notice(f"@{key} ({spec.name}) | {spec.role}")
        elif name == "/status":
            self.notice(f"{len(team.pending)} active request(s). Workspace: {team.workspace.root}")
            if team.service_connected:
                try:
                    self.notice(await team._service_request("work"))
                except (ServiceUnavailable, ServiceCommandError) as exc:
                    self.notice(str(exc))
            else:
                self.notice(team.workflow.summary())
            for label, started in team.task_info.values():
                self.notice(f"  {label}: {int(time.monotonic() - started)}s elapsed")
        elif name == "/start":
            try:
                team.workflow.start(argument)
                self.notice("Project started. Use /work to see progress; chat with teammates at any time.")
            except ValueError as exc:
                self.notice(str(exc))
        elif name == "/upgrade":
            try:
                team.workflow.start_upgrade(argument)
                self.notice("Repository upgrade started. Use /work to see progress.")
            except ValueError as exc:
                self.notice(str(exc))
        elif name == "/work":
            self.notice(team.workflow.summary())
        elif name == "/feedback":
            try:
                team.workflow.add_feedback(argument)
                self.notice("Feedback sent to the project workflow.")
            except ValueError as exc:
                self.notice(str(exc))
        elif name == "/files":
            self.notice(team.workspace.list_files())
        elif name == "/show":
            self.notice(team.workspace.read_file(argument.strip()) if argument.strip() else "Usage: /show <file>")
        elif name == "/run":
            team._track(asyncio.create_task(team._start_preview()), "/run")
        elif name == "/cancel":
            active = list(team.pending)
            for task in active:
                task.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)
            self.notice(f"Canceled {len(active)} request(s).")
        elif name == "/stop":
            try:
                await team.workflow.stop()
                self.notice(await asyncio.to_thread(team.preview.stop))
                self.notice("Project workflow stopped.")
            except ValueError as exc:
                self.notice(str(exc))
        elif name == "/stop-preview":
            self.notice(await asyncio.to_thread(team.preview.stop))
        elif name == "/remember":
            if not argument.strip():
                self.notice("Usage: /remember <project decision or correction>")
            else:
                try:
                    self.notice(team.workspace.remember_note(argument))
                except Exception as exc:
                    self.notice(f"[red]{exc}[/red]")
        else:
            self.notice(f"Commands: {COMMANDS}")
        return True

    async def chat(self, initial_message: str | None = None) -> None:
        team = self.team
        self.console.print(
            Panel(
                f"Workspace: {team.workspace.root}\n"
                "Use /start <goal> for a web project or /upgrade <goal> to improve a repository. "
                "Chat with any teammate from /team at any time.\n"
                f"Commands: {COMMANDS}",
                title="Agentic Web Team",
            )
        )
        team.service_connected = await team.service_client.available()
        if not team.service_connected:
            team.workflow.resume()
        if initial_message:
            team.submit(initial_message)
        try:
            if sys.stdin.isatty() and sys.stdout.isatty():
                session: PromptSession[str] = PromptSession()
                with patch_stdout():
                    await self._input_loop(
                        lambda: session.prompt_async(f"You -> {team._target_name(team.active_targets)}> ")
                    )
            else:
                await self._input_loop(
                    lambda: asyncio.to_thread(self.console.input, f"You -> {team._target_name(team.active_targets)}> ")
                )
        finally:
            if team.pending:
                self.notice(f"Waiting for {len(team.pending)} active request(s) to finish...")
                await asyncio.gather(*team.pending, return_exceptions=True)
            if not team.service_connected:
                if team.workflow.task and not team.workflow.task.done():
                    team.workflow.task.cancel()
                    await asyncio.gather(team.workflow.task, return_exceptions=True)
                await asyncio.to_thread(team.preview.stop)
            if team.history_path.is_file():
                self.notice(f"Conversation saved: {team.history_path}")

    async def _input_loop(self, read_line) -> None:
        while True:
            try:
                line = (await read_line()).strip()
            except (EOFError, KeyboardInterrupt):
                self.notice("")
                break
            if not line:
                continue
            if line.startswith("/"):
                if not await self.command(line):
                    break
            else:
                self.team.submit(line)
