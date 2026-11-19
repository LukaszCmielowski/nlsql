"""Explicit LangGraph SQL workflow with DCH as the only execution backend."""

import re
from dataclasses import dataclass
from typing import Literal, TypedDict

import pandas as pd
from data_connect_hub import DataConnectClient, DCHQueryError
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from openai import OpenAI
from sqlglot.errors import SqlglotError

from .schema import TableSchema, discover_tables
from .service import draft_sql, run_sql
from .validation import ColumnValidationError, validate_sql


class QueryState(TypedDict, total=False):
    question: str
    tables: list[TableSchema]
    sql: str
    attempts: int
    feedback: str
    error: str
    result: pd.DataFrame


@dataclass(frozen=True)
class QueryContext:
    """Trusted per-invocation dependencies, separate from model-generated state."""

    dch: DataConnectClient
    llm: OpenAI
    model: str
    connection_id: str
    dialect: str = "postgres"
    max_rows: int = 100
    selected_tables: tuple[str, ...] = ()
    business_context: str = ""
    dry_run: bool = False
    max_corrections: int = 1

    def __post_init__(self):
        if not self.connection_id.strip() or not self.model.strip():
            raise ValueError("Connection ID and model must not be empty")
        if self.max_corrections not in (0, 1):
            raise ValueError("max_corrections must be 0 or 1")
        # Check options before any schema or model request.
        validate_sql("SELECT 1", [], self.dialect, self.max_rows)


class QueryWorkflowError(ValueError):
    """The workflow stopped before obtaining a validated, successful result."""


def correction_feedback(error: DCHQueryError) -> str:
    """Return fixed categories only; transport bodies never enter model prompts."""
    message = str(error).lower()
    if re.search(
        r"permission|denied|unauthori[sz]ed|forbidden|token|timeout|timed out|tls|certificate",
        message,
    ):
        return ""
    categories = [
        (r"syntax error|parser error|parse error", "The source rejected the SQL syntax."),
        (
            r"no such column|unknown column|column .*does not exist|ambiguous.*column|column.*ambiguous",
            "The source could not resolve a column. Check column names and aliases against the schema.",
        ),
        (
            r"no such table|relation .*does not exist|table .*does not exist",
            "The source could not resolve a table. Check the supplied schema and qualified names.",
        ),
        (
            r"type mismatch|operator does not exist|cannot cast|binder error|function .*does not exist",
            "The source rejected an expression or type. Check types and supported functions.",
        ),
    ]
    return next((feedback for pattern, feedback in categories if re.search(pattern, message)), "")


def load_schema(state: QueryState, runtime: Runtime[QueryContext]) -> dict:
    if not state["question"].strip():
        raise ValueError("Question must not be empty")
    context = runtime.context
    return {
        "tables": discover_tables(
            context.dch, context.connection_id, list(context.selected_tables) or None
        )
    }


def generate(state: QueryState, runtime: Runtime[QueryContext]) -> dict:
    context = runtime.context
    sql = draft_sql(
        context.llm,
        context.model,
        state["question"],
        state["tables"],
        context.dialect,
        context.max_rows,
        context.business_context,
    )
    return {"sql": sql, "attempts": 1, "error": "", "feedback": ""}


def validate(state: QueryState, runtime: Runtime[QueryContext]) -> dict:
    context = runtime.context
    try:
        return {
            "sql": validate_sql(state["sql"], state["tables"], context.dialect, context.max_rows),
            "error": "",
        }
    except ColumnValidationError as exc:
        return {"error": str(exc), "feedback": str(exc)}
    except (ValueError, SqlglotError):
        # Validation failures stop; no unsafe query is used as an execution probe.
        return {
            "error": "Generated SQL failed validation; nothing was executed for this draft.",
            "feedback": "",
        }


def execute(state: QueryState, runtime: Runtime[QueryContext]) -> dict:
    context = runtime.context
    try:
        sql, frame = run_sql(
            context.dch,
            context.connection_id,
            state["sql"],
            state["tables"],
            context.dialect,
            context.max_rows,
        )
        return {"sql": sql, "result": frame, "error": "", "feedback": ""}
    except DCHQueryError as exc:
        feedback = correction_feedback(exc)
        return {
            "error": "DCH query failed. " + (feedback or "Check DCH access and source logs."),
            "feedback": feedback,
        }


def repair(state: QueryState, runtime: Runtime[QueryContext]) -> dict:
    context = runtime.context
    sql = draft_sql(
        context.llm,
        context.model,
        state["question"],
        state["tables"],
        context.dialect,
        context.max_rows,
        context.business_context,
        repair={
            "previous_sql": state["sql"],
            "feedback": state["feedback"],
            "instruction": "Return a corrected SELECT using the supplied schema.",
        },
    )
    return {"sql": sql, "attempts": state["attempts"] + 1, "error": "", "feedback": ""}


def after_validation(
    state: QueryState, runtime: Runtime[QueryContext]
) -> Literal["execute", "repair", "end"]:
    if state.get("error") and state.get("feedback"):
        return after_execution(state, runtime)
    return "end" if state.get("error") or runtime.context.dry_run else "execute"


def after_execution(state: QueryState, runtime: Runtime[QueryContext]) -> Literal["repair", "end"]:
    if state.get("feedback") and state["attempts"] <= runtime.context.max_corrections:
        return "repair"
    return "end"


def build_workflow():
    """Compile schema → generation → validation → DCH execution, with one repair edge."""
    graph = StateGraph(QueryState, context_schema=QueryContext)
    graph.add_node("schema", load_schema)
    graph.add_node("generate", generate)
    graph.add_node("validate", validate)
    graph.add_node("execute", execute)
    graph.add_node("repair", repair)
    graph.add_edge(START, "schema")
    graph.add_edge("schema", "generate")
    graph.add_edge("generate", "validate")
    graph.add_conditional_edges(
        "validate", after_validation, {"execute": "execute", "repair": "repair", "end": END}
    )
    graph.add_conditional_edges("execute", after_execution, {"repair": "repair", "end": END})
    graph.add_edge("repair", "validate")
    return graph.compile()


def run_question(question: str, context: QueryContext) -> QueryState:
    result = build_workflow().invoke(
        {"question": question}, context=context, config={"recursion_limit": 12}
    )
    if result.get("error"):
        raise QueryWorkflowError(
            result["error"] + f"\nSQL after {result['attempts']} attempt(s):\n{result['sql']}"
        )
    return result
