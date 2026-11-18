"""The command line tool, and the README examples run verbatim."""

from __future__ import annotations

import contextlib
import io
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from s3lin import Op, write_history
from s3lin._cli import main

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "double-create.jsonl"


def readme_block(heading: str, lang: str, index: int = 0) -> str:
    """The index-th fenced block of a language in a README section."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split(f"\n## {heading}\n", 1)[1].split("\n## ", 1)[0]
    blocks: list[str] = re.findall(rf"```{lang}\n(.*?)```", section, flags=re.DOTALL)
    return blocks[index]


def test_readme_quickstart_runs_verbatim() -> None:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(compile(readme_block("Quickstart", "python"), "README.md", "exec"), {})
    assert out.getvalue() == readme_block("Quickstart", "text")


def test_readme_cli_example_runs_verbatim(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    command = readme_block("Command line", "sh").strip()
    assert command.startswith("s3lin ")
    monkeypatch.chdir(ROOT)
    assert main(command.split()[1:]) == 1
    assert capsys.readouterr().out == readme_block("Command line", "text")


def _history(tmp_path: Path, name: str, ops: list[Op]) -> str:
    path = tmp_path / name
    write_history(ops, path)
    return str(path)


def _ok(tmp_path: Path) -> str:
    return _history(
        tmp_path,
        "ok.jsonl.gz",
        [Op(id=1, key="k", kind="put_inm", t0=0, t1=1, result="ok", value="a")],
    )


def _undecided(tmp_path: Path) -> str:
    ops = [
        Op(id=i, key="k", kind="put", t0=0, t1=None, result="info", value=f"v{i}")
        for i in range(13)
    ]
    ops.append(Op(id=99, key="k", kind="get", t0=5, t1=6, result="ok", read_value="zzz"))
    return _history(tmp_path, "undecided.jsonl", ops)


def test_linearizable_exit_0(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check", _ok(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert out.endswith("ok.jsonl.gz: linearizable\n  1 operation on 1 key, 0 indeterminate\n")


def test_unknown_exit_3(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check", "--key-state-limit", "10", _undecided(tmp_path)]) == 3
    out = capsys.readouterr().out
    assert "undecided, hit the states limit" in out
    assert "no-phantom: op 99 read 'zzz', which no write attempted" in out


def test_bad_input_exit_2_and_other_files_still_checked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"id": 1, "key": "k", "op": "get"}\n')
    assert main(["check", str(bad), _ok(tmp_path), str(tmp_path / "missing.jsonl")]) == 2
    captured = capsys.readouterr()
    assert "bad.jsonl:1: missing field 't0'" in captured.err
    assert "missing.jsonl: error:" in captured.err
    assert "ok.jsonl.gz: linearizable" in captured.out


def test_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        [
            "check",
            "--json",
            str(EXAMPLE),
            _ok(tmp_path),
            _undecided(tmp_path),
            str(tmp_path / "nope"),
        ]
    )
    assert code == 2
    files = json.loads(capsys.readouterr().out)["files"]
    assert [f.get("verdict") for f in files] == ["violation", "linearizable", "violation", None]
    (problem,) = files[0]["problems"]
    assert problem["blocking_op"]["id"] == 2
    assert problem["prefix"] == [1]
    assert problem["state_after_prefix"] == "client-1"
    assert problem["invariants"] == [
        {
            "invariant": "create-once",
            "ops": [1, 2],
            "message": "2 creates succeeded, but at most 0 deletes may have happened",
        }
    ]
    assert files[1]["problems"] == []
    assert files[2]["problems"][0]["blocking_op"]["id"] == 99
    assert "error" in files[3]


def test_json_unknown_entry(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check", "--json", "--key-time-limit", "0", _undecided(tmp_path)]) == 3
    (problem,) = json.loads(capsys.readouterr().out)["files"][0]["problems"]
    assert problem["verdict"] == "unknown"
    assert problem["limit"] == "time"


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert "usage: s3lin" in capsys.readouterr().err


def test_version_and_module_entry_point() -> None:
    run = subprocess.run(
        [sys.executable, "-m", "s3lin", "--version"], capture_output=True, text=True, check=True
    )
    assert run.stdout.strip() == "s3lin 0.1.0"
    run = subprocess.run(
        [sys.executable, "-m", "s3lin", "check", str(EXAMPLE)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 1
    assert "not linearizable" in run.stdout
