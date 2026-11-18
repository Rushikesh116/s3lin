"""The public API: records, validation, results, the recorder and file I/O."""

from __future__ import annotations

import gzip
import json
import threading
from pathlib import Path
from typing import get_args

import pytest

import s3lin
from s3lin import (
    HistoryError,
    Linearizable,
    Op,
    OpKind,
    Outcome,
    Recorder,
    Unknown,
    Violation,
    check,
    outcome_from_status,
    parse_history,
    read_history,
    write_history,
)
from s3lin._model import KINDS, OUTCOMES


def put_inm(
    i: int, t0: float, t1: float | None, value: str, key: str = "k", res: Outcome = "ok"
) -> Op:
    return Op(id=i, key=key, kind="put_inm", t0=t0, t1=t1, result=res, value=value)


def get(i: int, t0: float, t1: float, read: str | None, key: str = "k") -> Op:
    return Op(id=i, key=key, kind="get", t0=t0, t1=t1, result="ok", read_value=read)


# ---------- records ----------
def test_literal_types_match_runtime_sets() -> None:
    assert set(get_args(OpKind)) == KINDS
    assert set(get_args(Outcome)) == OUTCOMES


def test_all_is_the_public_api() -> None:
    assert sorted(s3lin.__all__) == sorted(
        n for n in dir(s3lin) if not n.startswith("_") or n == "__version__"
    )


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"kind": "put_inm"}, "needs the value it writes"),
        ({"kind": "put_im", "value": "v"}, "needs base_value"),
        ({"kind": "get", "value": "v"}, "does not write a value"),
        ({"kind": "del_im", "base_value": "v", "read_value": "x"}, "does not read a value"),
        ({"kind": "get", "result": "fail"}, "cannot fail"),
        ({"kind": "get", "t1": None}, "t1 must be a finite number"),
        ({"kind": "get", "t1": -1.0}, "is before t0"),
        ({"kind": "get", "t0": float("nan")}, "t0 must be a finite number"),
        ({"kind": "get", "result": "info"}, "has no completion time"),
        ({"kind": "get", "id": True}, "id must be an integer"),
        ({"kind": "frob"}, "unknown operation kind"),
        ({"kind": "get", "result": "maybe"}, "unknown result"),
    ],
)
def test_invalid_ops_are_rejected(fields: dict[str, object], message: str) -> None:
    base: dict[str, object] = {"id": 1, "key": "k", "t0": 0.0, "t1": 1.0, "result": "ok"}
    with pytest.raises(HistoryError, match=message):
        # Deliberately ill-typed input: validation must catch what a type checker would.
        Op(**(base | fields))  # type: ignore[arg-type]


def test_dict_round_trip_and_study_format() -> None:
    line = (
        '{"id": 3, "client": 1, "key": "k", "op": "put_inm", "val": "c1-3", "t0": 0.72,'
        ' "res": "ok", "t1": 0.75, "t1_obs": 0.75, "status": 200, "etag": "\\"49\\"", "err": null}'
    )
    (op,) = parse_history([line])
    assert op == Op(
        id=3,
        client=1,
        key="k",
        kind="put_inm",
        value="c1-3",
        t0=0.72,
        t1=0.75,
        result="ok",
        status=200,
    )
    assert Op.from_dict(op.to_dict()) == op
    assert op.to_dict()["op"] == "put_inm"


def test_parse_errors_name_the_line() -> None:
    with pytest.raises(HistoryError, match=r"h:2: missing field 'res'"):
        parse_history(
            [
                '{"id": 1, "key": "k", "op": "get", "t0": 0, "t1": 1, "res": "ok"}',
                '{"id": 2, "key": "k", "op": "get", "t0": 0}',
            ],
            source="h",
        )
    with pytest.raises(HistoryError, match="h:1: not JSON"):
        parse_history(["{nope"], source="h")
    with pytest.raises(HistoryError, match="expected a JSON object"):
        parse_history(["[1]"])
    with pytest.raises(HistoryError, match="'t0' must be a number"):
        parse_history(['{"id": 1, "key": "k", "op": "get", "t0": "0", "res": "info"}'])


