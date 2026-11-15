"""Positively selected authentic-source evidence and contained artifact controls."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import runpy
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manager_source_fixture as source
import manager_suite_fixture as native


@pytest.fixture
def git_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, str, str]:
    from agent_procutil import _caller_owned

    original = _caller_owned.run

    def contained(*args: Any, **options: Any) -> subprocess.CompletedProcess[str]:
        options["allow_daemon_breakaway"] = False
        return original(*args, **options)

    monkeypatch.setattr(_caller_owned, "run", contained)
    case = tmp_path / "case"
    (case / "project").mkdir(parents=True)
    repository = tmp_path / "repository"
    repository.mkdir()
    native._git(case, repository, "init", "-q", "-b", "main", "--template=")
    (repository / "input").write_text("actual main\n", encoding="utf-8")
    main = native._commit(case, repository)
    native._git(case, repository, "update-ref", "refs/remotes/origin/main", main)
    native._git(case, repository, "checkout", "-qb", "dev")
    (repository / "input").write_text("actual merged dev\n", encoding="utf-8")
    dev = native._commit(case, repository)
    native._git(case, repository, "update-ref", "refs/remotes/origin/dev", dev)
    return case, repository, main, dev


def test_frozen_source_keeps_real_objects_and_is_independent_of_its_donor(
    git_case: tuple[Path, Path, str, str],
) -> None:
    case, repository, main, dev = git_case
    result = source.freeze_source(case, repository, main, dev)
    published = case / "published"
    assert result == {
        branch: {"commit": commit, "tree": native._git(case, repository, "rev-parse", commit + "^{tree}")}
        for branch, commit in (("main", main), ("dev", dev))
    }
    repository.rename(repository.with_name("donor-unavailable"))
    assert native._git(case, published, "show", "main:input") == "actual main"
    assert native._git(case, published, "show", "dev:input") == "actual merged dev"
    assert native._git(case, published, "rev-parse", "origin/main") == main
    assert native._git(case, published, "rev-parse", "origin/dev") == dev
    assert native._git(case, published, "status", "--porcelain=v1", "--untracked-files=all", "--ignored") == ""
    admission = runpy.run_path(str(native.REPO / "tools" / "prepare_runtime_source.py"))["_admit"]
    for branch, commit in (("main", main), ("dev", dev)):
        native._git(case, published, "checkout", "-q", branch)
        assert admission(published, commit, branch) == (commit, result[branch]["tree"])


@pytest.mark.parametrize("fault", ("abbreviated", "same", "unmerged"))
def test_source_fixture_refuses_unqualified_inputs_before_copying(
    git_case: tuple[Path, Path, str, str], fault: str,
) -> None:
    case, repository, main, dev = git_case
    if fault == "abbreviated":
        main = main[:12]
    elif fault == "same":
        dev = main
    else:
        (repository / "input").write_text("unmerged local change\n", encoding="utf-8")
        dev = native._commit(case, repository)
    with pytest.raises((ValueError, AssertionError)):
        source.freeze_source(case, repository, main, dev)
    assert not (case / "published").exists()


@pytest.mark.parametrize("index", (False, True))
def test_configuration_only_creates_an_index_host_when_declared(
    tmp_path: Path, index: bool,
) -> None:
    case = tmp_path
    published = case / "published" / "plugins"
    declared = {"agent-worktrees": {"scope": "universal", "version": "1.0"}}
    if index:
        declared["agent-index"] = {"scope": "machine-gated", "version": "1.0"}
    for name in declared:
        (published / name).mkdir(parents=True)
    (case / "project").mkdir()
    source._env(case)
    source._configuration(case, declared, index_host=index)
    assert (case / "h" / ".agent-index" / "config.yaml").exists() is index
    assert (case / "project" / ".copilot-extensions" / "agent-index" / "config.yaml").exists() is index
    settings = native._read(case / "h" / ".copilot" / "settings.json")
    assert set(settings["enabledPlugins"]) == {name + "@copilot-extensions" for name in declared}


def test_source_prepare_refuses_local_native_effects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(RuntimeError, match="disposable GitHub-hosted Windows VM"):
        source.prepare(tmp_path, "a" * 40, "b" * 40)
    assert not list(tmp_path.iterdir())


def test_installed_manager_updates_frozen_source() -> None:
    raw = os.environ.get("MANAGER_SUITE_ACCEPTANCE_DATA")
    assert raw, "Run the explicit source mode on its disposable hosted Windows owner"
    case = Path(raw).resolve() / "source"
    setup = native._read(case / "setup.json")
    result = native._read(case / "result.json")
    assert setup["schema"] == result["schema"] == source.SCHEMA
    assert setup["ready"] and not result["errors"], result["errors"]
    assert set(setup["declared"]) == {"agent-worktrees"}
    assert setup["frozen"]["main"]["commit"] != setup["frozen"]["dev"]["commit"]
    assert result["update"]["returncode"] == 0, result["update"]
    assert result["update"]["command"][-3:] == ["update", "--project", "example"], result["update"]
    assert result["durable_before"] == result["durable_after"]
    assert result["frozen_after"] == {branch: row["commit"] for branch, row in setup["frozen"].items()}
    for phase, branch in (("before", "main"), ("after", "dev")):
        assert set(result[phase]["applications"]) == set(source.COMPONENTS)
        for name, row in result[phase]["applications"].items():
            expected = setup["prepared"][branch][name]
            assert row["application"] == expected["application"], name
            identity = row["application"]["construction"]["identity"]["runtime_source"]
            assert identity["source_commit"] == setup["frozen"][branch]["commit"]
            assert identity["source_tree"] == setup["frozen"][branch]["tree"]
            assert expected["main_seed"] == setup["frozen"]["main"]["commit"]
            assert Path(row["origin"]["prefix"]).resolve() == (Path(row["slot"]) / ".venv").resolve()
            assert Path(row["origin"]["executable"]).is_relative_to(Path(row["origin"]["prefix"]))
            assert set(row["commands"]) == {name + ".cmd", name + ".ps1"}
            assert all(value["routes_current"] and value["proof"] for value in row["commands"].values())
            assert result["before"]["applications"][name]["generation"] != result["after"]["applications"][name]["generation"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    stages = parser.add_mutually_exclusive_group(required=True)
    stages.add_argument("--prepare", type=Path)
    stages.add_argument("--exercise", type=Path)
    stages.add_argument("--settle", type=Path)
    parser.add_argument("--mode", choices=("source",), required=True)
    parser.add_argument("--main-commit")
    parser.add_argument("--dev-commit")
    options = parser.parse_args()
    if options.prepare is not None:
        if not options.main_commit or not options.dev_commit:
            parser.error("source preparation requires frozen Main and merged-Dev commits")
        source.prepare(options.prepare, options.main_commit, options.dev_commit)
    elif options.exercise is not None:
        source.exercise(options.exercise)
    else:
        source.settle(options.settle)
