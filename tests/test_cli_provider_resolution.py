"""Provider/model resolution precedence (hermetic -- no network/Ollama).

Covers the project-config-vs-environment precedence fixes: a project-selected
provider must win over an environment default, and a preflight-resolved model must
survive build_agent's second config merge.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from swe_agent.cli import _apply_project_config, resolve_runtime_config
from swe_agent.config import (DEFAULT_OLLAMA_BASE, DEFAULT_OLLAMA_MODEL,
                              DEFAULT_TEMPERATURE, MAX_STEPS)
from swe_agent.project_config import load_project_config, merge_into_args
from swe_agent.providers import get_provider


def _write_config(cwd: Path, body: str) -> None:
    d = cwd / ".agent"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text(body, encoding="utf-8")


def _args(cwd: Path, *, provider: str) -> argparse.Namespace:
    """Mimic parse_args output: provider is the env default, model is the ollama default."""
    return argparse.Namespace(
        cwd=str(cwd), provider=provider, model=DEFAULT_OLLAMA_MODEL,
        base_url=DEFAULT_OLLAMA_BASE, api_key="", temperature=DEFAULT_TEMPERATURE,
        max_steps=MAX_STEPS,
        _model_defaulted=True, _provider_defaulted=True,
        _temp_defaulted=True, _steps_defaulted=True,
    )


def test_project_provider_wins_over_env_default_with_correct_model(tmp_path):
    # Env defaulted the CLI to one cloud provider; project config selects another,
    # without a model. The resolved model must be the *project* provider's default,
    # not the env provider's.
    _write_config(tmp_path, "provider: openai\n")
    args = _args(tmp_path, provider="kimi")  # as if SWE_AGENT_PROVIDER=kimi

    _apply_project_config(args)
    resolve_runtime_config(args)

    assert args.provider == "openai"
    assert args.model == get_provider("openai").default_model
    assert args._provider_defaulted is False  # consumed


def test_preflight_resolved_model_survives_second_merge(tmp_path):
    # Project sets an (unavailable) ollama model; no --model flag.
    _write_config(tmp_path, "model: unpulled-model:1b\n")
    args = _args(tmp_path, provider="ollama")

    cfg = _apply_project_config(args)
    resolve_runtime_config(args)
    assert args.model == "unpulled-model:1b"
    assert args._model_defaulted is False  # consumed

    # Preflight resolves the unavailable model to a usable fallback.
    args.model = "qwen2.5-coder:7b"

    # build_agent merges project config again -- must NOT clobber the fallback.
    merge_into_args(args, load_project_config(tmp_path))
    assert args.model == "qwen2.5-coder:7b"


def test_no_project_config_is_noop(tmp_path):
    args = _args(tmp_path, provider="ollama")
    _apply_project_config(args)
    resolve_runtime_config(args)
    assert args.provider == "ollama"
    assert args.model == DEFAULT_OLLAMA_MODEL
    assert args.base_url == DEFAULT_OLLAMA_BASE
