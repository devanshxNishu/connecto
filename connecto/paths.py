"""Where Connecto keeps its files.

Everything lives in one folder - the "home" - so backing it up, moving it, or deleting it is one operation:

    config.yaml      your settings (no secrets)
    .env             API keys for the optional AI helper (permissions 0600)
    connecto.sqlite   people, sent invitations, run history
    browser/         the automation browser's profile (holds your LinkedIn session - treat it like a password)
    logs/            run.log
    data/            cached downloads and imported files
    run.lock         locked (the file itself stays) while a run is in progress

Resolution order: ``--home`` flag, the ``CONNECTO_HOME`` environment variable, then ``~/.connecto``.
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "CONNECTO_HOME"


def default_home() -> Path:
    env = os.environ.get(ENV_HOME)
    if env:
        return Path(env).expanduser()
    return Path.home() / ".connecto"


class Home:
    def __init__(self, root: str | os.PathLike | None = None):
        self.root = (Path(root).expanduser() if root else default_home()).resolve()

    # ------------------------------------------------------------------ files
    @property
    def config_path(self) -> Path:
        return self.root / "config.yaml"

    @property
    def env_path(self) -> Path:
        return self.root / ".env"

    @property
    def db_path(self) -> Path:
        return self.root / "connecto.sqlite"

    @property
    def browser_dir(self) -> Path:
        return self.root / "browser"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def log_path(self) -> Path:
        return self.logs_dir / "run.log"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def imports_dir(self) -> Path:
        return self.data_dir / "imports"

    @property
    def lock_path(self) -> Path:
        return self.root / "run.lock"

    # ------------------------------------------------------------------ setup
    def ensure(self) -> "Home":
        """Create the folder layout. The root is made private (0700) because it holds a logged-in browser profile."""
        for d in (self.root, self.browser_dir, self.logs_dir, self.data_dir, self.imports_dir):
            d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:  # Windows / odd filesystems
            pass
        return self

    def exists(self) -> bool:
        return self.config_path.exists()

    def __repr__(self) -> str:  # pragma: no cover
        return f"Home({str(self.root)!r})"
