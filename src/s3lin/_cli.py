"""The ``s3lin`` command."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import NotRequired, TypedDict

from . import __version__
from ._check import CheckResult, KeyResult, Linearizable, Unknown, Violation, check
from ._invariants import InvariantViolation
from ._io import read_history
from ._model import HistoryError, Op, OpRecord

EXIT_LINEARIZABLE = 0
EXIT_VIOLATION = 1
EXIT_ERROR = 2
EXIT_UNKNOWN = 3


class _Args(argparse.Namespace):
    command: str | None
    histories: list[str]
    json: bool
    key_time_limit: float
    key_state_limit: int


class _InvariantJSON(TypedDict):
    invariant: str
    ops: list[int]
    message: str


class _KeyJSON(TypedDict):
    key: str
    verdict: str
    ops_checked: int
    states_explored: int
    elapsed_s: float
    invariants: list[_InvariantJSON]
    blocking_op: NotRequired[OpRecord]
    prefix: NotRequired[list[int]]
    state_after_prefix: NotRequired[str | None]
    limit: NotRequired[str]


class _FileJSON(TypedDict):
    path: str
    verdict: NotRequired[str]
    operations: NotRequired[int]
    keys: NotRequired[int]
    indeterminate: NotRequired[int]
    elapsed_s: NotRequired[float]
    problems: NotRequired[list[_KeyJSON]]
    error: NotRequired[str]


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="s3lin", description="Check recorded S3 conditional-write histories."
    )
    p.add_argument("--version", action="version", version=f"s3lin {__version__}")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    c = sub.add_parser(
        "check",
        help="check histories for linearizability",
        description="Check JSON Lines histories (.gz is decompressed). Exit status: "
        "0 linearizable, 1 violation, 2 bad input, 3 undecided (a limit was hit).",
    )
    c.add_argument("histories", nargs="+", metavar="HISTORY")
    c.add_argument("--json", action="store_true", help="print one JSON document")
    c.add_argument(
        "--key-time-limit", type=float, default=30.0, metavar="SECONDS",
        help="search time limit per key (default: 30)",
    )  # fmt: skip
    c.add_argument(
        "--key-state-limit", type=int, default=5_000_000, metavar="N",
        help="search state limit per key (default: 5000000)",
    )  # fmt: skip
    return p


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _describe(op: Op) -> str:
    what: str = op.kind
    if op.value is not None:
        what += f" writing {op.value!r}"
    if op.base_value is not None:
        what += f" if-match {op.base_value!r}"
    if op.kind == "get" and op.result == "ok":
        what += f" read {op.read_value!r}" if op.read_value is not None else " read absent"
    status = f", HTTP {op.status}" if op.status is not None else ""
    end = f"{op.t1:.3f}" if op.t1 is not None else "never"
    return (
        f"op {op.id} (client {op.client}): {what} -> {op.result}{status}; "
        f"invoked {op.t0:.3f}, returned {end}"
    )


def _invariant_lines(found: Sequence[InvariantViolation]) -> list[str]:
    return [f"    {v.invariant}: {v.message}" for v in found]


def _text(path: str, ops: Sequence[Op], result: CheckResult) -> list[str]:
    n_info = sum(1 for o in ops if o.indeterminate)
    lines = [
        f"{path}: {result.verdict}",
        f"  {_plural(len(ops), 'operation')} on {_plural(len(result.keys), 'key')}, "
        f"{n_info:,} indeterminate",
    ]
    for r in result.keys:
        if isinstance(r, Violation):
            lines.append(
                f"  key {r.key!r}: not linearizable ({_plural(r.ops_checked, 'operation')})"
            )
            lines.append(f"    blocking {_describe(r.blocking_op)}")
            held = "absent" if r.state_after_prefix is None else repr(r.state_after_prefix)
            lines.append(
                f"    longest linearizable prefix: {_plural(len(r.prefix), 'operation')}, "
                f"after which the key is {held}"
            )
            lines.extend(_invariant_lines(r.invariants))
        elif isinstance(r, Unknown):
            lines.append(
                f"  key {r.key!r}: undecided, hit the {r.limit} limit after "
                f"{_plural(r.states_explored, 'state')}"
            )
            lines.extend(_invariant_lines(r.invariants))
    return lines


def _key_json(r: KeyResult) -> _KeyJSON:
    out: _KeyJSON = {
        "key": r.key,
        "verdict": r.verdict,
        "ops_checked": r.ops_checked,
        "states_explored": r.states_explored,
        "elapsed_s": round(r.elapsed_s, 6),
        "invariants": [],
    }
    if isinstance(r, Linearizable):
        return out
    out["invariants"] = [
        {"invariant": v.invariant, "ops": [o.id for o in v.ops], "message": v.message}
        for v in r.invariants
    ]
    if isinstance(r, Violation):
        out["blocking_op"] = r.blocking_op.to_dict()
        out["prefix"] = [o.id for o in r.prefix]
        out["state_after_prefix"] = r.state_after_prefix
    else:
        out["limit"] = r.limit
    return out


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line; returns the exit status."""
    parser = _parser()
    args = parser.parse_args(argv, namespace=_Args())
    if args.command != "check":
        parser.print_help(sys.stderr)
        return EXIT_ERROR
    codes: list[int] = []
    reports: list[_FileJSON] = []
    for path in args.histories:
        try:
            ops = read_history(path)
            result = check(
                ops, key_time_limit=args.key_time_limit, key_state_limit=args.key_state_limit
            )
        except (OSError, HistoryError, UnicodeDecodeError, EOFError) as e:
            codes.append(EXIT_ERROR)
            if args.json:
                reports.append({"path": path, "error": str(e)})
            else:
                print(f"{path}: error: {e}", file=sys.stderr)
            continue
        codes.append(
            {
                "linearizable": EXIT_LINEARIZABLE,
                "violation": EXIT_VIOLATION,
                "unknown": EXIT_UNKNOWN,
            }[result.verdict]
        )
        if args.json:
            reports.append(
                {
                    "path": path,
                    "verdict": result.verdict,
                    "operations": len(ops),
                    "keys": len(result.keys),
                    "indeterminate": sum(1 for o in ops if o.indeterminate),
                    "elapsed_s": round(result.elapsed_s, 6),
                    "problems": [
                        _key_json(r) for r in result.keys if not isinstance(r, Linearizable)
                    ],
                }
            )
        else:
            print("\n".join(_text(path, ops, result)))
    if args.json:
        payload: dict[str, list[_FileJSON]] = {"files": reports}
        print(json.dumps(payload, indent=2))
    for code in (EXIT_ERROR, EXIT_VIOLATION, EXIT_UNKNOWN):
        if code in codes:
            return code
    return EXIT_LINEARIZABLE
