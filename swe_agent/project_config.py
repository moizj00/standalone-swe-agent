"""Project configuration: load .agent/config.yaml from the workspace.

Provides per-project overrides for model, tools, policies, test/lint commands,
and path scoping. CLI flags take precedence over project config, which takes
precedence over environment defaults.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

try:
    import yaml  # type: ignore[import-untyped]
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


CONFIG_FILENAME = "config.yaml"
CONFIG_DIR = ".agent"


@dataclass
class ProjectConfig:
    """Settings loaded from .agent/config.yaml in the workspace root."""

    # LLM settings
    model: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    provider: Optional[str] = None

    # Tool control
    tools_enabled: Optional[List[str]] = None   # allowlist; None = all
    network: bool = False                        # allow web_fetch/web_search

    # Commands
    test_command: Optional[str] = None
    lint_command: Optional[str] = None
    type_check_command: Optional[str] = None
    format_command: Optional[str] = None

    # Policies
    approval: Optional[str] = None              # read-only | default | auto | yolo
    path_scope: List[str] = field(default_factory=list)  # restrict mutations

    # Agent loop
    max_steps: Optional[int] = None

    # Source path (where the config was found)
    _source: Optional[Path] = field(default=None, repr=False)


def load_project_config(cwd: Path) -> ProjectConfig:
    """Search from cwd upward for .agent/config.yaml; return parsed config.

    Falls back gracefully: returns an empty ProjectConfig if the file is missing,
    YAML is unavailable, or parsing fails.
    """
    here = cwd.resolve()
    for d in [here, *here.parents]:
        candidate = d / CONFIG_DIR / CONFIG_FILENAME
        if candidate.is_file():
            return _parse_config(candidate)
    return ProjectConfig()


def _parse_config(path: Path) -> ProjectConfig:
    """Parse a single .agent/config.yaml file into a ProjectConfig."""
    if not _HAS_YAML:
        # Attempt a minimal parse without PyYAML for simple key: value files
        return _parse_simple(path)
    try:
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text) or {}
    except Exception:
        return ProjectConfig(_source=path)
    return _from_dict(data, path)


def _parse_simple(path: Path) -> ProjectConfig:
    """Best-effort parser when PyYAML is not installed (handles flat key: value)."""
    data: dict = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            val = val.strip().strip("'\"")
            if val.lower() in ("true", "false"):
                data[key.strip()] = val.lower() == "true"
            elif val.isdigit():
                data[key.strip()] = int(val)
            else:
                try:
                    data[key.strip()] = float(val)
                except ValueError:
                    data[key.strip()] = val
    except Exception:
        pass
    return _from_dict(data, path)


def _from_dict(data: dict, path: Path) -> ProjectConfig:
    """Map a dict (from YAML or simple parse) into a ProjectConfig dataclass."""
    cfg = ProjectConfig(_source=path)
    cfg.model = data.get("model")
    cfg.provider = data.get("provider")

    temp = data.get("temperature")
    if temp is not None:
        try:
            cfg.temperature = float(temp)
        except (TypeError, ValueError):
            pass

    max_tok = data.get("max_tokens")
    if max_tok is not None:
        try:
            cfg.max_tokens = int(max_tok)
        except (TypeError, ValueError):
            pass

    tools = data.get("tools")
    if isinstance(tools, list):
        cfg.tools_enabled = [str(t) for t in tools]

    cfg.network = bool(data.get("network", False))

    # Commands (nested under "test", "style", or flat)
    test_cfg = data.get("test", {})
    if isinstance(test_cfg, dict):
        cfg.test_command = test_cfg.get("command")
    elif isinstance(test_cfg, str):
        cfg.test_command = test_cfg
    if data.get("test_command"):
        cfg.test_command = str(data["test_command"])

    style_cfg = data.get("style", {})
    if isinstance(style_cfg, dict):
        cfg.lint_command = style_cfg.get("linter") or style_cfg.get("lint")
        cfg.format_command = style_cfg.get("formatter") or style_cfg.get("format")
        cfg.type_check_command = style_cfg.get("type_checker") or style_cfg.get("typecheck")
    if data.get("lint_command"):
        cfg.lint_command = str(data["lint_command"])
    if data.get("type_check_command"):
        cfg.type_check_command = str(data["type_check_command"])
    if data.get("format_command"):
        cfg.format_command = str(data["format_command"])

    # Policies
    cfg.approval = data.get("approval")
    scope = data.get("path_scope", [])
    if isinstance(scope, list):
        cfg.path_scope = [str(s) for s in scope]

    max_s = data.get("max_steps")
    if max_s is not None:
        try:
            cfg.max_steps = int(max_s)
        except (TypeError, ValueError):
            pass

    return cfg


def merge_into_args(args, config: ProjectConfig) -> None:
    """Apply project config as defaults where CLI did not override.

    CLI flags always win. Project config fills in gaps that the user didn't
    explicitly set on the command line.
    """
    if config.model and getattr(args, "_model_defaulted", True):
        args.model = config.model
    if config.provider and getattr(args, "_provider_defaulted", True):
        args.provider = config.provider
    if config.temperature is not None and getattr(args, "_temp_defaulted", True):
        args.temperature = config.temperature
    if config.max_steps is not None and getattr(args, "_steps_defaulted", True):
        args.max_steps = config.max_steps
