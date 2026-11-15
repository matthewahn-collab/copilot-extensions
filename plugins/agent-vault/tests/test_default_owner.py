"""Native machine defaults do not require a repository-aware peer."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from configuration_layers import ConfigurationError

from agent_vault import config, configuration, default_owner
from agent_vault.invocation import use_configuration_root

Owners = tuple[Path, Path, Path, Path]


@pytest.fixture
def owners(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Owners:
    home, base, project = (tmp_path / name for name in ("neutral", "default", "project"))
    for root in (home, base, project):
        root.mkdir()
    native = tmp_path / "native.json"
    native.write_text(
        json.dumps(
            {
                "enabled": True,
                "default_configuration_root": str(base),
                "vaults": {
                    "first": {"kpdb": str(tmp_path / "first.kdbx")},
                    "second": {"kpdb": str(tmp_path / "second.kdbx")},
                },
            }
        )
    )
    (base / ".agent-vault.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "vault": "first",
                "group": "Example",
            }
        )
    )
    (project / ".agent-vault.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "vault": "second",
            }
        )
    )
    monkeypatch.chdir(home)
    monkeypatch.setenv("AGENT_VAULT_CONFIG", str(native))
    for key in (
        "AGENT_VAULT",
        "KPDB",
        "VAULT_GROUP",
        "AGENT_VAULT_PORT",
        "AGENT_VAULT_PINNED_CONTEXT",
        "AGENT_CONFIGURATION_CONTEXT",
        "COPILOT_EXTENSIONS_CONTEXT",
        "AGENT_RT_ROOT",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(config, "_ext_config", lambda _cwd: {})
    return home, base, project, native


def test_neutral_directory_uses_default_without_a_peer_or_git(
    owners: Owners, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, base, _, _ = owners
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(
        configuration,
        "_legacy_report",
        lambda *_args: pytest.fail("native default attempted a peer subprocess"),
    )
    assert configuration.runtime_enabled() is True
    assert configuration.runtime_enabled(str(home)) is True
    assert configuration.service_configuration_root(str(home)) == str(base)
    result = config.resolve_context(strict=True)
    assert result.vault_name == "first"
    assert result.group == "Example"
    assert config.configuration_root() == str(base)
    assert Path.cwd() == home
    assert result.kpdb is not None
    assert not Path(result.kpdb).exists()


def test_real_cli_uses_native_default_with_no_git_or_worktrees_path(owners: Owners) -> None:
    home, base, _, native = owners
    environment = {
        key: os.environ[key]
        for key in ("SystemRoot", "WINDIR", "COMSPEC", "OS", "PATHEXT")
        if key in os.environ
    }
    environment.update(
        {
            "PATH": "",
            "HOME": str(home),
            "USERPROFILE": str(home),
            "APPDATA": str(home),
            "LOCALAPPDATA": str(home),
            "XDG_CONFIG_HOME": str(home),
            "XDG_CACHE_HOME": str(home),
            "AGENT_VAULT_CONFIG": str(native),
            "AGENT_VAULT_HOME": str(home / "unused-runtime"),
            "AGENT_VAULT_NO_SELFPROVISION": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-m", "agent_vault", "which", "--json"],
        cwd=home,
        env=environment,
        capture_output=True,
        check=False,
        timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    selected = json.loads(result.stdout)
    assert selected["vault_name"] == "first"
    assert selected["group"] == "Example"
    assert not (home / "unused-runtime").exists()
    assert not (base / ".agent-worktrees").exists()


def test_cold_activation_reader_uses_default_without_construction(owners: Owners) -> None:
    home, _, _, native = owners
    script = Path(__file__).parents[1] / "scripts" / "runtime-activation.py"
    environment = {
        key: os.environ[key]
        for key in ("SystemRoot", "WINDIR", "COMSPEC", "OS", "PATHEXT")
        if key in os.environ
    }
    environment.update(
        {
            "PATH": "",
            "HOME": str(home),
            "USERPROFILE": str(home),
            "AGENT_VAULT_CONFIG": str(native),
            "AGENT_VAULT_HOME": str(home / "unused-runtime"),
        }
    )
    result = subprocess.run(  # noqa: S603 -- current interpreter and source-owned reader only.
        [sys.executable, "-I", "-B", str(script), "--cwd", str(home)],
        cwd=home,
        env=environment,
        capture_output=True,
        check=False,
        timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert json.loads(result.stdout)["state"] == "configured"
    assert not (home / "unused-runtime").exists()


def test_explicit_and_repository_sources_precede_default(
    owners: Owners, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, base, project, _ = owners
    monkeypatch.setattr(configuration, "_legacy_report", lambda *_args: None)
    with use_configuration_root(str(project)):
        assert config.configuration_root() == str(project)
        assert config.resolve_context(strict=True).vault_name == "second"
        assert Path.cwd() == home
    monkeypatch.chdir(project)
    assert config.configuration_root() == str(project)
    assert config.resolve_context(strict=True).vault_name == "second"
    monkeypatch.chdir(home)
    assert config.configuration_root() == str(base)


def test_retained_resident_source_is_not_replaced_by_default(
    owners: Owners, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_vault import configuration_scope

    home, _, project, _ = owners
    monkeypatch.setattr(configuration_scope, "_SERVICE_CONFIGURATION_ROOT", str(project))
    assert config.configuration_root() == str(project)
    assert config.configuration_root(str(home)) == str(home)


@pytest.mark.parametrize("selection", ["explicit", "caller", "resident"])
@pytest.mark.parametrize("damage", ["custody", "missing", "relative", "nonstring", "nul"])
def test_unrelated_invalid_default_does_not_veto_selected_owner(
    owners: Owners, monkeypatch: pytest.MonkeyPatch, selection: str, damage: str
) -> None:
    from agent_vault import configuration_scope

    _, base, project, native = owners
    if damage == "custody":
        (base / ".agent-vault.json").write_text('{"vaults":{}}')
    elif damage == "missing":
        (base / ".agent-vault.json").rename(base / "retained-config.json")
    else:
        document = json.loads(native.read_text())
        document["default_configuration_root"] = {
            "relative": ".", "nonstring": False, "nul": str(base / "invalid\0owner"),
        }[damage]
        native.write_text(json.dumps(document))
    monkeypatch.setattr(configuration, "_legacy_report", lambda *_args: None)
    if selection == "explicit":
        with use_configuration_root(str(project)):
            assert config.resolve_context(strict=True).vault_name == "second"
    elif selection == "caller":
        monkeypatch.chdir(project)
        assert config.resolve_context(strict=True).vault_name == "second"
    else:
        monkeypatch.setattr(configuration_scope, "_SERVICE_CONFIGURATION_ROOT", str(project))
        assert config.resolve_context(strict=True).vault_name == "second"


@pytest.mark.parametrize("value", [None, False, "", ".", "/missing-default-owner"])
def test_invalid_default_refuses_without_trying_a_peer(
    owners: Owners, monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    _, _, _, native = owners
    document = json.loads(native.read_text())
    document["default_configuration_root"] = value
    native.write_text(json.dumps(document))
    monkeypatch.setattr(
        configuration,
        "_legacy_report",
        lambda *_args: pytest.fail("invalid default attempted another owner"),
    )
    with pytest.raises(ConfigurationError, match="default configuration"):
        config.resolve_context(strict=True)


def test_default_source_cannot_replace_registry_or_cache_custody(owners: Owners) -> None:
    _, base, _, _ = owners
    (base / ".agent-vault.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "vaults": {"untrusted": {"kpdb": "other.kdbx"}},
            }
        )
    )
    with pytest.raises(ConfigurationError, match="native custody"):
        config.resolve_context(strict=True)


def test_missing_default_source_is_not_silently_ignored(owners: Owners) -> None:
    _, base, _, _ = owners
    (base / ".agent-vault.json").rename(base / "retained-config.json")
    with pytest.raises(ConfigurationError, match=r"requires \.agent-vault\.json"):
        config.resolve_context(strict=True)


def test_absent_default_preserves_existing_optional_owner_behavior(
    owners: Owners, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _, _, native = owners
    document = json.loads(native.read_text())
    del document["default_configuration_root"]
    native.write_text(json.dumps(document))
    observed: list[Path] = []
    monkeypatch.setattr(configuration, "_legacy_report", lambda root: observed.append(root))
    assert default_owner.fallback_root() is None
    assert configuration.owner_report(home) is None
    assert observed == [home]


def test_invalid_delivered_context_does_not_fall_back(
    owners: Owners, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_CONFIGURATION_CONTEXT", "{}")
    with pytest.raises(ConfigurationError):
        config.resolve_context(strict=True)
