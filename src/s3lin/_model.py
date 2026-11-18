"""Operation records: one S3 request and what the client saw."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final, Literal, NotRequired, TypeAlias, TypedDict, TypeVar

OpKind: TypeAlias = Literal["get", "put", "put_inm", "put_im", "del_im", "mpu_inm"]
"""The S3 request an operation made.

``get``      GetObject.
``put``      PutObject without conditions.
``put_inm``  PutObject with ``If-None-Match: *`` (create only if absent).
``put_im``   PutObject with ``If-Match: <ETag of base_value>`` (compare-and-swap).
``del_im``   DeleteObject with ``If-Match: <ETag of base_value>``.
``mpu_inm``  CompleteMultipartUpload with ``If-None-Match: *``.
"""

Outcome: TypeAlias = Literal["ok", "fail", "conflict", "noeffect", "info"]
"""What the client learned about an operation.

``ok``        The request succeeded (2xx; for ``get`` also 404, which reads "absent").
``fail``      The precondition did not hold: 412, or 404 for If-Match on a missing key.
``conflict``  409: the store refused the request; it had no effect.
``noeffect``  Any other definite error, or a request that was never sent.
``info``      Indeterminate (timeout, reset, 5xx): it may or may not have taken effect.
"""

KINDS: Final[frozenset[str]] = frozenset({"get", "put", "put_inm", "put_im", "del_im", "mpu_inm"})
OUTCOMES: Final[frozenset[str]] = frozenset({"ok", "fail", "conflict", "noeffect", "info"})
WRITES_VALUE: Final[frozenset[str]] = frozenset({"put", "put_inm", "put_im", "mpu_inm"})
NEEDS_BASE: Final[frozenset[str]] = frozenset({"put_im", "del_im"})


class HistoryError(ValueError):
    """An operation record or a history is malformed."""


class OpRecord(TypedDict):
    """One line of a history file (JSON Lines), the format the study recorded."""

    id: int
    key: str
    op: OpKind
    t0: float
    t1: float | None
    res: Outcome
    client: NotRequired[int | str]
    val: NotRequired[str]
    base_val: NotRequired[str]
    read_val: NotRequired[str | None]
    status: NotRequired[int | None]


def field_problem(kind: str, value: object, base_value: object, read_value: object) -> str | None:
    """Return why these fields do not fit ``kind``, or None if they do.

    The arguments are typed ``object`` because they may come from unchecked input.
    """
    if kind not in KINDS:
        return f"unknown operation kind {kind!r}"
    if kind in WRITES_VALUE:
        if not isinstance(value, str):
            return f"{kind} needs the value it writes (a string)"
    elif value is not None:
        return f"{kind} does not write a value"
    if kind in NEEDS_BASE:
        if not isinstance(base_value, str):
            return f"{kind} needs base_value, the value its If-Match ETag identifies"
    elif base_value is not None:
        return f"{kind} has no If-Match base value"
    if kind != "get" and read_value is not None:
        return f"{kind} does not read a value"
    if read_value is not None and not isinstance(read_value, str):
        return "read_value must be a string or None"
    return None


def _is_int(x: object) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _is_str(x: object) -> bool:
    return isinstance(x, str)


def _is_time(x: object) -> bool:
    return isinstance(x, int | float) and not isinstance(x, bool) and math.isfinite(x)


@dataclass(frozen=True, slots=True, kw_only=True)
class Op:
    """One operation in a history.

    ``t0`` is when the client sent the request and ``t1`` when it got the answer, in
    seconds on any clock shared by all clients. ``t1`` is None exactly when the
    outcome is ``info``. Values identify object versions, so every write must carry
    a unique ``value`` (its body). ``read_value`` is the value a ``get`` returned,
    or None for 404.
    """

    id: int
    key: str
    kind: OpKind
    t0: float
    t1: float | None
    result: Outcome
    value: str | None = None
    base_value: str | None = None
    read_value: str | None = None
    client: int | str = 0
    status: int | None = None

    def __post_init__(self) -> None:
        problem = self._problem()
        if problem is not None:
            raise HistoryError(f"op {self.id!r}: {problem}")

    def _problem(self) -> str | None:
        # Fields are re-checked as plain objects: callers without a type checker,
        # and records parsed from files, can pass anything.
        if not _is_int(self.id):
            return "id must be an integer"
        if not _is_str(self.key):
            return "key must be a string"
        if not (_is_int(self.client) or _is_str(self.client)):
            return "client must be an integer or a string"
        if self.status is not None and not _is_int(self.status):
            return "status must be an integer or None"
        problem = field_problem(self.kind, self.value, self.base_value, self.read_value)
        if problem is not None:
            return problem
        if not _is_str(self.result) or self.result not in OUTCOMES:
            return f"unknown result {self.result!r}"
        if self.kind == "get" and self.result == "fail":
            return "a get has no precondition, so it cannot fail"
        if not _is_time(self.t0):
            return "t0 must be a finite number"
        if self.result == "info":
            if self.t1 is not None:
                return "an indeterminate (info) operation has no completion time; t1 must be None"
        elif self.t1 is None or not _is_time(self.t1):
            return "t1 must be a finite number unless the result is info"
        elif self.t1 < self.t0:
            return f"t1 ({self.t1}) is before t0 ({self.t0})"
        return None

    @property
    def indeterminate(self) -> bool:
        """True if the client cannot know whether the operation took effect."""
        return self.result == "info"

    def to_dict(self) -> OpRecord:
        """The JSON Lines record for this operation."""
        rec: OpRecord = {
            "id": self.id,
            "client": self.client,
            "key": self.key,
            "op": self.kind,
            "t0": self.t0,
            "t1": self.t1,
            "res": self.result,
        }
        if self.value is not None:
            rec["val"] = self.value
        if self.base_value is not None:
            rec["base_val"] = self.base_value
        if self.kind == "get":
            rec["read_val"] = self.read_value
        if self.status is not None:
            rec["status"] = self.status
        return rec

    @classmethod
    def from_dict(cls, record: Mapping[str, object]) -> Op:
        """Build an operation from a JSON object; unknown fields are ignored.

        Raises HistoryError if a field is missing or has the wrong type.
        """
        for name in ("id", "key", "op", "t0", "res"):
            if name not in record:
                raise HistoryError(f"missing field {name!r}")
        kind = record["op"]
        result = record["res"]
        if not isinstance(kind, str) or kind not in KINDS:
            raise HistoryError(f"unknown operation kind {kind!r}")
        if not isinstance(result, str) or result not in OUTCOMES:
            raise HistoryError(f"unknown result {result!r}")
        return cls(
            id=_typed(record, "id", int),
            key=_typed(record, "key", str),
            kind=_KIND_OF[kind],
            t0=_number(record, "t0"),
            t1=None if record.get("t1") is None else _number(record, "t1"),
            result=_OUTCOME_OF[result],
            value=_optional(record, "val"),
            base_value=_optional(record, "base_val"),
            read_value=_optional(record, "read_val"),
            client=_client(record),
            status=None if record.get("status") is None else _typed(record, "status", int),
        )


# Literal-typed lookups, so parsed strings narrow to OpKind / Outcome without casts.
_KIND_OF: Final[dict[str, OpKind]] = {
    "get": "get",
    "put": "put",
    "put_inm": "put_inm",
    "put_im": "put_im",
    "del_im": "del_im",
    "mpu_inm": "mpu_inm",
}
_OUTCOME_OF: Final[dict[str, Outcome]] = {
    "ok": "ok",
    "fail": "fail",
    "conflict": "conflict",
    "noeffect": "noeffect",
    "info": "info",
}


_T = TypeVar("_T")


def _typed(record: Mapping[str, object], name: str, typ: type[_T]) -> _T:
    v = record[name]
    if not isinstance(v, typ) or isinstance(v, bool):
        raise HistoryError(f"field {name!r} must be {typ.__name__}, got {v!r}")
    return v


def _number(record: Mapping[str, object], name: str) -> float:
    v = record[name]
    if not isinstance(v, int | float) or isinstance(v, bool):
        raise HistoryError(f"field {name!r} must be a number, got {v!r}")
    return v


def _optional(record: Mapping[str, object], name: str) -> str | None:
    v = record.get(name)
    if v is not None and not isinstance(v, str):
        raise HistoryError(f"field {name!r} must be a string or null, got {v!r}")
    return v


def _client(record: Mapping[str, object]) -> int | str:
    v = record.get("client", 0)
    if not isinstance(v, int | str) or isinstance(v, bool):
        raise HistoryError(f"field 'client' must be an integer or a string, got {v!r}")
    return v


def group_by_key(history: Iterable[Op]) -> dict[str, list[Op]]:
    """Split a history by key, in order of first appearance.

    Raises HistoryError on duplicate ids, or on two writes of one value to a key:
    versions are identified by value, so a repeated value would be ambiguous.
    """
    by_key: dict[str, list[Op]] = {}
    ids: set[int] = set()
    writer: dict[tuple[str, str], int] = {}
    for op in history:
        if op.id in ids:
            raise HistoryError(f"duplicate op id {op.id}")
        ids.add(op.id)
        if op.value is not None:
            first = writer.setdefault((op.key, op.value), op.id)
            if first != op.id:
                raise HistoryError(
                    f"ops {first} and {op.id} both write {op.value!r} to key {op.key!r}; "
                    "every write must carry a unique value"
                )
        by_key.setdefault(op.key, []).append(op)
    return by_key


def outcome_from_status(status: int | None, kind: OpKind) -> Outcome:
    """Classify an HTTP status the way S3 defines it.

    None (no response: timeout, reset, connection closed) and 5xx are ``info``,
    because S3 says such a request may or may not have been applied.
    """
    if status is None:
        return "info"
    if 200 <= status < 300:
        return "ok"
    if kind == "get":
        if status == 404:
            return "ok"  # a read of an absent key
        return "noeffect" if 300 <= status < 500 else "info"
    if status == 412:
        return "fail"
    if status == 404:
        # AWS answers If-Match on a missing key with 404; for other writes a 404
        # means something else is missing (bucket, upload) and nothing happened.
        return "fail" if kind in NEEDS_BASE else "noeffect"
    if status == 409:
        return "conflict"
    if 400 <= status < 500:
        return "noeffect"
    return "info"
