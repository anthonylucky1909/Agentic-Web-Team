from __future__ import annotations

import os
import signal
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    output: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run_command(
    command: Sequence[str],
    cwd: Path,
    timeout: int,
    env: Mapping[str, str] | None = None,
    output_limit: int = 20_000,
) -> CommandResult:
    if not command:
        raise ValueError("Command must not be empty")
    try:
        with tempfile.TemporaryFile() as stream:
            process = subprocess.Popen(
                list(command),
                cwd=cwd,
                env=dict(env) if env is not None else None,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            timed_out = False
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - output_limit))
            output = stream.read().decode("utf-8", errors="replace").strip()
            if size > output_limit:
                output = "[output truncated]\n" + output
            if timed_out:
                output = f"Command timed out after {timeout}s.\n{output}".strip()
            return CommandResult(124 if timed_out else process.returncode, output, timed_out)
    except OSError as exc:
        return CommandResult(127, str(exc))
