"""Authentic Main-to-merged-Dev two-application case for the native suite owner."""
from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
import re
import runpy
import shutil
import sys
import time
from typing import Any

import manager_suite_fixture as native
from manager_suite_artifacts import _DELIVERY, _command, _runtime_process_inventory_script, _write
from manager_suite_artifacts import _configuration as _configuration, _env as _env

SCHEMA = "worktree-manager.native-source-replacement.v1"
COMPONENTS = ("worktree-manager", "agent-worktrees")


def freeze_source(case: Path, repository: Path, main: str, dev: str) -> dict[str, Any]:
    """Copy real, reachable Git objects without materializing synthetic commits."""
    for commit, branch in ((main, "main"), (dev, "dev")):
        if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit) is None:
            raise ValueError("Source qualification requires full immutable commit IDs")
        native._git(case, repository, "merge-base", "--is-ancestor", commit, "refs/remotes/origin/" + branch)
    if main == dev:
        raise ValueError("Source qualification requires distinct Main and merged-Dev inputs")
    source = case / "published"
    source.mkdir()
    native._git(case, source, "init", "-q", "--template=")
    native._git(case, source, "fetch", "--no-tags", str(repository),
                main + ":refs/heads/main", main + ":refs/remotes/origin/main",
                dev + ":refs/heads/dev", dev + ":refs/remotes/origin/dev")
    native._git(case, source, "checkout", "-q", "main")
    frozen = {
        branch: {"commit": commit, "tree": native._git(case, source, "rev-parse", commit + "^{tree}")}
        for branch, commit in (("main", main), ("dev", dev))
    }
    if native._git(case, source, "status", "--porcelain=v1", "--untracked-files=all", "--ignored"):
        raise RuntimeError("Frozen source checkout is not clean")
    if (source / ".git" / "objects" / "info" / "alternates").exists():
        raise RuntimeError("Frozen source objects depend on the donor checkout")
    return frozen


def _applications(case: Path, branch: str, frozen: dict[str, Any], base: str) -> dict[str, str]:
    source = case / "published"
    native._git(case, source, "checkout", "-q", branch)
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("Source qualification requires uv")
    command = [
        base, "-I", "-B", "-X", "utf8", str(native.REPO / "tools" / "local_install_source.py"),
        "--source-checkout", str(source), "--source-commit", frozen[branch]["commit"],
        "--main-head", frozen["main"]["commit"], "--branch", branch,
        "--output", str(case / ("applications-" + branch)), "--prepare-applications",
        "--uv", uv, "--timeout", "600",
    ]
    for name in COMPONENTS:
        command.extend(["--project", name])
    result = json.loads(native._run(command, case, timeout=600).stdout)
    if not isinstance(result, dict) or set(result) != set(COMPONENTS):
        raise RuntimeError("Canonical application producer returned another portfolio")
    return {name: str(Path(result[name]).resolve(strict=True)) for name in COMPONENTS}


def _declarations(snapshots: dict[str, str]) -> dict[str, Any]:
    result = {}
    for name, value in snapshots.items():
        snapshot = Path(value)
        record = native._read(snapshot / "install-source.json")
        project = record["projects"][name]
        result[name] = {
            "snapshot": str(snapshot), "source_commit": record["source_commit"],
            "source_tree": record["source_tree"], "main_seed": record["main_head"],
            "project": project,
            "application": native._read(snapshot / project["application"] / "application.json"),
        }
    return result


def _case(directory: Path) -> tuple[Path, dict[str, Any]]:
    native.require_venue()
    directory = directory.resolve()
    if directory == native.REPO or not directory.is_relative_to(native.REPO):
        raise ValueError("Native source case must be below this checkout")
    case = directory / "source"
    setup = native._read(case / "setup.json")
    if (setup.get("schema") != SCHEMA or setup.get("mode") != "source"
            or setup.get("case_root") != str(case) or case.is_symlink()):
        raise ValueError("Prepared source fixture ownership mismatch")
    return case, setup


@contextmanager
def source_transport(case: Path, *, deadline: float) -> Iterator[str]:
    """Reuse the existing read-only Git HTTP owner, preserving real Git objects."""
    transport = {"repository": str(case / "transport.git")}
    if not Path(transport["repository"]).is_dir():
        native._git(case, case, "clone", "--bare", "--no-hardlinks",
                    str(case / "published"), transport["repository"])
    provider = runpy.run_path(str(Path(__file__).with_name("manager_installed_suite_payloads.py")))

    def run(
        argv: list[Any], environment: dict[str, str], project: Path, diagnostics: Path,
        label: str, *, timeout: float,
    ) -> Any:
        return native._run(argv, case, timeout=timeout, extra_env=environment)

    with provider["serve_git_transport"](
        transport, environment=_env(case), project=case / "project",
        diagnostics=case, deadline=deadline, run=run,
    ) as serving:
        yield serving["url"]


