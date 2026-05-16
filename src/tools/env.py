from __future__ import annotations

import os
from pathlib import Path


def _load_env_file(path: str | Path, *, override: bool = False) -> None:
    env_path = Path(path)
    if not env_path.exists() or not env_path.is_file():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if not key:
            continue

        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]

        if override:
            os.environ[key] = value
        else:
            os.environ.setdefault(key, value)


def load_tools_env(*, override: bool = False) -> None:
    """Load shared project/tool environment variables.

    Search order:
    1. explicit TOOLS_ENV path
    2. ./tools/.env.tools
    3. ./.env.tools
    4. ./.env
    5. project-root/tools/.env.tools
    6. project-root/.env.tools
    7. project-root/.env

    Existing process environment variables win by default.
    Pass override=True only when you intentionally want the file to replace exports.
    """
    explicit = os.getenv("TOOLS_ENV")
    if explicit:
        _load_env_file(explicit, override=override)
        return

    cwd = Path.cwd()
    here = Path(__file__).resolve()
    # tools/env.py -> tools -> src/project root depending on layout
    tools_root = here.parent
    project_root = tools_root.parent

    candidates = [
        tools_root / ".env.tools",
        cwd / "tools" / ".env.tools",
        cwd / ".env.tools",
        cwd / ".env",
        project_root / "tools" / ".env.tools",
        project_root / ".env.tools",
        project_root / ".env",
    ]

    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in seen:
            continue
        seen.add(candidate)
        _load_env_file(candidate, override=override)


def env_str(name: str, default: str | None = None, *, required: bool = False) -> str | None:
    value = os.getenv(name, default)
    if required and (value is None or not str(value).strip()):
        raise RuntimeError(f"Missing required environment variable: {name}")
    if value is None:
        return None
    value = str(value).strip()
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value or None


def env_int(name: str, default: int | None = None, *, required: bool = False) -> int | None:
    raw = env_str(name, None if default is None else str(default), required=required)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be int, got: {raw!r}") from exc


def require_str(name: str) -> str:
    value = env_str(name, required=True)
    assert value is not None
    return value


def require_int(name: str) -> int:
    value = env_int(name, required=True)
    assert value is not None
    return value
