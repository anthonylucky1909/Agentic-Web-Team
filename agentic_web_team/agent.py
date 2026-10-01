from __future__ import annotations

import asyncio
import json
from typing import Any

from .config import load_skill_text
from .llm import LocalLLM
from .models import CompanyConfig, EmployeeSpec
from .workspace import Workspace, execute_tool, tool_schemas


class ToolRoundLimitError(RuntimeError):
    pass


class EmployeeAgent:
    def __init__(self, config: CompanyConfig, spec: EmployeeSpec, llm: LocalLLM, workspace: Workspace):
        self.config = config
        self.spec = spec
        self.llm = llm
        self.workspace = workspace
        self.skill_text = load_skill_text(config, spec.skills)

    def system_prompt(self) -> str:
        terminal_rule = (
            "You may run shell commands through run_terminal. Inspect before changing; avoid destructive operations "
            "and never expose credentials. Commands are audited."
            if self.workspace.allow_shell and "terminal" in self.spec.permissions
            else "Shell access is disabled. Use only configured checks; report commands you could not run."
        )
        return f"""
You are {self.spec.name}, working as {self.spec.role} in a software engineering company.

JOB DESCRIPTION
{self.spec.job_description}

YOUR SKILLS / SOP
{self.skill_text}

OPERATING RULES
- Stay inside your assigned role and task.
- Inspect real repository files instead of inventing file names.
- Use tools when repository facts are needed.
- Never claim a file was changed unless a write tool actually succeeded.
- {terminal_rule}
- Keep changes focused and preserve existing project conventions.
- The team handles web delivery and repository upgrades. The human may message any teammate at any time.
- In chat replies, address the human directly. Do not speak to teammates unless the human asks you to.
- Read the shared team context included with each message. Coordinate interfaces across frontend and backend.
- You may write only in: {", ".join(self.spec.write_paths) if self.spec.write_paths else "(no write access)"}.
- When asked to build, use the available file tools to make real changes. Explain what you actually did.
- Persistent notes are project memory, not model training. Apply prior feedback when relevant.
- When the human corrects a requirement or gives a lasting preference, save that lesson with remember_note.
- Report uncertainty and blockers clearly.
""".strip()

    def new_conversation(self) -> list[dict[str, Any]]:
        return [{"role": "system", "content": self.system_prompt()}]

    async def ask(self, prompt: str, use_tools: bool = True) -> str:
        messages = self.new_conversation()
        return await self.chat(messages, prompt, use_tools=use_tools)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        prompt: str,
        use_tools: bool = True,
        fast: bool = False,
    ) -> str:
        if len(messages) > 60:
            user_turns = [i for i, item in enumerate(messages) if item["role"] == "user"]
            if len(user_turns) > 6:
                messages[1:] = messages[user_turns[-6] :]
        messages.append({"role": "user", "content": prompt})
        tools = (
            tool_schemas(self.spec.permissions, self.config.checks, self.workspace.allow_shell) if use_tools else None
        )

        for _ in range(self.config.max_tool_rounds):
            response = await self.llm.chat(
                messages,
                tools=tools,
                reasoning_effort="none" if fast else "low",
                max_tokens=180 if fast else None,
            )
            msg = response.choices[0].message

            assistant_payload: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
            if msg.tool_calls:
                assistant_payload["tool_calls"] = [tc.model_dump(exclude_none=True) for tc in msg.tool_calls]
            messages.append(assistant_payload)

            if not msg.tool_calls:
                return msg.content or ""

            for call in msg.tool_calls:
                try:
                    args = json.loads(call.function.arguments or "{}")
                    result = await asyncio.to_thread(
                        execute_tool,
                        self.workspace,
                        call.function.name,
                        args,
                        self.spec.permissions,
                        self.spec.write_paths if "write" in self.spec.permissions else None,
                        self.spec.key,
                    )
                except Exception as exc:
                    result = f"TOOL ERROR: {type(exc).__name__}: {exc}"
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": result,
                    }
                )

        raise ToolRoundLimitError(f"{self.spec.name} reached the {self.config.max_tool_rounds}-round tool limit")
