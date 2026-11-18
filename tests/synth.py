"""Synthetic histories: a correct atomic register-with-ETag plus random concurrency.

Each op gets an interval [t0, t1] and a linearization point inside it; ops are applied
to a sequential model in linearization-point order, so the history is linearizable by
construction. Indeterminate ops (res 'info', t1 None) hide an op's outcome; with
probability 1/2 such an op took effect, otherwise it never happened.

This is the generator from the study, unchanged in its random choices, so the tests run
on the same histories. ``gen`` returns JSON-style records; ``to_ops`` builds Op objects.
"""

from __future__ import annotations

import random

from s3lin import Op

Record = dict[str, object]
DEFAULT_MIX = {"get": 0.35, "put_im": 0.4, "put_inm": 0.15, "del_im": 0.1}


def gen(
    n_ops: int = 200,
    n_clients: int = 6,
    p_info: float = 0.0,
    mix: dict[str, float] | None = None,
    seed: int = 0,
    key: str = "k",
    first_id: int = 0,
) -> list[Record]:
    rng = random.Random(seed)
    kinds, weights = zip(*(mix or DEFAULT_MIX).items(), strict=True)
    t = [0.0] * n_clients  # each client issues ops back to back
    ops: list[Record] = []
    lps: dict[int, float] = {}
    for i in range(first_id, first_id + n_ops):
        c = rng.randrange(n_clients)
        t0 = t[c] + rng.expovariate(1.0) * 0.2
        t1 = t0 + (rng.expovariate(1.0) * 1.0 + 0.01)  # same float ops as the study
        lps[i] = rng.uniform(t0, t1)
        t[c] = t1
        ops.append(
            {
                "id": i,
                "client": c,
                "key": key,
                "op": rng.choices(kinds, weights)[0],
                "t0": t0,
                "t1": t1,
            }
        )
    ops.sort(key=lambda o: lps[_int(o["id"])])
    state: str | None = None
    written: list[str] = []  # values ever written (pool for stale bases)
    for o in ops:
        info = o["op"] != "get" and rng.random() < p_info
        apply_effect = (not info) or rng.random() < 0.5
        k = o["op"]
        if k in ("put_im", "del_im"):
            # base: usually the current value, sometimes a stale one
            if state is not None and rng.random() < 0.6:
                o["base_val"] = state
            elif written:
                o["base_val"] = rng.choice(written[-5:])
            else:
                o["base_val"] = "none"
        if k in ("put", "put_im", "put_inm", "mpu_inm"):
            o["val"] = f"v{o['id']}"
        if k == "get":
            o["res"], o["read_val"] = "ok", state
            continue
        if k == "put":
            ok = True
        elif k in ("put_inm", "mpu_inm"):
            ok = state is None
        else:
            ok = state == o["base_val"]
        if info:
            o["res"], o["t1"] = "info", None
            if ok and apply_effect:
                state = _apply(o, written)
            continue
        o["res"] = "ok" if ok else "fail"
        if ok:
            state = _apply(o, written)
    ops.sort(key=lambda o: _int(o["id"]))
    return ops


def _int(x: object) -> int:
    assert isinstance(x, int)
    return x


def _apply(o: Record, written: list[str]) -> str | None:
    if o["op"] == "del_im":
        return None
    val = o["val"]
    assert isinstance(val, str)
    written.append(val)
    return val


def to_ops(records: list[Record]) -> list[Op]:
    return [Op.from_dict(r) for r in records]
