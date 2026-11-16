"""Positively selected authentic-source evidence and contained artifact controls."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import runpy
import shutil
import time
import urllib.error
import urllib.request
import venv
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manager_source_fixture as source
import manager_suite_fixture as native
from manager_suite_artifacts import _command


@pytest.fixture
def contained_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_procutil import _caller_owned

    original = _caller_owned.run

    def contained(*args: Any, **options: Any) -> subprocess.CompletedProcess[str]:
        options["allow_daemon_breakaway"] = False
        return original(*args, **options)

    monkeypatch.setattr(_caller_owned, "run", contained)


@pytest.fixture
def git_case(tmp_path_factory: pytest.TempPathFactory, contained_run: None) -> tuple[Path, Path, str, str]:
    tmp_path = tmp_path_factory.mktemp("git")
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


def test_real_source_acquisition_and_producer_provenance_share_the_owned_transport(
    git_case: tuple[Path, Path, str, str],
) -> None:
    from worktree_manager.runtime_source import acquire
    from worktree_manager.runtime_source_config import RuntimeSource

    case, repository, main, dev = git_case
    source.freeze_source(case, repository, main, dev)
    producer = runpy.run_path(str(native.REPO / "tools" / "local_install_source.py"))
    deadline = time.monotonic() + 60
    with source.source_transport(case, deadline=deadline) as url:
        with acquire(
            RuntimeSource("dev", repo=url, allow_dev=True), root=case / "acquired",
            environment=source._env(case), deadline=deadline,
        ) as resolved:
            assert resolved.commit == dev and resolved.main_head == main
            descriptor = producer["repository_source"](resolved.checkout, None, deadline=deadline)
            assert descriptor == {"kind": "git", "canonical": "git:" + url.removesuffix(".git"), "ref": ""}
            admitted = producer["preparation"]._admit(resolved.checkout, dev, "dev", deadline=deadline)
            assert admitted[1] == resolved.tree
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with pytest.raises(urllib.error.URLError):
        opener.open(url + "/HEAD", timeout=1)


def test_source_feed_survives_the_actual_scrubbed_subprocess_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contained_run: None,
) -> None:
    (tmp_path / "project").mkdir()
    index = "https://packages.example.invalid/simple"
    monkeypatch.setenv("UV_DEFAULT_INDEX", index)
    monkeypatch.setenv("UV_INDEX_URL", "https://not-selected.example.invalid/simple")
    monkeypatch.setenv("UV_EXTRA_INDEX_URL", "https://not-selected.example.invalid/extra")
    monkeypatch.setenv("GH_TOKEN", "fixture-not-a-token")
    observed = native._run([
        sys.executable, "-I", "-B", "-c",
        "import json,os; print(json.dumps({key:os.environ.get(key) for key in "
        "('UV_DEFAULT_INDEX','UV_INDEX_URL','UV_EXTRA_INDEX_URL','GH_TOKEN','HOME')}))",
    ], tmp_path, extra_env=source.source_feed_environment())
    assert json.loads(observed.stdout) == {
        "UV_DEFAULT_INDEX": index, "UV_INDEX_URL": None, "UV_EXTRA_INDEX_URL": None,
        "GH_TOKEN": None, "HOME": str(tmp_path / "h"),
    }


@pytest.mark.parametrize("index", [
    "https://pypi.org/simple", "http://packages.example.invalid/simple",
    "https://user:password@packages.example.invalid/simple",
    "https://packages.example.invalid/simple?query=value",
    "https://packages.example.invalid/simple#fragment",
])
def test_source_feed_cannot_weaken_the_governed_url_only_contract(
    index: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UV_DEFAULT_INDEX", index)
    with pytest.raises(ValueError):
        source.source_feed_environment()


def test_transport_starts_in_a_fresh_isolated_fixture_interpreter(
    git_case: tuple[Path, Path, str, str],
) -> None:
    case, repository, main, dev = git_case
    source.freeze_source(case, repository, main, dev)
    program = """
import sys, time
from pathlib import Path
from agent_procutil import _caller_owned
native = Path(sys.argv[1])
tools = native.parents[2] / 'tools'
assert sys.flags.isolated == 1
assert all(Path(value).resolve() != tools.resolve() for value in sys.path if value)
original = _caller_owned.run
def contained(*args, **options):
    options['allow_daemon_breakaway'] = False
    return original(*args, **options)