@pytest.mark.parametrize(
    ("status", "kind", "expected"),
    [
        (None, "put_inm", "info"),
        (200, "put_inm", "ok"),
        (204, "del_im", "ok"),
        (412, "put_inm", "fail"),
        (412, "put", "fail"),
        (404, "put_im", "fail"),
        (404, "del_im", "fail"),
        (404, "put_inm", "noeffect"),
        (404, "get", "ok"),
        (409, "put_im", "conflict"),
        (400, "put_inm", "noeffect"),
        (403, "get", "noeffect"),
        (500, "put_inm", "info"),
        (503, "get", "info"),
        (304, "put", "info"),
    ],
)
def test_outcome_from_status(status: int | None, kind: OpKind, expected: Outcome) -> None:
    assert outcome_from_status(status, kind) == expected


# ---------- check ----------
def test_violation_names_key_blocking_op_and_invariant() -> None:
    h = [
        put_inm(1, 0.0, 1.0, "a"),
        put_inm(2, 0.5, 1.5, "b"),
        put_inm(3, 0.0, 1.0, "x", key="other"),
        get(4, 2.0, 3.0, "x", key="other"),
    ]
    result = check(h)
    assert result.verdict == "violation"
    assert [r.key for r in result.keys] == ["k", "other"]
    assert isinstance(result.keys[1], Linearizable)
    (v,) = result.violations
    assert v.key == "k"
    assert v.blocking_op.id == 2
    assert [o.id for o in v.prefix] == [1]
    assert v.state_after_prefix == "a"
    assert [(i.invariant, [o.id for o in i.ops]) for i in v.invariants] == [("create-once", [1, 2])]


def test_stale_read_has_no_invariant_but_a_blocking_read() -> None:
    h = [
        put_inm(1, 0, 1, "a"),
        Op(id=2, key="k", kind="put_im", t0=2, t1=3, result="ok", value="b", base_value="a"),
        get(3, 4, 5, "a"),
    ]
    (v,) = check(h).violations
    assert v.blocking_op.id == 3
    assert v.state_after_prefix == "b"
    assert v.invariants == ()


def test_match_statement_on_results() -> None:
    def describe(r: s3lin.KeyResult) -> str:
        match r:
            case Violation(key=k, blocking_op=o):
                return f"{k}:{o.id}"
            case Unknown(limit=limit):
                return limit
            case Linearizable():
                return "ok"

    result = check([put_inm(1, 0, 1, "a"), put_inm(2, 2, 3, "b")])
    assert [describe(r) for r in result.keys] == ["k:2"]


def test_empty_history_and_only_dropped_ops() -> None:
    assert check([]).verdict == "linearizable"
    h = [
        put_inm(1, 0, 1, "a", res="conflict"),
        Op(id=2, key="k", kind="get", t0=0, t1=None, result="info"),
    ]
    (r,) = check(h).keys
    assert isinstance(r, Linearizable)
    assert r.ops_checked == 0


def test_duplicate_ids_and_values_are_rejected() -> None:
    with pytest.raises(HistoryError, match="duplicate op id 1"):
        check([put_inm(1, 0, 1, "a"), put_inm(1, 2, 3, "b")])
    with pytest.raises(HistoryError, match="unique value"):
        check([put_inm(1, 0, 1, "a"), put_inm(2, 2, 3, "a")])


def _exponential() -> list[Op]:
    """13 indeterminate unconditional puts and a read nobody wrote: 2^13 subsets to try."""
    h = [
        Op(id=i, key="k", kind="put", t0=0, t1=None, result="info", value=f"v{i}")
        for i in range(13)
    ]
    return [*h, get(99, 10, 11, "never-written")]


def test_state_limit_gives_unknown() -> None:
    result = check(_exponential(), key_state_limit=100)
    assert result.verdict == "unknown"
    (u,) = result.unknown
    assert u.limit == "states"
    assert [i.invariant for i in u.invariants] == ["no-phantom"]


