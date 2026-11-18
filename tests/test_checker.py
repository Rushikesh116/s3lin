"""The study's 26 checker tests, ported to pytest and the typed API.

Same histories and expectations: 21 hand-written patterns, 3 invariant checks, 150
synthetic linearizable histories and a 600-trial comparison with a brute-force oracle.
"""

from __future__ import annotations

import itertools
import random

import pytest

import synth
from s3lin import Op, OpKind, Outcome, Verdict, check, check_invariants
from s3lin._check import check_key, constraining, step

_id = itertools.count()


def op(
    kind: OpKind,
    t0: float,
    t1: float | None,
    res: Outcome = "ok",
    val: str | None = None,
    base_val: str | None = None,
    read_val: str | None = None,
    client: int = 0,
) -> Op:
    return Op(
        id=next(_id),
        client=client,
        key="k",
        kind=kind,
        t0=t0,
        t1=t1 if res != "info" else None,
        result=res,
        value=val,
        base_value=base_val,
        read_value=read_val,
    )


def lin(ops: list[Op]) -> Verdict:
    return check_key("k", ops).verdict


def brute_force(ops: list[Op]) -> Verdict:
    """Exhaustive oracle for tiny histories: try every subset of indeterminate ops and
    every real-time-respecting order."""
    kept = [o for o in ops if constraining(o)]
    det = [o for o in kept if o.result != "info"]
    ind = [o for o in kept if o.result == "info"]
    inf = float("inf")
    for r in range(len(ind) + 1):
        for sub in itertools.combinations(ind, r):
            for perm in itertools.permutations(det + list(sub)):
                ok, state = True, None
                for i, a in enumerate(perm):
                    # real time: no later op in perm may have completed before a was invoked
                    if any((b.t1 if b.t1 is not None else inf) < a.t0 for b in perm[i + 1 :]):
                        ok = False
                        break
                    legal, state = step(state, a)
                    if not legal:
                        ok = False
                        break
                if ok:
                    return "linearizable"
    return "violation"


# ---------- linearizable histories: must pass ----------
def test_simple_sequence() -> None:
    h = [
        op("put_inm", 0, 1, val="a"),
        op("get", 2, 3, read_val="a"),
        op("put_im", 4, 5, val="b", base_val="a"),
        op("put_im", 6, 7, res="fail", val="c", base_val="a"),
        op("get", 8, 9, read_val="b"),
        op("del_im", 10, 11, base_val="b"),
        op("get", 12, 13, read_val=None),
    ]
    assert lin(h) == "linearizable"


def test_concurrent_creates_one_wins() -> None:
    h = [
        op("put_inm", 0, 5, val="a"),
        op("put_inm", 1, 4, res="fail", val="b"),
        op("put_inm", 2, 6, res="fail", val="c"),
        op("get", 7, 8, read_val="a"),
    ]
    assert lin(h) == "linearizable"


def test_concurrent_read_sees_either() -> None:
    h = [
        op("put_inm", 0, 1, val="a"),
        op("put_im", 2, 6, val="b", base_val="a"),
        op("get", 3, 4, read_val="b"),
        op("get", 3, 7, read_val="b"),
    ]
    assert lin(h) == "linearizable"


def test_info_write_observed() -> None:
    h = [op("put_inm", 0, None, res="info", val="a"), op("get", 5, 6, read_val="a")]
    assert lin(h) == "linearizable"


def test_info_write_explains_412() -> None:
    h = [op("put_inm", 0, None, res="info", val="a"), op("put_inm", 5, 6, res="fail", val="b")]
    assert lin(h) == "linearizable"


def test_info_write_never_happened() -> None:
    h = [
        op("put_inm", 0, None, res="info", val="a"),
        op("put_inm", 5, 6, val="b"),
        op("get", 7, 8, read_val="b"),
    ]
    assert lin(h) == "linearizable"


def test_409_is_noeffect() -> None:
    h = [
        op("put_inm", 0, 1, val="a"),
        op("put_im", 2, 3, res="conflict", val="b", base_val="a"),
        op("get", 4, 5, read_val="a"),
    ]
    assert lin(h) == "linearizable"


