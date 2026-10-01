from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import stat
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from .storage import WorkflowLease

if TYPE_CHECKING:
    from .orchestrator import SoftwareTeam


logger = logging.getLogger(__name__)


class ServiceUnavailable(ConnectionError):
    pass


class ServiceCommandError(RuntimeError):
    pass


def socket_path_for(workspace: Path) -> Path:
    identifier = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"agentic-web-team-{identifier}.sock"


class ServiceClient:
    def __init__(self, workspace: Path):
        self.socket_path = socket_path_for(workspace)

    async def request(self, command: str, argument: str = "") -> str:
        try:
            reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        except OSError as exc:
            raise ServiceUnavailable(f"Team service is not available: {exc}") from exc
        try:
            writer.write((json.dumps({"command": command, "argument": argument}) + "\n").encode())
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=30)
            if not line:
                raise ServiceUnavailable("Team service closed the connection")
            response = json.loads(line)
            if not isinstance(response, dict) or not isinstance(response.get("result"), str):
                raise ServiceUnavailable("Invalid response from team service")
            if response.get("ok") is not True:
                raise ServiceCommandError(response["result"])
            return response["result"]
        except (TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise ServiceUnavailable(f"Team service did not respond: {exc}") from exc
        finally:
            writer.close()
            await writer.wait_closed()

    async def available(self) -> bool:
        try:
            return await self.request("ping") == "ready"
        except (ServiceUnavailable, ServiceCommandError):
            return False


class TeamService:
    def __init__(self, team: SoftwareTeam):
        self.team = team
        self.client = ServiceClient(team.workspace.root)
        self.socket_path = self.client.socket_path
        self.lease = WorkflowLease(team.state_dir / "service.lock")

    async def serve(self) -> None:
        if not self.lease.acquire():
            raise RuntimeError(f"A team service is already running for {self.team.workspace.root}")
        bound = False
        try:
            if await self.client.available():
                raise RuntimeError(f"A team service is already running for {self.team.workspace.root}")
            self._remove_stale_socket()
            server = await asyncio.start_unix_server(self._handle, path=str(self.socket_path))
            bound = True
            os.chmod(self.socket_path, 0o600)
            self.team.workflow.resume()
            self.team.emit(f"Team service ready for {self.team.workspace.root}")
            async with server:
                await server.serve_forever()
        finally:
            try:
                if bound:
                    self.socket_path.unlink(missing_ok=True)
                if self.team.workflow.task and not self.team.workflow.task.done():
                    self.team.workflow.task.cancel()
                    await asyncio.gather(self.team.workflow.task, return_exceptions=True)
                await asyncio.to_thread(self.team.preview.stop)
            finally:
                self.lease.release()

    def _remove_stale_socket(self) -> None:
        try:
            info = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if info.st_uid != os.getuid() or not stat.S_ISSOCK(info.st_mode):
            raise RuntimeError(f"Refusing to remove unexpected socket path: {self.socket_path}")
        self.socket_path.unlink()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            if len(line) > 16_384:
                raise ValueError("Service request is too large")
            request = json.loads(line)
            if not isinstance(request, dict) or not isinstance(request.get("command"), str):
                raise ValueError("Invalid service request")
            argument = request.get("argument", "")
            if not isinstance(argument, str):
                raise ValueError("Service argument must be text")
            result = await self._dispatch(request["command"], argument)
            response = {"ok": True, "result": result}
        except (TimeoutError, ValueError, json.JSONDecodeError) as exc:
            response = {"ok": False, "result": str(exc)}
        except Exception:
            logger.exception("Service command failed")
            response = {"ok": False, "result": "Service command failed; check the service log"}
        try:
            writer.write((json.dumps(response) + "\n").encode())
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def _dispatch(self, command: str, argument: str) -> str:
        if command == "ping":
            return "ready"
        if command == "start":
            self.team.workflow.start(argument)
            return "Project started. Use /work to see progress."
        if command == "upgrade":
            self.team.workflow.start_upgrade(argument)
            return "Repository upgrade started. Use /work to see progress."
        if command == "work":
            return self.team.workflow.summary()
        if command == "feedback":
            self.team.workflow.add_feedback(argument)
            return "Feedback sent to the project workflow."
        if command == "stop":
            await self.team.workflow.stop()
            async with self.team.preview_lock:
                preview = await asyncio.to_thread(self.team.preview.stop)
            return preview + "\nProject workflow stopped."
        if command in {"preview", "stop-preview"}:
            async with self.team.preview_lock:
                action = self.team.preview.start if command == "preview" else self.team.preview.stop
                return await asyncio.to_thread(action)
        raise ValueError(f"Unknown service command: {command}")
