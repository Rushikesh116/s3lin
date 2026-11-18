# s3lin

s3lin checks whether a recorded history of concurrent S3 requests with conditional headers
(`If-None-Match: *`, `If-Match: <etag>`) is linearizable. If it is not, it tells you which
key broke and which operations break it.

Stores that call themselves S3-compatible often get conditional writes wrong: they ignore
the headers, check and write non-atomically, or acknowledge writes they lost. A test of the
form "N clients race, exactly one wins" catches some of this. A linearizability check also
catches stale reads, lost updates and resurrected deletes, and it handles requests whose
outcome is unknown (timeouts, resets, 5xx), which fault-injection tests produce in bulk.

s3lin comes from a study of Supabase Storage's S3 endpoint,
[Rushikesh116/supabase-storage-s3lin](https://github.com/Rushikesh116/supabase-storage-s3lin),
where I wrote the checker. This package is that checker with a typed API, a recorder and a
command line tool. The study's store adapters, workload generator, fault proxy and Docker
setup stay in the study repository. s3lin has no runtime dependencies.

## Install

```sh
pip install s3lin
```

## Quickstart

Record each request with a `Recorder`: call `invoke` just before you send it and `complete`
with the HTTP status you got back. Then check the history.

```python
from s3lin import Recorder, check

rec = Recorder()

# Two clients race to create "lock" with PutObject If-None-Match: *.
a = rec.invoke("put_inm", "lock", value="client-1", client=1)
b = rec.invoke("put_inm", "lock", value="client-2", client=2)
rec.complete(a, 200)  # the HTTP status each client got back
rec.complete(b, 200)  # a correct store answers one of them with 412

# Client 1 reads the key back.
r = rec.invoke("get", "lock", client=1)
rec.complete(r, 200, read_value="client-2")

result = check(rec.history())
print(result.verdict)
for v in result.violations:
    print(v.key, "is blocked at op", v.blocking_op.id, v.blocking_op.kind)
    for inv in v.invariants:
        print(f"{inv.invariant}: {inv.message}")
```

Output:

```text
violation
lock is blocked at op 2 put_inm
create-once: 2 creates succeeded, but at most 0 deletes may have happened
```

Every write must carry a unique value (its body), because s3lin identifies versions by
value. For `If-Match` requests, pass as `base_value` the value whose ETag you sent.

How to complete a call depends on what the client saw:

| the client saw | call | recorded as |
|---|---|---|
| an HTTP response | `rec.complete(call, status)` | 2xx `ok`, 412 `fail`, 409 `conflict`, 5xx `info` |
| a successful GET | `rec.complete(call, 200, read_value=body)` | `ok`; a 404 GET reads "absent" |
| a timeout, reset or closed connection after sending | `rec.complete(call, None)` | `info`: may or may not have happened |
| a refused connection (the request was never sent) | `rec.complete(call, None, sent=False)` | `noeffect` |

`outcome_from_status` documents the full mapping. Calls that are never completed count as
indeterminate.

## Command line

```sh
s3lin check examples/double-create.jsonl
```

```text
examples/double-create.jsonl: violation
  3 operations on 1 key, 0 indeterminate
  key 'lock': not linearizable (3 operations)
    blocking op 2 (client 2): put_inm writing 'client-2' -> ok, HTTP 200; invoked 0.001, returned 0.014
    longest linearizable prefix: 1 operation, after which the key is 'client-1'
    create-once: 2 creates succeeded, but at most 0 deletes may have happened
```

`s3lin check` takes any number of files (`.gz` files are decompressed) and exits with 0 if
every history is linearizable, 1 if any has a violation, 2 on bad input, and 3 if a search
limit left a key undecided. `--json` prints one JSON document instead;
`--key-time-limit` and `--key-state-limit` set the search limits per key (defaults: 30 s,
5,000,000 states). `python -m s3lin` works too.

## History files

A history file is JSON Lines, one operation per line, in the format the study recorded.
`write_history` and `read_history` write and read it; `Op.to_dict` returns one line as a
`TypedDict`.

| field | meaning |
|---|---|
| `id` | unique integer |
| `key` | object key |
| `op` | `get`, `put`, `put_inm` (If-None-Match: \*), `put_im` (If-Match), `del_im` (DeleteObject If-Match), `mpu_inm` (CompleteMultipartUpload If-None-Match: \*) |
| `t0`, `t1` | send and answer times in seconds on one clock; `t1` is `null` for `info` |
| `res` | `ok`, `fail`, `conflict`, `noeffect` or `info` |
| `val` | value written (every write) |
| `base_val` | value whose ETag the If-Match header carried (`put_im`, `del_im`) |
| `read_val` | value a `get` returned; `null` means 404 |
| `client`, `status` | optional: client id and HTTP status |

Other fields are ignored, and so are lines whose object has neither `id` nor `op` (run
metadata).

## How it works

Linearizability is compositional, so s3lin checks each key on its own. A key is a register
whose state is the value of its current version, or absent. For one key, s3lin searches for
a linearization with the Wing and Gong algorithm as refined by Lowe, memoising the pairs
(set of linearized operations, state) as [Porcupine](https://github.com/anishathalye/porcupine)
does. An indeterminate operation gets no return event, so the search may place it anywhere
after its invocation (as a success) or leave it out. Requests that had no effect (409,
other 4xx, never sent) and reads that failed carry no information and are dropped. If no
linearization exists, the result names the longest linearizable prefix the search found and
the *blocking operation*: the earliest-returning operation missing from that prefix. Four
cheap invariants (`create-once`, `cas-unique`, `no-phantom`, `no-ghost`) then name the
culprit operations directly when they apply; each of them implies a violation.

The tests compare the search with a brute-force oracle on 1,000 small random histories
(600 from the study, 400 added here), and check 270 histories that are linearizable by
construction for false positives.

## Performance

Measured with `python tests/bench_check.py` on an Apple M5 MacBook with CPython 3.12
(synthetic linearizable histories, 5 clients per key, median of 5 runs):

| history | operations | check time |
|---|---|---|
| 1 key, 1,000 ops, 10% indeterminate | 1,000 | 0.017 s |
| 1,000 keys x 100 ops, 10% indeterminate | 100,000 | 0.16 s |
| 100 keys x 1,000 ops, 30% indeterminate | 100,000 | 8.8 s |
| 1 key, 10,000 ops, 10% indeterminate | 10,000 | 9.0 s |

Time grows faster than the number of operations on a key, and indeterminate writes make it
worse: they never return, so they stay in the search for the rest of the key's history.
Short-lived keys keep checks fast (the study rotated its hot keys every 2 s). The 54
histories recorded in the study (288,820 operations on 1,245 keys) read and check in 3.2 s,
and s3lin reports the same 468 violating keys, with the same blocking operations, as the
study's original checker.

## Limitations

- Versions are identified by value, so every write must carry a unique body; `check`
  rejects a history that writes one value twice to a key. s3lin never sees ETags: it trusts
  that the `base_value` you recorded is the version your ETag identified.
- Keys are assumed absent when the history starts. Record the writes that create them.
- Modelled requests: GetObject, PutObject (plain, If-None-Match: \*, If-Match),
  DeleteObject with If-Match, and CompleteMultipartUpload with If-None-Match: \*. Plain
  DeleteObject, CopyObject, conditional GETs, versioned buckets and listings are not
  modelled.
- `t0` and `t1` must come from one clock. Histories merged from machines with skewed clocks
  can show violations that did not happen.
- A linearizable verdict says that this history was linearizable, not that the store is
  correct. s3lin can only find anomalies that your workload provoked and recorded. It
  checks one key at a time, so it says nothing about multi-key atomicity or durability.
- Checking linearizability is NP-complete in general. Many concurrent indeterminate writes
  on one key make the search slow; the limits then give `unknown` instead of an answer.
- The blocking operation is a heuristic. The search does not find a unique culprit.

## Python versions and versioning

s3lin needs Python 3.11 or newer and has no dependencies. The test suite passes on these
interpreters (macOS arm64, wheel installed in a fresh virtual environment):

| CPython | result |
|---|---|
| 3.11.16 | 83 passed |
| 3.12.13 | 83 passed |
| 3.13.14 | 83 passed |
| 3.14.7 | 83 passed, also on the free-threaded build with the GIL disabled |
| 3.15.0 | 83 passed |

3.11 is the oldest CPython still supported upstream; 3.10 reached end of life in October
2026.

s3lin follows [Semantic Versioning](https://semver.org/). The public API is what
`s3lin.__all__` exports, the `s3lin` command (arguments, exit statuses, JSON output) and the
history file format. While the version is 0.x, a minor release may change the public API,
and the [changelog](CHANGELOG.md) lists every such change; patch releases only fix bugs.
s3lin supports the CPython versions that CI tests and that have not reached end of life.
Support for a version ends with the first minor release after its end of life.

## License

MIT. See [LICENSE](LICENSE).
