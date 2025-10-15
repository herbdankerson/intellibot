"""Workflow designer agent that derives a deterministic task graph from a plan."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Iterable, List

from ..schemas import Plan, PlanTask, TaskStatus


class WorkflowNodeType(str, Enum):
    """Categorization of workflow nodes for execution routing."""

    DB_QUERY = "db_query"
    GRAPH_QUERY = "graph_query"
    WEB_FETCH = "web_fetch"
    CUSTOM_AGENT = "custom_agent"
    SUMMARIZER = "summarizer"


@dataclass
class WorkflowNode:
    """Node in the deterministic workflow graph."""

    id: str
    task: PlanTask
    node_type: WorkflowNodeType
    dependencies: List[str] = field(default_factory=list)
    conditions: List["BranchCondition"] = field(default_factory=list)
    allowed_dependency_statuses: Dict[str, set[TaskStatus]] = field(
        default_factory=dict
    )
    max_attempts: int = 1
    retry_delay_seconds: float = 0.0

    def dependency_statuses(self, dependency: str) -> set[TaskStatus]:
        return self.allowed_dependency_statuses.get(dependency, {TaskStatus.COMPLETED})

    def should_run(self, status_map: Dict[str, TaskStatus]) -> bool:
        if not self.conditions:
            return True
        for condition in self.conditions:
            if not condition.is_satisfied(status_map):
                return False
        return True


@dataclass
class BranchCondition:
    """Conditional routing instruction for a workflow node."""

    task_id: str
    acceptable_statuses: set[TaskStatus]

    def is_satisfied(self, status_map: Dict[str, TaskStatus]) -> bool:
        actual = status_map.get(self.task_id)
        if actual is None:
            return False
        return actual in self.acceptable_statuses


@dataclass
class WorkflowGraph:
    """Directed acyclic graph representing the execution plan."""

    nodes: Dict[str, WorkflowNode]

    def ordered(self) -> List[WorkflowNode]:
        """Return nodes sorted topologically for execution."""

        in_degree: Dict[str, int] = {node_id: 0 for node_id in self.nodes}
        for node in self.nodes.values():
            for dep in node.dependencies:
                if dep in in_degree:
                    in_degree[node.id] += 1
        queue: List[str] = [node_id for node_id, degree in in_degree.items() if degree == 0]
        ordered: List[WorkflowNode] = []
        index = 0
        while index < len(queue):
            current_id = queue[index]
            index += 1
            node = self.nodes[current_id]
            ordered.append(node)
            for downstream in self._dependents(current_id):
                in_degree[downstream] -= 1
                if in_degree[downstream] == 0:
                    queue.append(downstream)
        if len(ordered) != len(self.nodes):
            raise ValueError("Workflow graph contains a cycle or unreachable nodes")
        return ordered

    def _dependents(self, node_id: str) -> Iterable[str]:
        for candidate in self.nodes.values():
            if node_id in candidate.dependencies:
                yield candidate.id


class WorkflowDesigner:
    """Translate a plan into an executable workflow graph."""

    def build_graph(self, plan: Plan) -> WorkflowGraph:
        nodes: Dict[str, WorkflowNode] = {}
        for task in plan.tasks:
            node_type = self._classify(task)
            dependencies = [dep for dep in task.depends_on if dep]
            conditions = self._parse_conditions(task)
            for condition in conditions:
                if condition.task_id and condition.task_id not in dependencies:
                    dependencies.append(condition.task_id)
            allowed_statuses = self._build_allowed_status_map(dependencies, conditions)
            max_attempts, delay_seconds = self._parse_retry(task)
            if not dependencies and nodes:  # maintain sequential safety if planner omitted deps
                previous_id = self._previous_task_id(plan, task.id)
                if previous_id and previous_id not in dependencies:
                    dependencies.append(previous_id)
            nodes[task.id] = WorkflowNode(
                id=task.id,
                task=task,
                node_type=node_type,
                dependencies=dependencies,
                conditions=conditions,
                allowed_dependency_statuses=allowed_statuses,
                max_attempts=max_attempts,
                retry_delay_seconds=delay_seconds,
            )
        return WorkflowGraph(nodes=nodes)

    def _previous_task_id(self, plan: Plan, current_id: str) -> str | None:
        ids = [task.id for task in plan.tasks]
        try:
            index = ids.index(current_id)
        except ValueError:
            return None
        if index > 0:
            return ids[index - 1]
        return None

    def _classify(self, task: PlanTask) -> WorkflowNodeType:
        tool = task.tool.lower()
        if tool.startswith("db"):
            return WorkflowNodeType.DB_QUERY
        if tool.startswith("graph"):
            return WorkflowNodeType.GRAPH_QUERY
        if tool.startswith("neo4j"):
            return WorkflowNodeType.GRAPH_QUERY
        if tool.startswith("legal"):
            return WorkflowNodeType.DB_QUERY
        if tool.startswith("web") or "search" in tool:
            return WorkflowNodeType.WEB_FETCH
        if tool.startswith("agent"):
            return WorkflowNodeType.CUSTOM_AGENT
        return WorkflowNodeType.SUMMARIZER

    def _parse_conditions(self, task: PlanTask) -> List[BranchCondition]:
        if not isinstance(task.inputs, dict):
            return []
        raw_conditions = task.inputs.get("when") or task.inputs.get("conditions")
        if raw_conditions is None:
            return []
        if isinstance(raw_conditions, dict):
            raw_conditions = [raw_conditions]
        if not isinstance(raw_conditions, list):
            return []
        conditions: List[BranchCondition] = []
        for entry in raw_conditions:
            if not isinstance(entry, dict):
                continue
            task_id = str(entry.get("task") or entry.get("depends_on") or "").strip()
            if not task_id:
                continue
            acceptable = self._normalize_statuses(entry.get("status") or entry.get("state"))
            if not acceptable:
                acceptable = {TaskStatus.COMPLETED}
            conditions.append(BranchCondition(task_id=task_id, acceptable_statuses=acceptable))
        return conditions

    def _normalize_statuses(self, raw_status) -> set[TaskStatus]:
        if raw_status is None:
            return {TaskStatus.COMPLETED}
        statuses: set[TaskStatus] = set()
        raw_items = raw_status if isinstance(raw_status, list) else [raw_status]
        for item in raw_items:
            if item is None:
                continue
            value = str(item).strip().lower()
            if value in {"success", "successful", "completed", "complete"}:
                statuses.add(TaskStatus.COMPLETED)
            elif value in {"failure", "failed", "error"}:
                statuses.add(TaskStatus.FAILED)
            elif value in {"skipped", "skip"}:
                statuses.add(TaskStatus.SKIPPED)
            elif value in {"any", "either", "all"}:
                statuses.update({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.SKIPPED})
        if not statuses:
            statuses.add(TaskStatus.COMPLETED)
        return statuses

    def _parse_retry(self, task: PlanTask) -> tuple[int, float]:
        max_attempts = 1
        delay_seconds = 0.0
        if not isinstance(task.inputs, dict):
            return max_attempts, delay_seconds
        retry_directive = task.inputs.get("retry") or task.inputs.get("retries")
        if retry_directive is None:
            return max_attempts, delay_seconds
        if isinstance(retry_directive, int):
            max_attempts = max(1, retry_directive)
            return max_attempts, delay_seconds
        if not isinstance(retry_directive, dict):
            return max_attempts, delay_seconds
        max_attempts = max(1, int(retry_directive.get("max_attempts") or retry_directive.get("max") or 1))
        delay_raw = retry_directive.get("delay_seconds") or retry_directive.get("backoff_seconds")
        if delay_raw is not None:
            try:
                delay_seconds = max(0.0, float(delay_raw))
            except (TypeError, ValueError):
                delay_seconds = 0.0
        return max_attempts, delay_seconds

    def _build_allowed_status_map(
        self, dependencies: List[str], conditions: List[BranchCondition]
    ) -> Dict[str, set[TaskStatus]]:
        allowed: Dict[str, set[TaskStatus]] = {dep: {TaskStatus.COMPLETED} for dep in dependencies}
        for condition in conditions:
            allowed.setdefault(condition.task_id, set()).update(condition.acceptable_statuses)
        return allowed


__all__ = [
    "WorkflowDesigner",
    "WorkflowGraph",
    "WorkflowNode",
    "BranchCondition",
    "WorkflowNodeType",
]
