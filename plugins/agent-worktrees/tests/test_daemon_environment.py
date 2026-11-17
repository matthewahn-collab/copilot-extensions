"""Fresh Windows daemon context, preserving runtime and containment boundaries."""

import os
import sys
import time
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_procutil
from agent_procutil import environment
from agent_worktrees import status_monitor_runtime as runtime


def test_fresh_os_context_preserves_only_runtime_overrides_and_scrubs(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "_core_helper", lambda name, local: local)
    inherited = {
        "PATH": "stale", "AGENT_HOME": str(tmp_path), "COPILOT_EXTENSIONS_CONTEXT": "cell",
        "COPILOT_AGENT_SESSION_ID": "old-session", "GH_TOKEN": "old-secret",
        "AGENT_WORKTREES_AHP_AUTH_TOKEN": "old-secret", "UNDECLARED": "stale",
    }
    calls = []
    monkeypatch.setattr(agent_procutil, "contained_test_mode", lambda: False)
    monkeypatch.setattr(runtime, "os", SimpleNamespace(name="nt", path=os.path))
    monkeypatch.setattr(runtime.status_updater_cli, "_background_environment", lambda: inherited.copy())

    def fresh(*, overrides):
        calls.append(overrides.copy())
        return {"PATH": "fresh", "USERPROFILE": "os-user", "GITHUB_TOKEN": "os-secret", **overrides}

    monkeypatch.setattr(environment, "fresh_user_environment", fresh)
    result = runtime._daemon_environment()
    assert result["PATH"] == "fresh"
    assert result["COPILOT_EXTENSIONS_CONTEXT"] == "cell"
    assert "UNDECLARED" not in result
    assert not any(key in result for key in ("GH_TOKEN", "GITHUB_TOKEN", "COPILOT_AGENT_SESSION_ID",
                                             "AGENT_WORKTREES_AHP_AUTH_TOKEN"))
    assert "PATH" not in calls[0]
    assert inherited["PATH"] == "stale"


def test_contained_daemon_never_replaces_sandbox_roots(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "_core_helper", lambda name, local: local)
    inherited = {"HOME": str(tmp_path), "USERPROFILE": str(tmp_path), "PATH": "fixture"}
    monkeypatch.setattr(agent_procutil, "contained_test_mode", lambda: True)
    monkeypatch.setattr(runtime.status_updater_cli, "_background_environment", lambda: inherited.copy())
    monkeypatch.setattr(environment, "fresh_user_environment",
                        lambda **kw: pytest.fail("must not escape the isolated test environment"))
    assert runtime._daemon_environment() == inherited


@pytest.mark.skipif(os.name != "nt", reason="native Windows OS user environment")
def test_native_fresh_environment_real_leased_daemon_git(tmp_path):
    from agent_procutil.daemon_instances import DaemonInstanceSpec
    from agent_procutil.git import resolve_git

    original = dict(os.environ)
    env = environment.fresh_user_environment()
    selected = resolve_git(env=env)
    env.update({key: value for key, value in original.items()
                if key.startswith("COPILOT_EXTENSIONS_")})
    env["COPILOT_EXTENSIONS_DAEMON_REGISTRY"] = str(tmp_path / "instances")
    result = tmp_path / "result.json"
    code = (
        "import json,subprocess,sys;"
        "r=subprocess.run([sys.argv[2],'--version'],capture_output=True,text=True);"
        "open(sys.argv[1],'w').write(json.dumps({'exit':r.returncode,'version':r.stdout}));"
    )
    child = agent_procutil.spawn_daemon(
        [sys.executable, "-c", code, str(result), selected], env=env,
        cwd=str(tmp_path), log_path=tmp_path / "fixture.log",
        instance=DaemonInstanceSpec("git-environment-fixture", tmp_path, Path(__file__).resolve()),
    )
    try:
        deadline = time.monotonic() + 15
        while not result.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert result.exists()
        observed = json.loads(result.read_text())
        assert observed["exit"] == 0
        assert observed["version"].startswith("git version ")
        child.wait(timeout=10)
        assert dict(os.environ) == original
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)
