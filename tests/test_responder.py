"""Tests for the responder component."""

import json

from src.my_agentic_chatbot.response.responder import Responder
from src.my_agentic_chatbot.schemas import EvidenceItem, EvidencePack, Finding


class StubResponderClient:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def chat(self, messages):
        return json.dumps(self.payload)


def test_responder_includes_citations() -> None:
    pack = EvidencePack(
        items=[
            EvidenceItem(id="db-1", source="db", content="Snippet about policies."),
            EvidenceItem(id="db-2", source="db", content="Another snippet."),
        ],
        summary="Policies summary",
    )
    payload = {
        "answer": "Policies summary [db-1] [db-2]",
        "citations": ["db-1", "db-2"],
        "confidence": 0.7,
        "unresolved_questions": [],
    }
    responder = Responder(client=StubResponderClient(payload))
    findings = [
        Finding(
            id="finding-1",
            requirement_id="req-1",
            key="Policies summary",
            value="Snippet about policies.",
            confidence=0.8,
            evidence_ids=["db-1"],
            metadata={},
        ),
        Finding(
            id="finding-2",
            requirement_id="req-1",
            key="Policies summary",
            value="Another snippet.",
            confidence=0.7,
            evidence_ids=["db-2"],
            metadata={},
        ),
    ]
    response = responder.respond(
        "Summarize policies",
        pack,
        findings=findings,
        acceptance_criteria=["Summarize policies"],
        open_questions=[],
    )
    assert response.citations == ["db-1", "db-2"]
    assert response.answer.startswith("Policies summary")
    assert response.confidence == 0.7
    assert not response.unresolved_questions
