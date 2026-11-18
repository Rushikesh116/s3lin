# Contributing

Bug reports and pull requests are welcome at https://github.com/Rushikesh116/s3lin.
A history file that reproduces a wrong verdict is the most useful bug report.

## Set up

```sh
uv venv -p 3.12 .venv
uv pip install --python .venv/bin/python -e . --group dev
```

With pip 25.1 or newer: `python -m pip install -e . --group dev`.

## Check your change

```sh
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/mypy --strict src tests
.venv/bin/python -m pytest
.venv/bin/coverage run -m pytest && .venv/bin/coverage report
```

All five must pass; CI runs the same commands on every supported Python version. Type
checking is strict, and the package itself also forbids expressions of type `Any`.
`python tests/bench_check.py` prints timings if your change touches the search.

New behaviour needs a test. Changes to the search in `src/s3lin/_check.py` should keep
`tests/test_checker.py` passing unchanged: its brute-force comparison and synthetic
histories are what the checker's correctness rests on. Add a line to `CHANGELOG.md` under
"Unreleased" for anything a user would notice.
