"""Benchmark: time to check synthetic linearizable histories.

Run with ``python tests/bench_check.py``; not collected by pytest.
"""

from __future__ import annotations

import platform
import statistics
import sys
import time

import synth
from s3lin import Op, check


def history(keys: int, ops_per_key: int, p_info: float) -> list[Op]:
    ops: list[Op] = []
    for k in range(keys):
        records = synth.gen(
            n_ops=ops_per_key, n_clients=5, p_info=p_info, seed=k, key=f"k{k}", first_id=len(ops)
        )
        ops.extend(synth.to_ops(records))
    return ops


def main() -> None:
    print(f"Python {platform.python_version()} on {platform.machine()} ({sys.platform})")
    print("keys x ops/key, indeterminate share: median of 5 runs")
    for keys, per_key, p_info in [
        (1, 1_000, 0.1),
        (1, 10_000, 0.1),
        (1_000, 100, 0.1),
        (100, 1_000, 0.3),
    ]:
        h = history(keys, per_key, p_info)
        result = check(h)
        runs: list[float] = []
        for _ in range(5):
            start = time.perf_counter()
            check(h)
            runs.append(time.perf_counter() - start)
        assert result.verdict == "linearizable"
        states = sum(r.states_explored for r in result.keys)
        print(
            f"{keys:>5} x {per_key:>6,}  p_info {p_info}: {len(h):>7,} ops, "
            f"{statistics.median(runs):.3f} s, {states:,} states"
        )


if __name__ == "__main__":
    main()
