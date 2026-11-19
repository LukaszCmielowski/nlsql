from contextlib import nullcontext
from decimal import Decimal
from io import StringIO

import pandas as pd
import pytest
from rich.console import Console

from nlsql.output import print_query_output, query_progress


def render(sql, frame, *, width=100, **options):
    stream = StringIO()
    print_query_output(
        sql,
        frame,
        connection_id="demo-connection",
        console=Console(file=stream, width=width, color_system=None),
        **options,
    )
    return stream.getvalue()


def test_answer_precedes_formatted_sql_and_uses_actual_query_limit():
    # Numeric PostgreSQL aggregates may arrive over Flight as text.
    frame = pd.DataFrame(
        {
            "customer_name": ["Jordan Lee", "Jan Kowalski", "Lena Fischer"],
            "total_spent": ["2700", "1260", "730"],
        }
    )
    original = frame.copy(deep=True)
    output = render(
        "SELECT customer_name, SUM(quantity * unit_price) AS total_spent "
        "FROM order_items GROUP BY customer_name ORDER BY total_spent DESC LIMIT 3",
        frame,
        question="Which three customers spent the most?",
        max_rows=100,
    )
    assert "Customer name" in output and "Total spent" in output
    assert "2,700" in output and "1,260" in output
    assert "3 rows returned · query limit 3" in output
    assert "limit 100" not in output
    assert output.index("Results") < output.index("Jordan Lee") < output.index("\nSQL\n")
    assert "\nFROM" in output and "\nGROUP BY" in output
    assert "\x1b" not in output
    pd.testing.assert_frame_equal(frame, original)


def test_text_identifiers_and_markup_are_preserved_literally():
    output = render(
        "SELECT customer_id, label FROM customers LIMIT 10",
        pd.DataFrame(
            {
                "customer_id": ["001234567890"],
                "label": ["[red]customer[/red]"],
            }
        ),
    )
    assert "Customer ID" in output
    assert "001234567890" in output and "[red]customer[/red]" in output
    assert "1,234,567,890" not in output


@pytest.mark.parametrize("wildcard", ["*", "items.*"])
def test_wildcard_does_not_shift_numeric_formatting_onto_text(wildcard):
    output = render(
        f"SELECT {wildcard}, amount * 2 AS doubled FROM items",
        pd.DataFrame({"id": [1], "postal_code": ["001234"], "amount": [10], "doubled": [20]}),
    )
    assert "001234" in output
    assert "1,234" not in output


def test_decimal_values_keep_precision_and_scale():
    output = render(
        "SELECT total FROM totals LIMIT 10",
        pd.DataFrame(
            {
                "total": [Decimal("1234567.1234567890123456789"), Decimal("12.00")],
            }
        ),
    )
    assert "1,234,567.1234567890123456789" in output
    assert "12.00" in output


def test_nulls_dates_and_boolean_values_are_readable():
    output = render(
        "SELECT * FROM events LIMIT 10",
        pd.DataFrame(
            {
                "event_date": [pd.Timestamp("2026-01-05"), pd.NaT],
                "active": [True, False],
                "amount": [None, float("nan")],
            }
        ),
    )
    assert "2026-01-05" in output
    assert "True" in output and "False" in output
    assert "NULL" in output
    assert "NaT" not in output and "nan" not in output and "None" not in output


def test_empty_results_are_explicit_and_still_show_sql():
    output = render("SELECT name FROM customers LIMIT 10", pd.DataFrame(columns=["name"]))
    assert "No matching rows." in output and "0 rows returned" in output
    assert "\nSQL\n" in output and "SELECT" in output


def test_dry_run_does_not_look_like_an_empty_query_result():
    output = render("SELECT name FROM customers LIMIT 10", None)
    assert "SQL preview (not executed)" in output
    assert "rows returned" not in output and "No matching rows" not in output


def test_one_row_and_correction_are_reported_accurately():
    output = render(
        "SELECT COUNT(*) AS customers FROM customers LIMIT 100",
        pd.DataFrame(
            {
                "customers": [6],
            }
        ),
        corrected=True,
    )
    assert "1 row returned" in output
    assert "SQL corrected once after a schema or source query error" in output


@pytest.mark.parametrize("width", [40, 80])
def test_text_wraps_to_terminal_width_without_ellipsis(width):
    output = render(
        "SELECT description FROM products LIMIT 10",
        pd.DataFrame(
            {
                "description": [
                    "A long product description with multiple words that should wrap naturally."
                ],
            }
        ),
        width=width,
    )
    assert all(len(line) <= width for line in output.splitlines())
    assert "naturally." in output and "…" not in output


def test_duplicate_column_labels_keep_both_values():
    output = render(
        "SELECT a.id, b.id FROM a JOIN b ON a.id = b.id LIMIT 10",
        pd.DataFrame(
            [[1234, 5678]],
            columns=["id", "id"],
        ),
    )
    assert "1,234" in output and "5,678" in output


def test_progress_is_silent_when_output_is_redirected():
    stream = StringIO()
    console = Console(file=stream, force_terminal=False)
    with query_progress(console) as progress:
        progress("Generating SQL with the model...")
        progress("Running the query through DCH...")
    assert stream.getvalue() == ""


@pytest.mark.parametrize("failure", [None, RuntimeError, KeyboardInterrupt])
def test_spinner_releases_terminal_before_success_errors_or_interruptions(failure):
    stream = StringIO()
    console = Console(file=stream, force_terminal=True, width=80)
    expected = pytest.raises(failure) if failure else nullcontext()
    with expected, query_progress(console) as progress:
        progress("Running the query through DCH...")
        if failure:
            raise failure("stopped")
    console.print("Final output", markup=False)
    output = stream.getvalue()
    assert "Running the query through DCH" in output
    # The cursor must be restored before the caller prints results or an error.
    assert 0 <= output.rfind("\x1b[?25h") < output.index("Final output")