def test_info_late_effect() -> None:
    # info write applied long after its invocation (e.g. a delayed retry) is allowed
    h = [
        op("put_inm", 0, 1, val="a"),
        op("put_im", 1, None, res="info", val="b", base_val="a"),
        op("get", 5, 6, read_val="a"),
        op("get", 10, 11, read_val="b"),
    ]
    assert lin(h) == "linearizable"


# ---------- known anomaly patterns: must be flagged ----------
def test_double_create_sequential() -> None:  # Garage / MinIO #21526 class
    h = [op("put_inm", 0, 1, val="a"), op("put_inm", 2, 3, val="b")]
    assert lin(h) == "violation"


def test_double_create_concurrent() -> None:  # SeaweedFS #12208 class
    h = [op("put_inm", 0, 5, val="a"), op("put_inm", 1, 4, val="b")]
    assert lin(h) == "violation"


def test_cas_double_win() -> None:  # ignored If-Match / lost update
    h = [
        op("put_inm", 0, 1, val="a"),
        op("put_im", 2, 5, val="b", base_val="a"),
        op("put_im", 3, 6, val="c", base_val="a"),
    ]
    assert lin(h) == "violation"


def test_double_conditional_delete() -> None:  # SeaweedFS #12619 class
    h = [
        op("put_inm", 0, 1, val="a"),
        op("del_im", 2, 5, base_val="a"),
        op("del_im", 3, 6, base_val="a"),
    ]
    assert lin(h) == "violation"


def test_stale_read() -> None:
    h = [
        op("put_inm", 0, 1, val="a"),
        op("put_im", 2, 3, val="b", base_val="a"),
        op("get", 4, 5, read_val="a"),
    ]
    assert lin(h) == "violation"


def test_lost_ack() -> None:  # ceph #72109 class: 200 but write never happened
    h = [
        op("put_inm", 0, 1, val="a"),
        op("put_im", 2, 3, val="b", base_val="a"),
        op("put_im", 4, 5, val="c", base_val="a"),
    ]
    assert lin(h) == "violation"


def test_ghost_write() -> None:  # 412 but the write is visible
    h = [
        op("put_inm", 0, 1, val="a"),
        op("put_im", 2, 3, res="fail", val="b", base_val="a"),
        op("get", 4, 5, read_val="b"),
    ]
    assert lin(h) == "violation"


def test_spurious_412() -> None:  # 412 although the precondition held and nobody else wrote
    h = [
        op("put_inm", 0, 1, val="a"),
        op("put_im", 2, 3, res="fail", val="b", base_val="a"),
        op("get", 4, 5, read_val="a"),
    ]
    assert lin(h) == "violation"


def test_phantom_read() -> None:
    h = [op("put_inm", 0, 1, val="a"), op("get", 2, 3, read_val="zzz")]
    assert lin(h) == "violation"


def test_nonmonotonic_reads() -> None:
    h = [
        op("put_inm", 0, 1, val="a"),
        op("put_im", 2, 6, val="b", base_val="a"),
        op("get", 3, 4, read_val="b", client=1),
        op("get", 4.5, 5, read_val="a", client=1),
    ]
    assert lin(h) == "violation"


def test_read_before_invoke() -> None:  # value visible before its write was invoked
    h = [op("get", 0, 1, read_val="a"), op("put_inm", 2, None, res="info", val="a")]
    assert lin(h) == "violation"


def test_info_cannot_flip_flop() -> None:
    h = [
        op("put_inm", 0, 1, val="a"),
        op("put_im", 1, None, res="info", val="b", base_val="a"),
        op("get", 5, 6, read_val="b"),
        op("get", 7, 8, read_val="a"),
    ]
    assert lin(h) == "violation"


def test_resurrection_after_delete() -> None:
    h = [
        op("put_inm", 0, 1, val="a"),
        op("del_im", 2, 3, base_val="a"),
        op("get", 4, 5, read_val="a"),
    ]
    assert lin(h) == "violation"


