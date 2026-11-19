"""Explicit LangGraph SQL workflow with DCH as the only execution backend."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, TypedDict

import pandas as pd
from data_connect_hub import DataConnectClient, DCHQueryError
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from openai import OpenAI
from sqlglot.errors import SqlglotError

from .metadata import inspect_tables
from .schema import TableSchema, discover_tables
from .service import QUERY_CHECK_PROGRESS, check_source_sql, draft_sql, run_sql
from .validation import CorrectableSQLValidationError, validate_sql


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
    on_progress: Callable[[str], None] | None = None

    def report_progress(self, message: str):
        if self.on_progress is not None:
            self.on_progress(message)

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
            r"type mismatch|operator does not exist|cannot cast|binder error|(?:function|type) .*does not exist",
            "The source rejected an expression or type. Check types and supported functions.",
        ),
    ]
    return next((feedback for pattern, feedback in categories if re.search(pattern, message)), "")


def load_schema(state: QueryState, runtime: Runtime[QueryContext]) -> dict:
    if not state["question"].strip():
        raise ValueError("Question must not be empty")
    context = runtime.context
    context.report_progress("Discovering database schema through DCH...")
    return {
        "tables": discover_tables(
            context.dch, context.connection_id, list(context.selected_tables) or None
        )
    }


def generate(state: QueryState, runtime: Runtime[QueryContext]) -> dict:
    context = runtime.context
    context.report_progress("Generating SQL with the model...")
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


def inspect_schema(state: QueryState, runtime: Runtime[QueryContext]) -> dict:
    context = runtime.context
    context.report_progress("Inspecting database types, keys, and descriptions through DCH...")
    return {
        "tables": inspect_tables(
            context.dch, context.connection_id, state["tables"], context.dialect
        )
    }


def check_query(state: QueryState, runtime: Runtime[QueryContext]) -> dict:
    context = runtime.context
    context.report_progress(QUERY_CHECK_PROGRESS)
    try:
        return {
            "sql": check_source_sql(
                context.dch,
                context.connection_id,
                state["sql"],
                state["tables"],
                context.dialect,
                context.max_rows,
            ),
            "error": "",
            "feedback": "",
        }
    except CorrectableSQLValidationError as exc:
        return {"error": str(exc), "feedback": str(exc)}
    except SqlglotError:
        return {
            "error": "Generated SQL failed syntax validation; nothing was executed for this draft.",
            "feedback": "The SQL could not be parsed. Rewrite one valid SELECT for the supplied dialect and schema.",
        }
    except DCHQueryError as exc:
        feedback = correction_feedback(exc)
        return {
            "error": "Database query check failed. "
            + (feedback or "Check source permissions and DCH logs."),
            "feedback": feedback,
        }
    except ValueError:
        # Validation failures stop; no unsafe query is used as an execution probe.
        return {
            "error": "Generated SQL failed validation; nothing was executed for this draft.",
            "feedback": "",
        }


def execute(state: QueryState, runtime: Runtime[QueryContext]) -> dict:
    context = runtime.context
    context.report_progress("Running the query through DCH...")
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
    context.report_progress("Correcting SQL with the model...")
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


def after_query_check(
    state: QueryState, runtime: Runtime[QueryContext]
) -> Literal["execute", "repair", "end"]:
    if state.get("error"):
        return repair_or_end(state, runtime)
    return "end" if runtime.context.dry_run else "execute"


def repair_or_end(state: QueryState, runtime: Runtime[QueryContext]) -> Literal["repair", "end"]:
    if state.get("feedback") and state["attempts"] <= runtime.context.max_corrections:
        return "repair"
    return "end"


def build_workflow():
    """Compile discovery → inspection → generation → validation → planning → execution."""
    graph = StateGraph(QueryState, context_schema=QueryContext)
    graph.add_node("schema", load_schema)
    graph.add_node("inspect_schema", inspect_schema)
    graph.add_node("generate", generate)
    graph.add_node("check_query", check_query)
    graph.add_node("execute", execute)
    graph.add_node("repair", repair)
    graph.add_edge(START, "schema")
    graph.add_edge("schema", "inspect_schema")
    graph.add_edge("inspect_schema", "generate")
    graph.add_edge("generate", "check_query")
    graph.add_conditional_edges(
        "check_query", after_query_check, {"execute": "execute", "repair": "repair", "end": END}
    )
    graph.add_conditional_edges("execute", repair_or_end, {"repair": "repair", "end": END})
    graph.add_edge("repair", "check_query")
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
