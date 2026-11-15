"""Explicit native configuration ownership for repository-independent callers."""

from __future__ import annotations

import os
from pathlib import Path

from configuration_layers import ConfigurationError, read_source

FIELD = "default_configuration_root"


def configured_root() -> Path | None:
    """Read an explicit machine-owned source; never infer one from install paths."""
    from .config import load_global_config

    document = load_global_config(strict=True)
    if FIELD not in document:
        return None
    value = document[FIELD]
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError("default configuration root must be an absolute directory")
    root = Path(value)
    if not root.is_absolute() or not root.is_dir():
        raise ConfigurationError(
            "default configuration root must be an existing absolute directory"
        )
    source = read_source(str(root), ".agent-vault.json")
    if source is None:
        raise ConfigurationError("default configuration owner requires .agent-vault.json")
    if set(source["values"]) - {"enabled", "vault", "group"}:
        raise ConfigurationError("default configuration owner cannot replace native custody")
    return root.resolve()


def fallback_root(cwd: str | None = None) -> str | None:
    """Keep delivered or locally declared repository configuration ahead of defaults."""
    from configuration_context import delivered_context

    directory = Path(cwd or os.getcwd()).absolute()
    if delivered_context(repository_root=directory) is not None:
        return None
    for parent in (directory, *directory.parents):
        source = parent / ".agent-vault.json"
        if source.exists() or source.is_symlink():
            return None
    root = configured_root()
    return str(root) if root is not None else None


def owns_root(root: Path) -> bool:
    """A native default source is standalone data, not a Worktrees peer request."""
    from .config import load_global_config

    value = load_global_config(strict=True).get(FIELD)
    if not isinstance(value, str) or not Path(value).is_absolute():
        return False
    try:
        matches = root.resolve() == Path(value).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    if not matches:
        return False
    configured = configured_root()
    return configured is not None and root.resolve() == configured
