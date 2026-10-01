from __future__ import annotations

from typing import Any

from openai import AsyncOpenAI


class LocalLLM:
    def __init__(self, base_url: str, api_key: str, model: str, temperature: float = 0.2):
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=180, max_retries=1)
        self.model = model
        self.temperature = temperature

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        reasoning_effort: str = "low",
        max_tokens: int | None = None,
    ):
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "reasoning_effort": reasoning_effort,
        }
        if tools:
            kwargs["tools"] = tools
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        return await self.client.chat.completions.create(**kwargs)
