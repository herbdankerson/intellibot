"""Tests for the workflow designer agent."""

from src.my_agentic_chatbot.schemas import Plan, PlanTask, Requirement, TaskStatus
from src.my_agentic_chatbot.workflows.workflow_designer import WorkflowDesigner, WorkflowNodeType
from src.my_agentic_chatbot.workflows import policies


def _task(
    task_id: str,
    tool: str,
    depends_on: list[str] | None = None,
    inputs: dict | None = None,
    requirement_id: str = "req-1",
) -> PlanTask:
    return PlanTask(
        id=task_id,
        requirement_id=requirement_id,
        description=f"Execute {tool}",
        tool=tool,
        priority=1,
        budget_tokens=policies.DEFAULT_DB_BUDGET_TOKENS,
        timeout_seconds=policies.DEFAULT_DB_TIMEOUT_SECONDS,
        depends_on=depends_on or [],
        inputs=inputs or {},
    )


def _plan(tasks: list[PlanTask], *, problem: str) -> Plan:
    requirement = Requirement(
        id="req-1",
        question=problem,
        priority=1,
        quality_bar="At least one snippet",
        stop_when_satisfied=True,
        metadata={},
    )
    return Plan(
        problem_spec=problem,
        acceptance_criteria=["Answer should be grounded."],
        requirements=[requirement],
        tasks=tasks,
        findings=[],
        open_questions=[],
        stop_conditions=["Acceptance criteria met"],
    )


def test_designer_enforces_sequential_dependencies() -> None:
    plan = _plan(
        [
            _task("db-1", "db_search"),
            _task("web-1", "web_search"),
        ],
        problem="Answer question",
    )
    graph = WorkflowDesigner().build_graph(plan)
    ordered = [node.id for node in graph.ordered()]
    assert ordered == ["db-1", "web-1"]
    assert graph.nodes["db-1"].node_type == WorkflowNodeType.DB_QUERY
    assert graph.nodes["web-1"].dependencies == ["db-1"]


def test_designer_respects_explicit_dependencies() -> None:
    plan = _plan(
        [
            _task("db-1", "db_search"),
            _task("graph-1", "graph_search", depends_on=["db-1"]),
        ],
        problem="Analyze",
    )
    graph = WorkflowDesigner().build_graph(plan)
    graph_nodes = graph.ordered()
    assert [node.id for node in graph_nodes] == ["db-1", "graph-1"]
    assert graph.nodes["graph-1"].dependencies == ["db-1"]
    assert graph.nodes["graph-1"].node_type == WorkflowNodeType.GRAPH_QUERY


def test_designer_classifies_custom_agents() -> None:
    plan = _plan([
        _task("agent-1", "agent-demo"),
    ], problem="Delegate")
    graph = WorkflowDesigner().build_graph(plan)
    node = graph.nodes["agent-1"]
    assert node.node_type == WorkflowNodeType.CUSTOM_AGENT


def test_designer_encodes_branch_conditions() -> None:
    plan = _plan(
        [
            _task("db-1", "db_search"),
            _task(
                "web-1",
                "web_search",
                depends_on=["db-1"],
                inputs={"when": [{"task": "db-1", "status": "failure"}]},
            ),
        ],
        problem="Branch",
    )
    graph = WorkflowDesigner().build_graph(plan)
    node = graph.nodes["web-1"]
    assert node.conditions, "Branch conditions should be recorded"
    assert "db-1" in node.allowed_dependency_statuses
    allowed_statuses = node.allowed_dependency_statuses["db-1"]
    assert TaskStatus.FAILED in allowed_statuses


def test_designer_parses_retry_directive() -> None:
    plan = _plan(
        [
            _task(
                "db-1",
                "db_search",
                inputs={"retry": {"max_attempts": 3, "delay_seconds": 1.5}},
            )
        ],
        problem="Retry",
    )
    graph = WorkflowDesigner().build_graph(plan)
    node = graph.nodes["db-1"]
    assert node.max_attempts == 3
    assert node.retry_delay_seconds == 1.5
