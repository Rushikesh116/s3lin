"""Recording a history while a workload runs."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from ._model import HistoryError, Op, OpKind, field_problem, outcome_from_status


@dataclass(frozen=True, slots=True)
class Call:
    """An operation that has been invoked and not completed; see Recorder.invoke."""

    id: int
    key: str
    kind: OpKind
    t0: float
    value: str | None
    base_value: str | None
    client: int | str


class Recorder:
    """Records operations from any number of threads.

    Call :meth:`invoke` just before sending a request and :meth:`complete` as soon
    as the answer (or the error) arrives. Times are seconds since the recorder was
    created, read from ``clock``. Taking them outside the request only widens each
    operation's interval, which keeps the check sound.
    """

    def __init__(self, *, clock: Callable[[], float] = time.perf_counter) -> None:
        self._clock = clock
        self._origin = clock()
        self._lock = threading.Lock()
        self._next_id = 1
        self._pending: dict[int, Call] = {}
        self._done: list[Op] = []

    def _now(self) -> float:
        return self._clock() - self._origin

    def invoke(
        self,
        kind: OpKind,
        key: str,
        *,
        value: str | None = None,
        base_value: str | None = None,
        client: int | str = 0,
    ) -> Call:
        """Start an operation. Writes need ``value``; If-Match ops need ``base_value``."""
        problem = field_problem(kind, value, base_value, None)
        if problem is not None:
            raise HistoryError(problem)
        with self._lock:
            call = Call(self._next_id, key, kind, self._now(), value, base_value, client)
            self._next_id += 1
            self._pending[call.id] = call
        return call

    def complete(
        self,
        call: Call,
        status: int | None,
        *,
        read_value: str | None = None,
        sent: bool = True,
    ) -> Op:
        """Finish an operation with the HTTP status it got, or None if there was no answer.

        A successful ``get`` must pass the ``read_value`` it read; a 404 means absent.
        Pass ``sent=False`` (with status None) when the request provably never left
        the client, for example when the connection was refused: it then had no
        effect, instead of an unknown one.
        """
        t1 = self._now()
        with self._lock:
            pending = self._pending.get(call.id) is call
        if not pending:
            raise ValueError(f"call {call.id} is not pending in this recorder")
        if not sent and status is not None:
            raise ValueError("sent=False means there was no answer, so status must be None")
        result = outcome_from_status(status, call.kind) if sent else "noeffect"
        if call.kind == "get" and result == "ok":
            if status == 404 and read_value is not None:
                raise ValueError("a 404 read has no value; pass read_value=None")
            if status != 404 and read_value is None:
                raise ValueError(f"a successful get (status {status}) must pass read_value")
        elif read_value is not None:
            raise ValueError("read_value is only for successful gets")
        op = Op(
            id=call.id,
            key=call.key,
            kind=call.kind,
            t0=call.t0,
            t1=None if result == "info" else t1,
            result=result,
            value=call.value,
            base_value=call.base_value,
            read_value=read_value,
            client=call.client,
            status=status,
        )
        with self._lock:
            if self._pending.get(call.id) is not call:
                raise ValueError(f"call {call.id} is not pending in this recorder")
            del self._pending[call.id]
            self._done.append(op)
        return op

    def history(self) -> list[Op]:
        """All operations so far, by id. Calls still pending count as indeterminate."""
        with self._lock:
            ops = list(self._done)
            pending = list(self._pending.values())
        ops.extend(
            Op(
                id=c.id,
                key=c.key,
                kind=c.kind,
                t0=c.t0,
                t1=None,
                result="info",
                value=c.value,
                base_value=c.base_value,
                client=c.client,
            )
            for c in pending
        )
        ops.sort(key=lambda o: o.id)
        return ops
