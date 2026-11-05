"""Static checks on migration 089 (exec_broker): no database needed.

The ledger trigger functions are re-emitted from 083, so the risk is a dropped/renamed branch or an
INSERT that forgets the new column. Counts are derived from 083's two function bodies (its backfill
INSERT..SELECT statements are outside the functions and are not part of this comparison).
"""

from __future__ import annotations

import re
from pathlib import Path

_MIG = Path(__file__).parents[3] / "migrations"
SQL = (_MIG / "089_exec_broker.sql").read_text(encoding="utf-8")
LEDGER_083 = (_MIG / "083_trade_executions_ledger.sql").read_text(encoding="utf-8")

_FUNC_RE = re.compile(
    r"CREATE OR REPLACE FUNCTION (trade_executions_(?:equity|options)_ledger)\(\).*?"
    r"END \$\$ LANGUAGE plpgsql;",
    re.S,
)
_INSERT_RE = re.compile(r"INSERT INTO trade_executions\s*\((.*?)\)\s*VALUES\s*\(", re.S)


def _functions(text: str) -> dict[str, str]:
    return {m.group(1): m.group(0) for m in _FUNC_RE.finditer(text)}


def _split_top_level(s: str) -> list[str]:
    """Split on commas that are not inside parentheses or single-quoted strings."""
    parts: list[str] = []
    depth = 0
    in_str = False
    cur: list[str] = []
    for ch in s:
        if ch == "'":
            in_str = not in_str
        elif not in_str and ch == "(":
            depth += 1
        elif not in_str and ch == ")":
            depth -= 1
        elif not in_str and depth == 0 and ch == ",":
            parts.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    parts.append("".join(cur).strip())
    return parts


def _insert_pairs(text: str) -> list[tuple[list[str], list[str]]]:
    """(columns, values) for every INSERT INTO trade_executions ... VALUES (...) in text."""
    pairs = []
    for m in _INSERT_RE.finditer(text):
        i, depth = m.end(), 1  # just past VALUES (
        in_str = False
        while depth:
            ch = text[i]
            if ch == "'":
                in_str = not in_str
            elif not in_str and ch == "(":
                depth += 1
            elif not in_str and ch == ")":
                depth -= 1
            i += 1
        pairs.append((_split_top_level(m.group(1)), _split_top_level(text[m.end() : i - 1])))
    return pairs


def test_adds_nullable_exec_broker_columns_idempotently() -> None:
    for table in (
        "trades",
        "paper_trades",
        "options_positions",
        "options_structures",
        "external_positions",
        "trade_executions",
    ):
        assert re.search(
            rf"ALTER TABLE\s+{table}\s+ADD COLUMN IF NOT EXISTS exec_broker TEXT\s*;", SQL
        ), table


def test_altered_tables_are_created_by_earlier_migrations() -> None:
    created = "\n".join(p.read_text(encoding="utf-8") for p in sorted(_MIG.glob("*.sql")))
    for table in (
        "trades",
        "paper_trades",
        "options_positions",
        "options_structures",
        "external_positions",
        "trade_executions",
    ):
        assert re.search(rf"CREATE TABLE IF NOT EXISTS {table}\b", created), table


def test_both_ledger_functions_redefined_and_use_jsonb_read() -> None:
    funcs = _functions(SQL)
    assert set(funcs) == {"trade_executions_equity_ledger", "trade_executions_options_ledger"}
    assert "r.exec_broker" not in SQL
    for body in funcs.values():
        assert "to_jsonb(r)->>'exec_broker'" in body


def test_functions_keep_every_083_branch() -> None:
    old, new = _functions(LEDGER_083), _functions(SQL)
    assert set(old) == set(new)
    for name in old:
        events_old = re.findall(r"'([A-Z_]{5,})'", old[name])
        events_new = re.findall(r"'([A-Z_]{5,})'", new[name])
        assert events_old == events_new, name
        for kw in ("IF TG_OP = 'INSERT'", "ELSIF TG_OP = 'DELETE'", "EXCEPTION WHEN OTHERS"):
            assert old[name].count(kw) == new[name].count(kw), (name, kw)


def test_every_ledger_insert_names_exec_broker_with_a_matching_value() -> None:
    old = [p for body in _functions(LEDGER_083).values() for p in _insert_pairs(body)]
    new = _insert_pairs(SQL)
    # Counted from 083's two function bodies: 13 (equity) + 8 (options) = 21.
    assert len(old) == 21
    assert len(new) == len(old)
    for cols, vals in new:
        assert len(cols) == len(vals), (cols, vals)
        assert cols[-1] == "exec_broker"
    # Same inserts as before, plus exactly one column each.
    for (ocols, ovals), (ncols, nvals) in zip(old, new, strict=True):
        assert ncols[:-1] == ocols
        assert nvals[:-1] == ovals


def test_no_destructive_statements() -> None:
    assert not re.search(r"\b(DROP TABLE|DROP COLUMN|TRUNCATE|DELETE FROM)\b", SQL, re.I)
    assert not re.search(r"\bDISABLE TRIGGER\b", SQL, re.I)  # 083 never needed it either


def test_backfill_never_invents_a_broker_for_paper_rows() -> None:
    assert re.search(
        r"UPDATE trades\s+SET exec_broker = 'kite'\s+WHERE exec_broker IS NULL\s*;", SQL
    )
    assert re.search(
        r"UPDATE options_positions\s+SET exec_broker = 'kite'\s+"
        r"WHERE exec_broker IS NULL AND is_paper IS NOT TRUE\s*;",
        SQL,
    )
    assert not re.search(r"UPDATE paper_trades", SQL)


def test_indexes_are_idempotent() -> None:
    assert (
        "CREATE INDEX IF NOT EXISTS idx_trades_exec_broker_state ON trades (exec_broker, order_state)"
        in SQL
    )
    assert re.search(r"CREATE INDEX IF NOT EXISTS \w+ ON trade_executions \(exec_broker\)", SQL)
