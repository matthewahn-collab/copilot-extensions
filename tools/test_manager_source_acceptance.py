"""The authentic-source selection retains the existing runner's lifetime bounds."""
from __future__ import annotations

import json
from pathlib import Path
import runpy
import subprocess
import sys
from typing import Any

import pytest

RUNNER = Path(__file__).with_name("run-manager-suite-acceptance.py")


def test_fresh_interpreter_bootstraps_the_runner_without_installed_packages(tmp_path: Path) -> None:
    from agent_procutil import no_window_kwargs

    base = Path(getattr(sys, "_base_executable", sys.executable))
    result = subprocess.run(
        [base, "-E", "-S", str(RUNNER), "--help"], cwd=tmp_path,
        capture_output=True, text=True, timeout=15, **no_window_kwargs(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "--main-commit" in result.stdout and "--dev-commit" in result.stdout


@pytest.mark.parametrize("failed", ("prepare", "exercise", "assertions", "settle", "exception", None))
def test_source_stages_keep_bounds_selection_and_unconditional_settlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed: str | None,
) -> None:
    runner = runpy.run_path(str(RUNNER))
    calls: list[str] = []

    def run(argv: list[str], *, timeout: float) -> int:
        stage = next((name for name in ("prepare", "exercise", "settle")
                      if "--" + name in argv), "assertions")
        calls.append(stage)
        assert timeout == {"prepare": 1800, "exercise": 900, "assertions": 300, "settle": 90}[stage]
        assert "test_manager_source_replacement.py" in " ".join(argv)
        assert ("--main-commit" in argv) is (stage == "prepare")
        assert ("--dev-commit" in argv) is (stage == "prepare")
        if stage == "prepare":
            assert argv[-4:] == ["--main-commit", "a" * 40, "--dev-commit", "b" * 40]
            if failed == "exception":
                raise subprocess.TimeoutExpired(argv, timeout)
        elif stage == "assertions":
            assert argv[-1] == "test_installed_manager_updates_frozen_source"
        return int(stage == failed)

    qualify = runner["qualify"]
    monkeypatch.setitem(qualify.__globals__, "_run", run)
    monkeypatch.setenv("MANAGER_SUITE_ACCEPTANCE_DATA", "previous-owner")
    assert qualify(
        tmp_path, "source", Path(sys.executable), main_commit="a" * 40, dev_commit="b" * 40,
    ) == int(failed is not None)
    expected: dict[str | None, list[str]] = {
        "prepare": ["prepare", "settle"], "exception": ["prepare", "settle"],
        "exercise": ["prepare", "exercise", "settle"],
    }
    assert calls == expected.get(failed, ["prepare", "exercise", "assertions", "settle"])
    assert runner["os"].environ["MANAGER_SUITE_ACCEPTANCE_DATA"] == "previous-owner"
    report = json.loads((tmp_path / "source-runner.json").read_text(encoding="utf-8"))
    assert report["stages"]["settle"] == int(failed == "settle")


@pytest.mark.parametrize("commits", ([], ["--main-commit", "a" * 40], ["--dev-commit", "b" * 40]))
def test_source_runner_requires_both_frozen_inputs_before_native_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, commits: list[str],
) -> None:
    runner: dict[str, Any] = runpy.run_path(str(RUNNER))
    root = tmp_path / "must-not-exist"
    monkeypatch.setattr(sys, "argv", [
        str(RUNNER), "--root", str(root), "--mode", "source", *commits,
    ])
    with pytest.raises(SystemExit) as result:
        runner["main"]()
    assert result.value.code == 2 and not root.exists()
