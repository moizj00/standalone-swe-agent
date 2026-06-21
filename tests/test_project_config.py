"""Tests for swe_agent.project_config."""
import tempfile
from pathlib import Path

from swe_agent.project_config import ProjectConfig, load_project_config, merge_into_args


def test_load_missing_config():
    """Returns empty ProjectConfig when no .agent/config.yaml exists."""
    with tempfile.TemporaryDirectory() as td:
        cfg = load_project_config(Path(td))
        assert cfg.model is None
        assert cfg.test_command is None
        assert cfg.path_scope == []


def test_load_simple_config():
    """Parses a simple .agent/config.yaml without PyYAML."""
    with tempfile.TemporaryDirectory() as td:
        agent_dir = Path(td) / ".agent"
        agent_dir.mkdir()
        (agent_dir / "config.yaml").write_text(
            "model: gpt-4\n"
            "temperature: 0.5\n"
            "max_steps: 30\n"
            "test_command: pytest -q\n"
            "network: true\n"
        )
        cfg = load_project_config(Path(td))
        assert cfg.model == "gpt-4"
        assert cfg.temperature == 0.5
        assert cfg.max_steps == 30
        assert cfg.test_command == "pytest -q"
        assert cfg.network is True


def test_load_searches_parent_dirs():
    """Finds .agent/config.yaml in a parent directory."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        agent_dir = root / ".agent"
        agent_dir.mkdir()
        (agent_dir / "config.yaml").write_text("model: llama3\n")
        child = root / "src" / "deep"
        child.mkdir(parents=True)
        cfg = load_project_config(child)
        assert cfg.model == "llama3"


def test_merge_into_args_respects_cli_override():
    """CLI flags win over project config."""
    import argparse
    args = argparse.Namespace(
        model="my-model", provider="openai", temperature=0.9, max_steps=100,
        _model_defaulted=False, _provider_defaulted=False,
        _temp_defaulted=False, _steps_defaulted=False,
    )
    cfg = ProjectConfig(model="other-model", temperature=0.1)
    merge_into_args(args, cfg)
    assert args.model == "my-model"
    assert args.temperature == 0.9


def test_merge_into_args_fills_defaults():
    """Project config fills in values when CLI used defaults."""
    import argparse
    args = argparse.Namespace(
        model="default-model", provider="ollama", temperature=0.7, max_steps=50,
        _model_defaulted=True, _provider_defaulted=True,
        _temp_defaulted=True, _steps_defaulted=True,
    )
    cfg = ProjectConfig(model="proj-model", temperature=0.3, max_steps=25)
    merge_into_args(args, cfg)
    assert args.model == "proj-model"
    assert args.temperature == 0.3
    assert args.max_steps == 25


def test_nested_style_config():
    """Parses lint/format/typecheck from nested style block."""
    with tempfile.TemporaryDirectory() as td:
        agent_dir = Path(td) / ".agent"
        agent_dir.mkdir()
        # When YAML is available, test nested dict parsing
        try:
            import yaml
            (agent_dir / "config.yaml").write_text(
                "style:\n"
                "  linter: ruff check --fix\n"
                "  formatter: black .\n"
                "  type_checker: mypy src/\n"
            )
            cfg = load_project_config(Path(td))
            assert cfg.lint_command == "ruff check --fix"
            assert cfg.format_command == "black ."
            assert cfg.type_check_command == "mypy src/"
        except ImportError:
            # Without PyYAML, nested dicts won't parse via simple parser
            pass