def manager_frontdoors(case: Path, expected_python: Path) -> dict[str, Any]:
    rows = {}
    for extension in ("cmd", "ps1"):
        observed = native._run(
            _command(case, "worktree-manager", "--version", extension=extension), case, timeout=30,
            extra_env={"CE_TERMINAL_CAPABILITY_QUERY": "1"},
        )
        proof = json.loads(observed.stdout)
        actual = Path(proof["runtime"]["command"][0]).resolve(strict=True)
        if actual != expected_python.resolve(strict=True):
            raise RuntimeError(f"Manager {extension} launcher routes to another physical generation")
        rows["worktree-manager." + extension] = {
            "returncode": observed.returncode, "routes_current": True,
            "proof": {"kind": "actual terminal runtime capability query", "record": proof},
        }
    return rows


def _capture(case: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"applications": {}, "resident_observations": {}}
    for name, module in (("worktree-manager", "worktree_manager"), ("agent-worktrees", "agent_worktrees")):
        root = case / "h" / ("." + name)
        python = native._python(root)
        generation = (root / "current-version").read_text(encoding="utf-8").strip()
        slot = root / "versions" / generation
        payload = (
            case / "published" / name if name == "worktree-manager"
            else case / "published" / "plugins" / name
        )
        distribution = native.tomllib.loads((payload / "pyproject.toml").read_text(encoding="utf-8"))["project"]["name"]
        probe = native._run([
            python, "-I", "-B", "-X", "utf8", "-c",
            "import importlib,importlib.metadata,json,sys;"
            "print(json.dumps({'prefix':sys.prefix,'executable':sys.executable,"
            "'module':importlib.import_module(sys.argv[1]).__file__,"
            "'version':importlib.metadata.version(sys.argv[2])}))",
            module, distribution,
        ], case, timeout=30)
        origin = json.loads(probe.stdout)
        if not Path(origin["module"]).resolve().is_relative_to(Path(origin["prefix"]).resolve()):
            raise RuntimeError(f"{name}: imported module is outside its physical environment")
        result["applications"][name] = {
            "generation": generation, "slot": str(slot), "origin": origin,
            "application": native._read(slot / "application.json"),
        }
        try:
            marker, _ = native._record(root, name)
        except FileNotFoundError:
            continue
        with native.closing_process(marker["pid"]) as process:
            created, _exited, code = process.times()
        if code == 259:
            result["resident_observations"][name] = {"pid": marker["pid"], "created_ns": created}
    commands = native._frontdoors(case, {
        "commands": {
            name: [{"command": name, "module": name.replace("-", "_"), "variants": ["cmd", "ps1"]}]
            for name in ("agent-worktrees",)
        },
        "origins": {name: row["origin"] for name, row in result["applications"].items()},
        "versions": {name: row["generation"] for name, row in result["applications"].items()},
    })
    commands["worktree-manager"] = manager_frontdoors(
        case, Path(result["applications"]["worktree-manager"]["origin"]["executable"]),
    )
    for name in COMPONENTS:
        result["applications"][name]["commands"] = commands[name]
    inventory = native._run(_command(case, "agent-worktrees", "update", "--inventory", "--json"), case)
    result["inventory"] = json.loads(inventory.stdout)
    targets = [
        row["id"] for row in result["inventory"]["entries"]
        if row["updatable"] and row["kind"] in {"universal", "machine-gated"}
    ]
    if targets != ["agent-worktrees"]:
        raise RuntimeError(f"Fixture inventory exceeds its selected Worktrees portfolio: {targets}")
    return result


