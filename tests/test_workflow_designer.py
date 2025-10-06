"""Tests for the workflow designer agent."""

from src.my_agentic_chatbot.schemas import Plan, PlanTask, TaskStatus
from src.my_agentic_chatbot.workflows.workflow_designer import WorkflowDesigner, WorkflowNodeType
from src.my_agentic_chatbot.workflows import policies


def _task(
    task_id: str,
    tool: str,
    depends_on: list[str] | None = None,
    inputs: dict | None = None,
) -> PlanTask:
    return PlanTask(
        id=task_id,
        description=f"Execute {tool}",
        tool=tool,
        budget_tokens=policies.DEFAULT_DB_BUDGET_TOKENS,
        timeout_seconds=policies.DEFAULT_DB_TIMEOUT_SECONDS,
        depends_on=depends_on or [],
        inputs=inputs or {},
    )


def test_designer_enforces_sequential_dependencies() -> None:
    plan = Plan(
        goals=["Answer question"],
        tasks=[
            _task("db-1", "db_search"),
            _task("web-1", "web_search"),
        ],
    )
    graph = WorkflowDesigner().build_graph(plan)
    ordered = [node.id for node in graph.ordered()]
    assert ordered == ["db-1", "web-1"]
    assert graph.nodes["db-1"].node_type == WorkflowNodeType.DB_QUERY
    assert graph.nodes["web-1"].dependencies == ["db-1"]


def test_designer_respects_explicit_dependencies() -> None:
    plan = Plan(
        goals=["Analyze"],
        tasks=[
            _task("db-1", "db_search"),
            _task("graph-1", "graph_search", depends_on=["db-1"]),
        ],
    )
    graph = WorkflowDesigner().build_graph(plan)
    graph_nodes = graph.ordered()
    assert [node.id for node in graph_nodes] == ["db-1", "graph-1"]
    assert graph.nodes["graph-1"].dependencies == ["db-1"]
    assert graph.nodes["graph-1"].node_type == WorkflowNodeType.GRAPH_QUERY


def test_designer_classifies_custom_agents() -> None:
    plan = Plan(
        goals=["Delegate"],
        tasks=[
            _task("agent-1", "agent-demo"),
        ],
    )
    graph = WorkflowDesigner().build_graph(plan)
    node = graph.nodes["agent-1"]
    assert node.node_type == WorkflowNodeType.CUSTOM_AGENT


def test_designer_encodes_branch_conditions() -> None:
    plan = Plan(
        goals=["Branch"],
        tasks=[
            _task("db-1", "db_search"),
            _task(
                "web-1",
                "web_search",
                depends_on=["db-1"],
                inputs={"when": [{"task": "db-1", "status": "failure"}]},
            ),
        ],
    )
    graph = WorkflowDesigner().build_graph(plan)
    node = graph.nodes["web-1"]
    assert node.conditions, "Branch conditions should be recorded"
    assert "db-1" in node.allowed_dependency_statuses
    allowed_statuses = node.allowed_dependency_statuses["db-1"]
    assert TaskStatus.FAILED in allowed_statuses


def test_designer_parses_retry_directive() -> None:
    plan = Plan(
        goals=["Retry"],
        tasks=[
            _task(
                "db-1",
                "db_search",
                inputs={"retry": {"max_attempts": 3, "delay_seconds": 1.5}},
            )
        ],
    )
    graph = WorkflowDesigner().build_graph(plan)
    node = graph.nodes["db-1"]
    assert node.max_attempts == 3
    assert node.retry_delay_seconds == 1.5
