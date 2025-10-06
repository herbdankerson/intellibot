"""Tests for the custom agent runner parsing logic."""

from src.my_agentic_chatbot.agents import AgentDescriptor, get_agent_config
from src.my_agentic_chatbot.agents.runtime import CustomAgentRunner
from src.my_agentic_chatbot.constants import (
    DEFAULT_CUSTOM_AGENT_BUDGET_TOKENS,
    DEFAULT_CUSTOM_AGENT_TIMEOUT_SECONDS,
)
from src.my_agentic_chatbot.schemas import PlanTask


class StubLLMClient:
    def __init__(self, payload: str) -> None:
        self.payload = payload

    def chat(self, messages, agent_config=None):  # type: ignore[override]
        return self.payload


def _descriptor() -> AgentDescriptor:
    return AgentDescriptor(
        tool="agent-test",
        description="",
        runtime="llm",
        default_budget_tokens=DEFAULT_CUSTOM_AGENT_BUDGET_TOKENS,
        default_timeout_seconds=DEFAULT_CUSTOM_AGENT_TIMEOUT_SECONDS,
        agent_config_name="cheap-worker",
    )


def _task() -> PlanTask:
    return PlanTask(
        id="agent-test",
        requirement_id="req-1",
        description="Collect facts",
        tool="agent-test",
        priority=1,
        budget_tokens=DEFAULT_CUSTOM_AGENT_BUDGET_TOKENS,
        timeout_seconds=DEFAULT_CUSTOM_AGENT_TIMEOUT_SECONDS,
        inputs={"topic": "demo"},
        depends_on=[],
    )


def test_custom_agent_runner_parses_structured_items() -> None:
    client = StubLLMClient(
        payload="""
        {
          "items": [
            {"id": "demo-1", "content": "Result one", "source": "demo"},
            {"content": "Result two", "source": "demo"}
          ]
        }
        """
    )
    runner = CustomAgentRunner(
        descriptor=_descriptor(),
        agent_config=get_agent_config("cheap-worker"),
        client=client,
    )
    items = runner.execute(_task())
    assert len(items) == 2
    assert items[0].id == "demo-1"
    assert items[0].metadata["agent"] == "cheap-worker"


def test_custom_agent_runner_handles_malformed_payload() -> None:
    client = StubLLMClient(payload="not json")
    runner = CustomAgentRunner(
        descriptor=_descriptor(),
        agent_config=get_agent_config("cheap-worker"),
        client=client,
    )
    items = runner.execute(_task())
    assert len(items) == 1
    assert items[0].content