def prepare(directory: Path, main: str, dev: str) -> None:
    native.require_venue()
    directory = directory.resolve()
    if directory == native.REPO or not directory.is_relative_to(native.REPO):
        raise ValueError("Native source fixture storage must be below this checkout")
    case = directory / "source"
    case.mkdir(parents=True, exist_ok=False)
    (case / "project").mkdir()
    env = _env(case)
    setup: dict[str, Any] = {
        "schema": SCHEMA, "mode": "source", "case_root": str(case), "ready": False,
        "declared": {"agent-worktrees": {}}, "before": {},
        "runner_source": native._git(case, native.REPO, "rev-parse", "HEAD"),
        "effects": {"components": list(COMPONENTS), "home": str(case / "h"),
                    "venue": "github-hosted-windows", "actors": "no New/Resume or survival attempt",
                    "transport": "existing context-owned loopback Git HTTP; actual objects only"},
    }
    _write(case / "setup.json", setup)
    try:
        frozen = setup["frozen"] = freeze_source(case, native.REPO, main, dev)
        base = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
        setup["base"] = base
        snapshots = {branch: _applications(case, branch, frozen, base) for branch in ("main", "dev")}
        setup["prepared"] = {branch: _declarations(rows) for branch, rows in snapshots.items()}
        native._git(case, case / "published", "checkout", "-q", "main")
        manifest = native._read(case / "published" / "plugins" / "agent-worktrees" / "plugin.json")
        declared = {"agent-worktrees": {"scope": manifest["runtimeScope"], "version": manifest["version"]}}
        setup["declared"] = declared
        tools = case / "tools"
        tools.mkdir()
        (tools / "delivery.py").write_text(_DELIVERY, encoding="utf-8")
        for name in ("copilot", "gh"):
            (tools / (name + ".cmd")).write_text(
                f'@echo off\n"{base}" -I -X utf8 -B "{tools / "delivery.py"}" {name} %*\n'
                "exit /b %errorlevel%\n", encoding="utf-8",
            )
        _configuration(case, declared, index_host=False)
        native._git(case, case / "project", "init", "-q", "-b", "main", "--template=")
        native._commit(case, case / "project")
        native._git(case, case / "project", "remote", "add", "origin", str(case / "project"))
        native._git(case, case / "project", "fetch", "-q", "origin", "main")
        native._git(case, case / "project", "branch", "--set-upstream-to=origin/main", "main")
        # Freeze producer-owned declarations before the first installed effect.
        _write(case / "setup.json", setup)
        native._run([
            base, "-I", "-B", "-X", "utf8",
            native.REPO / "worktree-manager" / "scripts" / "runtime_install.py",
            "--root", Path(env["HOME"]) / ".worktree-manager",
            "--install-source", snapshots["main"]["worktree-manager"], "--", "--version",
        ], case, timeout=600)
        payload = case / "h" / ".copilot" / "installed-plugins" / "copilot-extensions" / "agent-worktrees"
        native._run([
            shutil.which("pwsh"), "-NoProfile", "-File", payload / "scripts" / "install.ps1",
            "install", "-InstallSource", snapshots["main"]["agent-worktrees"], "-InputTimeout", "600",
        ], case, timeout=600)
        setup["before"] = _capture(case)
        setup["ready"] = True
        _write(case / "setup.json", setup)
    except BaseException as error:
        _write(case / "setup.json", setup)
        _write(case / "preparation-error.json", {"schema": SCHEMA, "errors": [f"{type(error).__name__}: {error}"]})
        raise


def exercise(directory: Path) -> None:
    case, setup = _case(directory)
    if not setup["ready"]:
        raise ValueError("Source fixture was not completely prepared")
    report: dict[str, Any] = {"schema": SCHEMA, "errors": [], "before": setup["before"]}
    durable = [
        case / "h" / ".worktree-manager" / "config.toml",
        case / "h" / ".example" / "config.yaml",
        case / "h" / ".agent-worktrees" / "source-fixture-data",
    ]
    durable[-1].write_text("retained fixture data\n", encoding="utf-8")
    try:
        deadline = time.monotonic() + 840
        with source_transport(case, deadline=deadline) as repository:
            durable[0].write_text(
                "[source]\nrepo=" + json.dumps(repository) + '\nref="dev"\n'
                "[runtime_source]\nchannel=\"dev\"\nallow_dev=true\nrepo="
                + json.dumps(repository) + "\n[maintenance]\nautomatic=false\n",
                encoding="utf-8",
            )
            report["transport"] = {"repository": repository, "kind": "existing read-only Git HTTP owner"}
            report["durable_before"] = native._durable(durable)
            (case / "process-trace-window").write_text("ordinary-source-update", encoding="utf-8")
            command = _command(case, "worktree-manager", "update", "--project", "example")
            result = native._run(command, case, timeout=deadline - time.monotonic(), check=False)
            report["update"] = {"command": command, "returncode": result.returncode,
                                "stdout": result.stdout, "stderr": result.stderr}
            report["after"] = _capture(case)
            report["durable_after"] = native._durable(durable)
        report["frozen_after"] = {
            branch: native._git(case, case / "published", "rev-parse", branch)
            for branch in ("main", "dev")
        }
    except Exception as error:
        report["errors"].append(f"{type(error).__name__}: {error}")
    finally:
        _write(case / "result.json", report)
    if report["errors"]:
        raise AssertionError(json.dumps(report["errors"]))


def settle(directory: Path) -> None:
    case, setup = _case(directory)
    errors = native._cleanup(case)
    remaining = native._tasks(case)
    if remaining:
        errors.append("Owned Scheduled Tasks remain")
    slots = {name: str(case / "h" / ("." + name) / "versions") for name in COMPONENTS}
    processes = json.loads(native._run([
        shutil.which("pwsh"), "-NoProfile", "-NonInteractive", "-Command",
        _runtime_process_inventory_script(slots),
    ], case, timeout=30).stdout)
    if processes:
        errors.append("Fixture-owned runtime processes remain")
    _write(case / "settlement.json", {"schema": SCHEMA, "errors": errors,
                                     "remaining_tasks": remaining, "remaining_processes": processes,
                                     "source": setup.get("frozen")})
    if errors:
        raise AssertionError(json.dumps(errors))
