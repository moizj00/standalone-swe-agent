"""Hermetic tests for the subprocess sandbox test-runner (no network, no Docker)."""
from __future__ import annotations

from pathlib import Path

from swe_agent.sandbox import TestResult, detect_test_command, run_tests

PYTEST = ["python", "-m", "pytest", "-q"]


def test_run_tests_passes_for_green_suite(tmp_path: Path):
    (tmp_path / "test_green.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    r = run_tests(tmp_path, PYTEST)
    assert isinstance(r, TestResult)
    assert r.passed and r.exit_code == 0 and not r.skipped


def test_run_tests_fails_for_red_suite(tmp_path: Path):
    (tmp_path / "test_red.py").write_text("def test_bad():\n    assert False\n", encoding="utf-8")
    r = run_tests(tmp_path, PYTEST)
    assert not r.passed and r.exit_code != 0


def test_run_tests_times_out(tmp_path: Path):
    r = run_tests(tmp_path, ["python", "-c", "import time; time.sleep(5)"], timeout=1)
    assert not r.passed and r.exit_code == 124 and "timed out" in r.output.lower()


def test_detect_pytest_from_tests_dir(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    assert detect_test_command(tmp_path) == ["python", "-m", "pytest", "-q"]


def test_detect_npm_from_package_json(tmp_path: Path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    assert detect_test_command(tmp_path) == ["npm", "test", "--silent"]


def test_detect_none_when_no_signals(tmp_path: Path):
    assert detect_test_command(tmp_path) is None


def test_run_tests_skipped_when_no_command(tmp_path: Path):
    r = run_tests(tmp_path)
    assert r.skipped and not r.passed and r.command == []
