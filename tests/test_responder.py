"""Tests for the responder component."""

from src.my_agentic_chatbot.response.responder import Responder
from src.my_agentic_chatbot.schemas import EvidenceItem, EvidencePack


def test_responder_includes_citations() -> None:
    pack = EvidencePack(
        items=[
            EvidenceItem(id="db-1", source="db", content="Snippet about policies.")
        ],
        summary="Policies summary",
    )
    responder = Responder()
    response = responder.respond("Summarize policies", pack)
    assert response.citations == ["db-1"]
    assert "[db-1]" in response.answer
    assert response.confidence > 0
