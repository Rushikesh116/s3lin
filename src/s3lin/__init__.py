"""Check recorded histories of S3 conditional writes for linearizability."""

__version__ = "0.1.0"

from ._check import CheckResult, KeyResult, Linearizable, Unknown, Verdict, Violation, check
from ._invariants import Invariant, InvariantViolation, check_invariants
from ._io import parse_history, read_history, write_history
from ._model import HistoryError, Op, OpKind, OpRecord, Outcome, outcome_from_status
from ._record import Call, Recorder

__all__ = [
    "Call",
    "CheckResult",
    "HistoryError",
    "Invariant",
    "InvariantViolation",
    "KeyResult",
    "Linearizable",
    "Op",
    "OpKind",
    "OpRecord",
    "Outcome",
    "Recorder",
    "Unknown",
    "Verdict",
    "Violation",
    "__version__",
    "check",
    "check_invariants",
    "outcome_from_status",
    "parse_history",
    "read_history",
    "write_history",
]
