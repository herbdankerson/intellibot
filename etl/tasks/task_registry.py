"""Helpers that parse task manager documents and sync them into ParadeDB.

The helpers convert Markdown documents into structured project/task records.
Each public function is designed so Prefect flows can orchestrate them while
keeping the logic testable outside of Prefect.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from uuid import uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection


@dataclass
class ParsedProject:
    """Parsed project metadata extracted from a Markdown task document."""

    project_key: str
    name: str
    description: Optional[str]
    tags: List[str] = field(default_factory=list)
    related_scopes: List[str] = field(default_factory=list)
    flags: Dict[str, str] = field(default_factory=dict)
    raw_metadata: Dict[str, str] = field(default_factory=dict)


@dataclass
class ParsedTask:
    """Single task row parsed from the task registry table."""

    task_key: str
    title: str
    status: str
    task_type: Optional[str]
    priority: Optional[str]
    dependencies: List[str]
    owner: Optional[str]
    notes: Optional[str]
    raw_row: Dict[str, str] = field(default_factory=dict)


@dataclass
class ParsedTasksDocument:
    """Aggregate of the parsed project, tasks, and DAG edges."""

    project: ParsedProject
    tasks: List[ParsedTask]
    edges: List[Tuple[str, str]]


SECTION_PATTERN = re.compile(r"^## (?P<title>.+)$", re.MULTILINE)


def _split_sections(markdown: str) -> Dict[str, str]:
    matches = list(SECTION_PATTERN.finditer(markdown))
    sections: Dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        title = match.group("title").strip()
        sections[title] = markdown[start:end].strip()
    return sections


def _parse_document_metadata(section: str) -> Dict[str, str]:
    metadata: Dict[str, str] = {}
    bullet_pattern = re.compile(r"- `(?P<key>[^`]+)`: ?(?P<value>.+)")
    for raw_line in section.splitlines():
        line = raw_line.strip()
        if not line.startswith("-"):
            continue
        match = bullet_pattern.match(line)
        if not match:
            continue
        key = match.group("key").strip()
        value = match.group("value").strip()
        value = value.strip()
        if value.startswith("`") and value.endswith("`"):
            value = value[1:-1]
        metadata[key.lower()] = value
    return metadata


def _parse_list(value: Optional[str]) -> List[str]:
    if not value:
        return []
    parts = [item.strip(" `") for item in value.split(",")]
    return [item for item in (part.strip() for part in parts) if item]


def _normalise_task_key(value: str) -> str:
    return value.strip().strip("`").upper()


def _parse_task_registry(section: str) -> List[ParsedTask]:
    lines = [
        line.strip()
        for line in section.splitlines()
        if line.strip().startswith("|")
    ]
    if len(lines) < 2:
        return []

    header = [column.strip() for column in lines[0].strip("|").split("|")]
    tasks: List[ParsedTask] = []
    for line in lines[2:]:  # Skip header + divider
        if not line or set(line) <= {"|", "-"}:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        while len(cells) < len(header):
            cells.append("")
        row = dict(zip(header, cells))
        task_key = _normalise_task_key(row.get("Task ID", ""))
        if not task_key:
            continue
        dependencies = _parse_list(row.get("Dependencies"))
        tasks.append(
            ParsedTask(
                task_key=task_key,
                title=row.get("Title", ""),
                status=row.get("Status", "").lower(),
                task_type=row.get("Type"),
                priority=row.get("Priority"),
                dependencies=[_normalise_task_key(dep) for dep in dependencies],
                owner=row.get("Owner"),
                notes=row.get("Notes"),
                raw_row=row,
            )
        )
    return tasks


def _parse_dependency_graph(section: str) -> List[Tuple[str, str]]:
    edges: List[Tuple[str, str]] = []
    for raw_line in section.splitlines():
        line = raw_line.strip()
        if not line.startswith("-"):
            continue
        content = line.lstrip("- ").strip()
        if "←" not in content:
            continue
        target_raw, sources_raw = content.split("←", 1)
        target = _normalise_task_key(target_raw)
        for source_part in sources_raw.split(","):
            dependency = _normalise_task_key(source_part)
            if dependency and target:
                edges.append((dependency, target))
    return edges


def _extract_description(markdown: str) -> Optional[str]:
    for raw_line in markdown.splitlines():
        if raw_line.startswith(">"):
            return raw_line.lstrip("> ").strip()
    return None


def parse_task_document(markdown: str) -> Optional[ParsedTasksDocument]:
    sections = _split_sections(markdown)
    registry_section = sections.get("Task Registry")
    if not registry_section:
        return None

    tasks = _parse_task_registry(registry_section)
    if not tasks:
        return None

    metadata_section = sections.get("Document Metadata", "")
    metadata = _parse_document_metadata(metadata_section)
    doc_id = metadata.get("doc_id") or metadata.get("project_id")
    if not doc_id:
        # Fallback to the document title if doc_id missing.
        title_match = re.match(r"# (?P<title>.+)", markdown)
        doc_id = (title_match.group("title") if title_match else "TASK-DOC").upper().replace(" ", "-")

    project_name_match = re.match(r"# (?P<title>.+)", markdown)
    project_name = project_name_match.group("title").strip() if project_name_match else doc_id
    description = _extract_description(markdown)
    flags = {}
    raw_flags = metadata.get("flags")
    if raw_flags:
        for flag in _parse_list(raw_flags):
            if "=" in flag:
                key, value = flag.split("=", 1)
                flags[key.strip()] = value.strip()
            else:
                flags[flag] = "true"

    project = ParsedProject(
        project_key=doc_id,
        name=project_name,
        description=description,
        tags=_parse_list(metadata.get("tags")),
        related_scopes=_parse_list(metadata.get("related_scopes")),
        flags=flags,
        raw_metadata=metadata,
    )

    dependency_section = sections.get("Dependency Graph", "")
    edges = _parse_dependency_graph(dependency_section)
    for task in tasks:
        for dep in task.dependencies:
            edges.append((dep, task.task_key))

    unique_edges = list({(dep, target) for dep, target in edges if dep and target})
    return ParsedTasksDocument(
        project=project,
        tasks=tasks,
        edges=unique_edges,
    )


def sync_task_document(
    conn: Connection,
    *,
    document_id: str,
    ingest_item_id: str,
    markdown: str,
    is_dev: bool,
) -> Optional[ParsedTasksDocument]:
    """Update ParadeDB task tables based on the supplied Markdown content."""

    parsed = parse_task_document(markdown)
    if parsed is None:
        return None

    project = parsed.project
    tasks = parsed.tasks
    edges = parsed.edges
    sync_time = datetime.now(timezone.utc).isoformat()

    existing_project = conn.execute(
        text(
            """
            SELECT id, status
            FROM task.projects
            WHERE project_key = :project_key
            """
        ),
        {"project_key": project.project_key},
    ).fetchone()

    project_id = str(existing_project.id) if existing_project else str(uuid4())

    conn.execute(
        text(
            """
            INSERT INTO task.projects (
                id,
                project_key,
                name,
                description,
                status,
                priority,
                document_id,
                metadata,
                tags,
                updated_at
            )
            VALUES (
                :id,
                :project_key,
                :name,
                :description,
                :status,
                :priority,
                :document_id,
                CAST(:metadata AS JSONB),
                :tags,
                NOW()
            )
            ON CONFLICT (project_key) DO UPDATE
            SET
                name = EXCLUDED.name,
                description = EXCLUDED.description,
                status = EXCLUDED.status,
                priority = EXCLUDED.priority,
                document_id = EXCLUDED.document_id,
                metadata = EXCLUDED.metadata,
                tags = EXCLUDED.tags,
                updated_at = NOW()
            """
        ),
        {
            "id": project_id,
            "project_key": project.project_key,
            "name": project.name,
            "description": project.description,
            "status": project.flags.get("status", "active"),
            "priority": project.flags.get("priority"),
            "document_id": document_id,
            "metadata": json.dumps(
                {
                    "raw": project.raw_metadata,
                    "flags": project.flags,
                    "related_scopes": project.related_scopes,
                    "is_dev": is_dev,
                }
            ),
            "tags": project.tags,
        },
    )

    existing_tasks_rows = conn.execute(
        text(
            """
            SELECT id, task_key, status, is_blocked
            FROM task.tasks
            WHERE project_id = :project_id
            """
        ),
        {"project_id": project_id},
    ).fetchall()
    existing_tasks: Dict[str, Tuple[str, str, bool]] = {
        row.task_key: (str(row.id), row.status, bool(row.is_blocked))
        for row in existing_tasks_rows
    }

    upsert_ids: Dict[str, str] = {}
    for task in tasks:
        existing = existing_tasks.get(task.task_key)
        task_id = existing[0] if existing else str(uuid4())
        status_normalised = task.status.lower() or "pending"
        is_blocked = status_normalised == "blocked"

        conn.execute(
            text(
                """
                INSERT INTO task.tasks (
                    id,
                    task_key,
                    project_id,
                    document_id,
                    ingest_item_id,
                    title,
                    status,
                    task_type,
                    priority,
                    owner,
                    notes,
                    metadata,
                    tags,
                    is_blocked,
                    updated_at
                )
                VALUES (
                    :id,
                    :task_key,
                    :project_id,
                    :document_id,
                    :ingest_item_id,
                    :title,
                    :status,
                    :task_type,
                    :priority,
                    :owner,
                    :notes,
                    CAST(:metadata AS JSONB),
                    :tags,
                    :is_blocked,
                    NOW()
                )
                ON CONFLICT (task_key) DO UPDATE
                SET
                    project_id = EXCLUDED.project_id,
                    document_id = EXCLUDED.document_id,
                    ingest_item_id = EXCLUDED.ingest_item_id,
                    title = EXCLUDED.title,
                    status = EXCLUDED.status,
                    task_type = EXCLUDED.task_type,
                    priority = EXCLUDED.priority,
                    owner = EXCLUDED.owner,
                    notes = EXCLUDED.notes,
                    metadata = EXCLUDED.metadata,
                    tags = EXCLUDED.tags,
                    is_blocked = EXCLUDED.is_blocked,
                    updated_at = NOW()
                """
            ),
            {
                "id": task_id,
                "task_key": task.task_key,
                "project_id": project_id,
                "document_id": document_id,
                "ingest_item_id": ingest_item_id,
                "title": task.title,
                "status": status_normalised,
                "task_type": task.task_type,
                "priority": task.priority,
                "owner": task.owner,
                "notes": task.notes,
                "metadata": json.dumps(
                    {
                        "raw": task.raw_row,
                        "dependencies": task.dependencies,
                        "source": "task_registry_document",
                        "last_synced": sync_time,
                        "is_dev": is_dev,
                    }
                ),
                "tags": [],
                "is_blocked": is_blocked,
            },
        )

        if existing is None or existing[1] != status_normalised or existing[2] != is_blocked:
            conn.execute(
                text(
                    """
                    INSERT INTO task.activity_log (
                        id,
                        task_id,
                        project_id,
                        event_type,
                        details,
                        occurred_at,
                        created_at
                    )
                    VALUES (
                        :id,
                        :task_id,
                        :project_id,
                        :event_type,
                        CAST(:details AS JSONB),
                        NOW(),
                        NOW()
                    )
                    """
                ),
                {
                    "id": str(uuid4()),
                    "task_id": task_id,
                    "project_id": project_id,
                    "event_type": "task_synced" if existing is None else "task_status_changed",
                    "details": json.dumps(
                        {
                            "previous_status": existing[1] if existing else None,
                            "new_status": status_normalised,
                            "is_blocked": is_blocked,
                            "dependencies": task.dependencies,
                        }
                    ),
                },
            )
        upsert_ids[task.task_key] = task_id

    missing_keys = sorted(set(existing_tasks) - set(upsert_ids))
    if missing_keys:
        conn.execute(
            text(
                """
                UPDATE task.tasks
                SET status = 'archived',
                    is_blocked = TRUE,
                    updated_at = NOW()
                WHERE task_key IN :keys
                """
            ).bindparams(bindparam("keys", expanding=True)),
            {"keys": tuple(missing_keys)},
        )

    conn.execute(
        text(
            "DELETE FROM task.dag_edges WHERE project_id = :project_id"
        ),
        {"project_id": project_id},
    )

    for dependency, target in edges:
        from_id = upsert_ids.get(dependency) or existing_tasks.get(dependency, (None, "", False))[0]
        to_id = upsert_ids.get(target) or existing_tasks.get(target, (None, "", False))[0]
        if not from_id or not to_id:
            continue
        conn.execute(
            text(
                """
                INSERT INTO task.dag_edges (
                    id,
                    project_id,
                    from_task_id,
                    to_task_id,
                    edge_type,
                    metadata,
                    updated_at
                )
                VALUES (
                    :id,
                    :project_id,
                    :from_task_id,
                    :to_task_id,
                    :edge_type,
                    CAST(:metadata AS JSONB),
                    NOW()
                )
                ON CONFLICT (project_id, from_task_id, to_task_id, edge_type)
                DO UPDATE
                SET metadata = EXCLUDED.metadata,
                    updated_at = NOW()
                """
            ),
            {
                "id": str(uuid4()),
                "project_id": project_id,
                "from_task_id": from_id,
                "to_task_id": to_id,
                "edge_type": "depends_on",
                "metadata": json.dumps({"source": "task_registry_document"}),
            },
        )

    return parsed