_caller_owned.run = contained
sys.path.insert(0, str(native))
import manager_source_fixture as source
with source.source_transport(Path(sys.argv[2]), deadline=time.monotonic()+60) as url:
    assert url.startswith('http://127.0.0.1:')
print('isolated transport served and closed')
"""
    observed = native._run(
        [sys.executable, "-I", "-B", "-c", program, Path(__file__).parent, case], case, timeout=90,
    )
    assert observed.stdout.strip() == "isolated transport served and closed"


def test_fresh_main_preparation_preserves_the_actual_donor_provenance(
    git_case: tuple[Path, Path, str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    case, repository, main, dev = git_case
    native._git(case, repository, "remote", "add", "origin", "https://example.invalid/runtime.git")
    descriptor = source.repository_provenance(repository, deadline=time.monotonic() + 60)
    assert descriptor == {"kind": "git", "canonical": "git:https://example.invalid/runtime", "ref": ""}
    frozen = source.freeze_source(case, repository, main, dev)
    snapshots = {name: case / name for name in source.COMPONENTS}
    for path in snapshots.values():
        path.mkdir()
    execute = native._run
    which = shutil.which
    calls: list[dict[str, str]] = []

    def run(argv: list[Any], directory: Path, **options: Any) -> subprocess.CompletedProcess[str]:
        if "--prepare-applications" not in argv:
            return execute(argv, directory, **options)
        observed = json.loads(argv[argv.index("--source-descriptor") + 1])
        assert observed == descriptor and argv[argv.index("--branch") + 1] == "main"
        assert "--timeout" not in argv and "--deadline" in argv
        calls.append(observed)
        return subprocess.CompletedProcess(argv, 0, json.dumps({name: str(path) for name, path in snapshots.items()}), "")

    monkeypatch.setattr(shutil, "which", lambda name: sys.executable if name == "uv" else which(name))
    monkeypatch.setattr(native, "_run", run)
    observed = source._applications(case, "main", frozen, sys.executable, descriptor=descriptor)
    assert calls == [descriptor] and set(observed) == set(source.COMPONENTS)


def test_unattributed_donor_refuses_before_application_preparation(
    git_case: tuple[Path, Path, str, str],
) -> None:
    _case, repository, _main, _dev = git_case
    with pytest.raises(ValueError, match="donor's portable repository provenance"):
        source.repository_provenance(repository, deadline=time.monotonic() + 60)


@pytest.mark.skipif(os.name != "nt" or shutil.which("pwsh") is None, reason="Native Windows frontdoors")
@pytest.mark.parametrize("stale", ("cmd", "ps1"))
def test_manager_frontdoors_reject_a_stale_interpreter_with_the_same_package_version(
    tmp_path: Path, contained_run: None, stale: str,
) -> None:
    case = tmp_path
    (case / "project").mkdir()
    interpreters = {}
    for generation in ("main", "dev"):
        environment = case / "interpreters" / generation
        venv.EnvBuilder(with_pip=False).create(environment)
        interpreters[generation] = environment / "Scripts" / "python.exe"
    program = case / "capability.py"
    program.write_text(
        "import sys\n"
        f"sys.path.insert(0,{str(native.REPO / 'worktree-manager' / 'src')!r})\n"
        "from worktree_manager import __version__,terminal_launch\n"
        "if not terminal_launch.answer_query(['--version']):\n"
        " print('worktree-manager '+__version__)\n", encoding="utf-8",
    )
    bin_dir = case / "h" / ".local" / "bin"
    bin_dir.mkdir(parents=True)

    def write(extension: str, generation: str) -> None:
        python = interpreters[generation]
        body = (
            f'@echo off\n"{python}" -I -B "{program}" %*\nexit /b %errorlevel%\n'
            if extension == "cmd" else
            f"& '{python}' -I -B '{program}' @args\nexit $LASTEXITCODE\n"
        )
        (bin_dir / ("worktree-manager." + extension)).write_text(body, encoding="utf-8")

    for extension in ("cmd", "ps1"):
        write(extension, "dev")
    assert len(source.manager_frontdoors(case, interpreters["dev"])) == 2
    before = native._run(_command(case, "worktree-manager", "--version", extension=stale), case).stdout
    write(stale, "main")
    after = native._run(_command(case, "worktree-manager", "--version", extension=stale), case).stdout
    assert before == after
    with pytest.raises(RuntimeError, match=stale + " launcher routes to another physical generation"):
        source.manager_frontdoors(case, interpreters["dev"])


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
