"""Lightweight abstraction around language model calls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List


@dataclass
class LLMMessage:
    """Simple chat message container used for tests and mocks."""

    role: str
    content: str


@dataclass
class LLMClient:
    """Tiny synchronous client that simulates deterministic completions."""

    model_name: str = "planner"
    temperature: float = 0.0

    def complete(self, prompt: str) -> str:
        """Return a deterministic "completion" for scaffolding purposes."""

        prompt = prompt.strip()
        if not prompt:
            return ""
        return f"[{self.model_name}] {prompt}"

    def chat(self, messages: Iterable[LLMMessage]) -> str:
        """Return a concatenation of the provided messages."""

        transcript: List[str] = [f"{message.role}: {message.content}" for message in messages]
        summary = "\n".join(transcript)
        if not summary:
            return ""
        return f"[{self.model_name}] {summary}"


__all__ = ["LLMClient", "LLMMessage"]
