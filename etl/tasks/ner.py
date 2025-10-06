"""Lightweight NER helpers mirrored from the legal MCP server."""

from __future__ import annotations

import re
from typing import Dict, List

STATUTE_FL = re.compile(r"\bFla\.?\s+Stat\.?\s*§?\s*([\d\.]+[A-Za-z\-]*)", re.IGNORECASE)
CASE_CITE = re.compile(r"\b(\d{1,3}\s+So\.?\s?\d{1,3}\s+\d{1,4})\b")
DATE_RX = re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s+\d{1,2},\s+\d{4}\b", re.IGNORECASE)
PARTY_RX = re.compile(r"\b(Petitioner|Respondent|Mother|Father|Minor Child)\b", re.IGNORECASE)
JUDGE_RX = re.compile(r"\bJudge\s+[A-Z][a-zA-Z\-]+\b")


def extract_ner_tags(text: str) -> List[Dict[str, str]]:
    """Extract lightweight entity tags from the provided text."""

    tags: List[Dict[str, str]] = []
    for match in STATUTE_FL.finditer(text):
        tags.append({"tag_key": "statute_cited", "tag_value": f"Fla. Stat. {match.group(1)}"})
    for match in CASE_CITE.finditer(text):
        tags.append({"tag_key": "case_cited", "tag_value": match.group(1)})
    for match in DATE_RX.finditer(text):
        tags.append({"tag_key": "date_mentioned", "tag_value": match.group(0)})
    for match in PARTY_RX.finditer(text):
        tags.append({"tag_key": "party_term", "tag_value": match.group(1).title()})
    for match in JUDGE_RX.finditer(text):
        tags.append({"tag_key": "judge", "tag_value": match.group(0)})
    return tags

