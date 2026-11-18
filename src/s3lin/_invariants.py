"""Cheap invariants that name the operations behind a violation.

Each one implies that the key's history is not linearizable, given the library's
assumptions (keys start absent, every write carries a unique value).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal, TypeAlias

from ._model import Op, group_by_key

Invariant: TypeAlias = Literal["create-once", "cas-unique", "no-phantom", "no-ghost"]
"""``create-once``  more If-None-Match creates succeeded than deletes could explain.
``cas-unique``   two If-Match operations succeeded from the same base value.
``no-phantom``   a read returned a value that no write attempted.
``no-ghost``     a read returned the value of a write that was answered as failed.
"""

_CREATES = ("put_inm", "mpu_inm")
_WRITES = ("put", "put_inm", "put_im", "mpu_inm")


@dataclass(frozen=True, slots=True, kw_only=True)
class InvariantViolation:
    """A broken invariant and the operations that break it."""

    invariant: Invariant
    key: str
    ops: tuple[Op, ...]
    message: str


def key_invariants(key: str, ops: Sequence[Op]) -> list[InvariantViolation]:
    """Invariant violations among the operations of one key."""
    found: list[InvariantViolation] = []
    attempted = {o.value for o in ops if o.kind in _WRITES and o.result in ("ok", "info")}
    failed = {
        o.value: o
        for o in ops
        if o.kind in _WRITES and o.result in ("fail", "conflict", "noeffect")
    }
    creates = tuple(o for o in ops if o.kind in _CREATES and o.result == "ok")
    deletes = sum(1 for o in ops if o.kind == "del_im" and o.result in ("ok", "info"))
    if len(creates) > 1 + deletes:
        found.append(
            InvariantViolation(
                invariant="create-once",
                key=key,
                ops=creates,
                message=f"{len(creates)} creates succeeded, "
                f"but at most {deletes} deletes may have happened",
            )
        )
    winner_from: dict[str | None, Op] = {}
    for o in ops:
        if o.kind in ("put_im", "del_im") and o.result == "ok":
            first = winner_from.get(o.base_value)
            if first is None:
                winner_from[o.base_value] = o
            else:
                found.append(
                    InvariantViolation(
                        invariant="cas-unique",
                        key=key,
                        ops=(first, o),
                        message=f"ops {first.id} and {o.id} both succeeded "
                        f"with If-Match on value {o.base_value!r}",
                    )
                )
    for o in ops:
        if o.kind != "get" or o.result != "ok" or o.read_value is None:
            continue
        ghost = failed.get(o.read_value)
        if ghost is not None:
            found.append(
                InvariantViolation(
                    invariant="no-ghost",
                    key=key,
                    ops=(o, ghost),
                    message=f"op {o.id} read {o.read_value!r}, "
                    f"written by op {ghost.id}, which was answered {ghost.result}",
                )
            )
        elif o.read_value not in attempted:
            found.append(
                InvariantViolation(
                    invariant="no-phantom",
                    key=key,
                    ops=(o,),
                    message=f"op {o.id} read {o.read_value!r}, which no write attempted",
                )
            )
    return found


def check_invariants(history: Iterable[Op]) -> list[InvariantViolation]:
    """Check the invariants on every key of a history.

    Faster than :func:`s3lin.check`, but it misses anomalies such as stale reads.
    """
    found: list[InvariantViolation] = []
    for key, ops in group_by_key(history).items():
        found.extend(key_invariants(key, ops))
    return found
