"""Linearizability checking of S3 conditional writes, one key at a time.

Each key is a register whose state is the value of its current version, or None
when it is absent. The search is Wing and Gong's algorithm with Lowe's
refinements and Porcupine-style memoisation of (linearized set, state) pairs.
Indeterminate operations have no return event: the search may linearize them
anywhere after their call (as if they succeeded) or leave them out.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import ClassVar, Literal, TypeAlias, assert_never

from ._invariants import InvariantViolation, key_invariants
from ._model import Op, group_by_key

Verdict: TypeAlias = Literal["linearizable", "violation", "unknown"]


@dataclass(frozen=True, slots=True, kw_only=True)
class Linearizable:
    """The key's history is linearizable."""

    verdict: ClassVar[Literal["linearizable"]] = "linearizable"
    key: str
    ops_checked: int
    states_explored: int
    elapsed_s: float


@dataclass(frozen=True, slots=True, kw_only=True)
class Violation:
    """The key's history is not linearizable.

    ``prefix`` is the longest linearizable prefix the search found, in
    linearization order, and ``state_after_prefix`` the value the key holds after
    it (None: absent). ``blocking_op`` is the earliest-returning operation that is
    missing from that prefix: no linearization lets it take its place. This
    attribution is a heuristic; ``invariants`` lists any cheap invariant that the
    key's operations break, which names the culprits directly.
    """

    verdict: ClassVar[Literal["violation"]] = "violation"
    key: str
    ops_checked: int
    states_explored: int
    elapsed_s: float
    blocking_op: Op
    prefix: tuple[Op, ...]
    state_after_prefix: str | None
    invariants: tuple[InvariantViolation, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class Unknown:
    """The search hit its time or state limit before deciding.

    ``invariants`` may still prove the key non-linearizable.
    """

    verdict: ClassVar[Literal["unknown"]] = "unknown"
    key: str
    ops_checked: int
    states_explored: int
    elapsed_s: float
    limit: Literal["time", "states"]
    invariants: tuple[InvariantViolation, ...]


KeyResult: TypeAlias = Linearizable | Violation | Unknown


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Per-key results of :func:`check`, in order of each key's first appearance."""

    keys: tuple[KeyResult, ...]
    elapsed_s: float

    @property
    def verdict(self) -> Verdict:
        """``violation`` if any key has one, else ``unknown`` if any key is undecided."""
        if self.violations:
            return "violation"
        if self.unknown:
            return "unknown"
        return "linearizable"

    @property
    def violations(self) -> tuple[Violation, ...]:
        return tuple(r for r in self.keys if isinstance(r, Violation))

    @property
    def unknown(self) -> tuple[Unknown, ...]:
        return tuple(r for r in self.keys if isinstance(r, Unknown))


def step(state: str | None, op: Op) -> tuple[bool, str | None]:
    """Linearize ``op`` in ``state``: return (legal, new state).

    An indeterminate operation is applied as if it succeeded; leaving it out of the
    linearization covers the case where it never took effect.
    """
    ok = op.result in ("ok", "info")
    match op.kind:
        case "get":
            return state == op.read_value, state
        case "put":
            return ok, op.value  # an unconditional put has no reason to fail
        case "put_inm" | "mpu_inm":
            if ok:
                return state is None, op.value
            return state is not None, state
        case "put_im":
            if ok:
                return state == op.base_value, op.value
            return state != op.base_value, state
        case "del_im":
            if ok:
                return state == op.base_value, None
            return state != op.base_value, state
        case _:  # pragma: no cover
            assert_never(op.kind)


def constraining(op: Op) -> bool:
    """False for operations that tell nothing about the key's state.

    409 and other definite errors had no effect; a read that did not succeed
    returned nothing.
    """
    if op.result in ("conflict", "noeffect"):
        return False
    return op.kind != "get" or op.result == "ok"


class _Entry:
    """A call or return event in a circular doubly linked list with a sentinel."""

    __slots__ = ("idx", "is_call", "match", "next", "prev")

    def __init__(self, idx: int, is_call: bool) -> None:
        self.idx = idx
        self.is_call = is_call
        self.match: _Entry | None = None  # a call's return event; None if indeterminate
        self.prev: _Entry = self
        self.next: _Entry = self


def _lift(e: _Entry) -> None:
    e.prev.next = e.next
    e.next.prev = e.prev
    m = e.match
    if m is not None:
        m.prev.next = m.next
        m.next.prev = m.prev


def _unlift(e: _Entry) -> None:
    m = e.match
    if m is not None:
        m.prev.next = m
        m.next.prev = m
    e.prev.next = e
    e.next.prev = e


def _event_order(event: tuple[float, int, int, _Entry]) -> tuple[float, int, int]:
    return event[0], event[1], event[2]


def check_key(
    key: str,
    ops: Sequence[Op],
    *,
    time_limit: float = 30.0,
    state_limit: int = 5_000_000,
) -> KeyResult:
    """Check the operations of one key (all must have that key)."""
    start = time.perf_counter()
    kept = [o for o in ops if constraining(o)]
    n = len(kept)

    head = _Entry(-1, True)
    calls = [_Entry(i, True) for i in range(n)]
    events: list[tuple[float, int, int, _Entry]] = [
        (o.t0, 0, i, calls[i]) for i, o in enumerate(kept)
    ]
    for i, o in enumerate(kept):
        if o.t1 is not None:
            ret = _Entry(i, False)
            calls[i].match = ret
            events.append((o.t1, 1, i, ret))
    # Calls sort before returns at the same time, so such operations count as concurrent.
    events.sort(key=_event_order)
    for _t, _kind, _i, e in events:
        e.prev, e.next = head.prev, head
        head.prev.next = e
        head.prev = e

    state: str | None = None
    linearized = 0  # bitmask of the kept operations in the current partial linearization
    seen: set[tuple[int, str | None]] = set()
    stack: list[tuple[_Entry, str | None]] = []
    determinate_left = sum(1 for o in kept if o.t1 is not None)
    best: tuple[tuple[int, ...], str | None] = ((), None)
    best_len = 0
    best_unsaved = False  # the stack reached a new maximum that `best` does not hold yet
    states = 0
    entry = head.next

    def unknown(limit: Literal["time", "states"]) -> Unknown:
        return Unknown(
            key=key,
            ops_checked=n,
            states_explored=states,
            elapsed_s=time.perf_counter() - start,
            limit=limit,
            invariants=tuple(key_invariants(key, ops)),
        )

    while determinate_left > 0:
        if entry is head:  # pragma: no cover - unreachable, see below
            # An unlinearized determinate op keeps its return event in the list, and
            # the scan never passes a return event, so the scan cannot reach the end.
            raise AssertionError("linearizability search ran past the last event")
        if entry.is_call:
            legal, new_state = step(state, kept[entry.idx])
            if legal:
                new_linearized = linearized | (1 << entry.idx)
                memo = (new_linearized, new_state)
                if memo not in seen:
                    seen.add(memo)
                    states += 1
                    stack.append((entry, state))
                    state, linearized = new_state, new_linearized
                    if entry.match is not None:
                        determinate_left -= 1
                    _lift(entry)
                    if len(stack) > best_len:
                        best_len, best_unsaved = len(stack), True
                    if states > state_limit:
                        return unknown("states")
                    if states & 4095 == 0 and time.perf_counter() - start > time_limit:
                        return unknown("time")
                    entry = head.next
                    continue
            entry = entry.next
        else:
            # The return of an operation that is not linearized yet: backtrack.
            if best_unsaved:
                best = (tuple(e.idx for e, _ in stack), state)
                best_unsaved = False
            if not stack:
                return _violation(key, ops, kept, best, states, start)
            entry, state = stack.pop()
            linearized &= ~(1 << entry.idx)
            if entry.match is not None:
                determinate_left += 1
            _unlift(entry)
            entry = entry.next
    return Linearizable(
        key=key, ops_checked=n, states_explored=states, elapsed_s=time.perf_counter() - start
    )


def _violation(
    key: str,
    ops: Sequence[Op],
    kept: Sequence[Op],
    best: tuple[tuple[int, ...], str | None],
    states: int,
    start: float,
) -> Violation:
    prefix_idx, state_after = best
    in_prefix = set(prefix_idx)
    blocking: Op | None = None
    for i, o in enumerate(kept):
        if o.t1 is None or i in in_prefix:
            continue
        if blocking is None or (blocking.t1 is not None and o.t1 < blocking.t1):
            blocking = o
    # Some determinate op is always missing from the prefix, or the search would
    # have succeeded.
    assert blocking is not None
    return Violation(
        key=key,
        ops_checked=len(kept),
        states_explored=states,
        elapsed_s=time.perf_counter() - start,
        blocking_op=blocking,
        prefix=tuple(kept[i] for i in prefix_idx),
        state_after_prefix=state_after,
        invariants=tuple(key_invariants(key, ops)),
    )


def check(
    history: Iterable[Op],
    *,
    key_time_limit: float = 30.0,
    key_state_limit: int = 5_000_000,
) -> CheckResult:
    """Check whether a history of S3 operations is linearizable.

    Linearizability is compositional, so each key is checked on its own; the
    limits apply per key. Keys are assumed absent when the history starts.

    Raises HistoryError on duplicate op ids or on a value written twice to a key.
    """
    start = time.perf_counter()
    results = tuple(
        check_key(key, ops, time_limit=key_time_limit, state_limit=key_state_limit)
        for key, ops in group_by_key(history).items()
    )
    return CheckResult(keys=results, elapsed_s=time.perf_counter() - start)