# ---------- invariants ----------
def test_invariants_miss_stale_read() -> None:
    h = [
        op("put_inm", 0, 1, val="a"),
        op("put_im", 2, 3, val="b", base_val="a"),
        op("get", 4, 5, read_val="a"),
    ]
    assert check_invariants(h) == []  # only the linearizability check catches it


def test_invariants_catch_double_create() -> None:
    h = [op("put_inm", 0, 1, val="a"), op("put_inm", 2, 3, val="b")]
    assert check_invariants(h)[0].invariant == "create-once"


def test_invariants_catch_ghost() -> None:
    h = [
        op("put_inm", 0, 1, val="a"),
        op("put_im", 2, 3, res="fail", val="b", base_val="a"),
        op("get", 4, 5, read_val="b"),
    ]
    assert check_invariants(h)[0].invariant == "no-ghost"


# ---------- randomized ----------
def test_no_false_positives_synthetic() -> None:
    """Linearizable-by-construction histories, with and without indeterminate ops."""
    for seed in range(150):
        p_info = [0.0, 0.1, 0.3][seed % 3]
        h = synth.to_ops(synth.gen(n_ops=60, n_clients=5, p_info=p_info, seed=seed))
        r = check(h).keys[0]
        assert r.verdict == "linearizable", f"seed {seed} p_info {p_info}: {r}"


def mutated_tiny_histories(
    rng: random.Random, trials: int, mix: dict[str, float] | None = None
) -> list[list[Op]]:
    """Tiny random histories, 70% of them with one outcome or read value flipped."""
    out: list[list[Op]] = []
    for trial in range(trials):
        h = synth.gen(n_ops=rng.randint(2, 6), n_clients=3, p_info=0.25, seed=1000 + trial, mix=mix)
        if rng.random() < 0.7:
            o = rng.choice(h)
            if o["op"] == "get":
                vals = [x.get("val") for x in h if x.get("val")] + [None]
                o["read_val"] = rng.choice(vals)
            elif o["res"] == "ok":
                o["res"] = "fail"
            elif o["res"] == "fail":
                o["res"] = "ok"
        out.append(synth.to_ops(h))
    return out


def test_differential_vs_bruteforce() -> None:
    """Tiny random (often non-linearizable) histories: checker must agree with brute force."""
    n_viol = n_ok = 0
    for trial, h in enumerate(mutated_tiny_histories(random.Random(7), 600)):
        expected = brute_force(h)
        assert lin(h) == expected, f"trial {trial}: {h}"
        n_viol += expected == "violation"
        n_ok += expected == "linearizable"
    assert n_viol > 50
    assert n_ok > 50


@pytest.mark.parametrize("p_info", [0.0, 0.3])
def test_no_false_positives_all_kinds(p_info: float) -> None:
    """Added in the library: the same check with every operation kind in the mix."""
    mix = {"get": 0.3, "put": 0.1, "put_inm": 0.15, "mpu_inm": 0.1, "put_im": 0.25, "del_im": 0.1}
    for seed in range(60):
        h = synth.to_ops(synth.gen(n_ops=60, n_clients=5, p_info=p_info, seed=seed, mix=mix))
        assert lin(h) == "linearizable", f"seed {seed}"


def test_differential_vs_bruteforce_all_kinds() -> None:
    """Added in the library: brute-force agreement with every kind, and invariant soundness:
    an invariant never fires on a history the oracle finds linearizable."""
    mix = {"get": 0.3, "put": 0.1, "put_inm": 0.15, "mpu_inm": 0.1, "put_im": 0.25, "del_im": 0.1}
    n_viol = 0
    for trial, h in enumerate(mutated_tiny_histories(random.Random(11), 400, mix)):
        expected = brute_force(h)
        assert lin(h) == expected, f"trial {trial}: {h}"
        if expected == "linearizable":
            assert check_invariants(h) == [], f"trial {trial}: {h}"
        n_viol += expected == "violation"
    assert n_viol > 50