def test_time_limit_gives_unknown() -> None:
    (u,) = check(_exponential(), key_time_limit=0.0).unknown
    assert u.limit == "time"


def test_exhaustive_search_finishes_without_limits() -> None:
    (v,) = check(_exponential()).violations
    assert v.blocking_op.id == 99
    assert v.states_explored > 4096


# ---------- recorder ----------
class FakeClock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        self.t += 1.0
        return self.t


def test_recorder_records_and_classifies() -> None:
    rec = Recorder(clock=FakeClock())
    a = rec.invoke("put_inm", "k", value="a", client=1)
    g = rec.invoke("get", "k", client=2)
    assert rec.complete(a, 200).t1 == 3.0
    assert rec.complete(g, 200, read_value="a").read_value == "a"
    m = rec.invoke("put_im", "k", value="b", base_value="a")
    late = rec.invoke("put_im", "k", value="c", base_value="a")
    refused = rec.invoke("put", "k", value="d")
    assert rec.complete(m, None).result == "info"
    assert rec.complete(refused, None, sent=False).result == "noeffect"
    ops = rec.history()
    assert [o.id for o in ops] == [1, 2, 3, 4, 5]
    assert ops[3] == Op(
        id=4, key="k", kind="put_im", t0=6.0, t1=None, result="info", value="c", base_value="a"
    )
    assert late.id == 4
    assert [o.t0 for o in ops] == [1.0, 2.0, 5.0, 6.0, 7.0]
    assert check(ops).verdict == "linearizable"


def test_recorder_rejects_misuse() -> None:
    rec = Recorder()
    with pytest.raises(HistoryError, match="needs base_value"):
        rec.invoke("put_im", "k", value="v")
    g = rec.invoke("get", "k")
    with pytest.raises(ValueError, match="must pass read_value"):
        rec.complete(g, 200)
    with pytest.raises(ValueError, match="404 read has no value"):
        rec.complete(g, 404, read_value="x")
    with pytest.raises(ValueError, match="status must be None"):
        rec.complete(g, 200, sent=False)
    assert rec.complete(g, 404).read_value is None
    with pytest.raises(ValueError, match="not pending"):
        rec.complete(g, 404)
    w = rec.invoke("put", "k", value="v")
    with pytest.raises(ValueError, match="only for successful gets"):
        rec.complete(w, 200, read_value="v")
    with pytest.raises(ValueError, match="not pending"):
        Recorder().complete(w, 200)


def test_recorder_is_thread_safe() -> None:
    rec = Recorder()
    n_threads, per_thread = 8, 250

    def work(c: int) -> None:
        for i in range(per_thread):
            call = rec.invoke("put", f"k{c}", value=f"{c}-{i}", client=c)
            rec.complete(call, 200)

    threads = [threading.Thread(target=work, args=(c,)) for c in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ops = rec.history()
    assert len(ops) == len({o.id for o in ops}) == n_threads * per_thread
    assert all(o.t1 is not None and o.t0 <= o.t1 for o in ops)
    assert check(ops).verdict == "linearizable"


# ---------- files ----------
@pytest.mark.parametrize("name", ["h.jsonl", "h.jsonl.gz"])
def test_write_and_read_history(tmp_path: Path, name: str) -> None:
    h = [put_inm(1, 0, 1, "a"), get(2, 2, 3, "a"), get(3, 4, 5, None, key="absent")]
    write_history(h, tmp_path / name)
    assert read_history(tmp_path / name) == h
    if name.endswith(".gz"):
        with gzip.open(tmp_path / name, "rt") as f:
            assert json.loads(f.readline())["op"] == "put_inm"


def test_metadata_and_blank_lines_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "h.jsonl"
    path.write_text(
        json.dumps(put_inm(1, 0, 1, "a").to_dict())
        + '\n\n{"nemesis": [{"t": 1.0, "event": "kill db"}]}\n'
    )
    assert [o.id for o in read_history(path)] == [1]
