"""Reading and writing ~/.ctuirc."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

RC_VERSION = 1

DEFAULT_TASKS_DIRNAME = "ctui-tasks"
DEFAULT_WIKI_DIRNAME = "ctui-wiki"


def rc_path() -> Path:
    """Location of the ctui config file.

    Overridable with CTUI_RC so tests (and odd setups) do not have to touch $HOME.
    """
    override = os.environ.get("CTUI_RC")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".ctuirc"


class ConfigError(Exception):
    pass


@dataclass
class Config:
    tasks_repo: Path
    wiki_repo: Path | None = None
    tasks_remote: str | None = None
    wiki_remote: str | None = None
    version: int = RC_VERSION

    def to_json(self) -> str:
        data = asdict(self)
        data["tasks_repo"] = str(self.tasks_repo)
        data["wiki_repo"] = str(self.wiki_repo) if self.wiki_repo else None
        return json.dumps(data, indent=2, sort_keys=True) + "\n"

    def save(self, path: Path | None = None) -> Path:
        target = path or rc_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.to_json())
        return target


def load(path: Path | None = None) -> Config:
    """Load the config, raising ConfigError with actionable text if it is unusable."""
    target = path or rc_path()
    if not target.exists():
        raise ConfigError(f"No ctui config at {target}. Run `ctui --setup` first.")
    try:
        raw = json.loads(target.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{target} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{target} should contain a JSON object.")

    tasks_repo = raw.get("tasks_repo")
    if not tasks_repo:
        raise ConfigError(f"{target} has no `tasks_repo`. Re-run `ctui --setup`.")

    wiki_repo = raw.get("wiki_repo")
    return Config(
        tasks_repo=_absolute(tasks_repo, "tasks_repo", target),
        wiki_repo=_absolute(wiki_repo, "wiki_repo", target) if wiki_repo else None,
        tasks_remote=raw.get("tasks_remote") or None,
        wiki_remote=raw.get("wiki_remote") or None,
        version=int(raw.get("version", RC_VERSION)),
    )


def _absolute(value: str, field: str, rc: Path) -> Path:
    """Expand `~` and insist the result is absolute.

    Repo paths end up in task directory paths, which ctui hands to claude via
    --add-dir and the appended system prompt *before* chdir'ing into the task
    root. A relative path would silently be read against the wrong directory
    there, so a relative value is rejected rather than resolved against the
    current directory (which would also make `--init` create tasks in a
    different place depending on where it was run).
    """
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ConfigError(
            f"`{field}` in {rc} must be an absolute path, got {value!r}. "
            "Edit it to a full path (or re-run `ctui --setup`)."
        )
    return path


def load_or_none(path: Path | None = None) -> Config | None:
    try:
        return load(path)
    except ConfigError:
        return None
