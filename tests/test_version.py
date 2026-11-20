"""版本号两层制的护栏。

大版本（基线）写在两处，漂移了会在发布时出现「status 说 2.0、exe 报 1.9」这类怪事，
所以钉一条测试。本地构建序号（`-local.N`）住在发布根，不进仓库，源码态读不到。
"""

from __future__ import annotations

import re
from pathlib import Path

import humanity_memory
from humanity_memory import _BASE_VERSION

ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^\s*version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, "pyproject.toml 里找不到 version"
    return match.group(1)


def test_base_version_matches_pyproject() -> None:
    assert _BASE_VERSION == _pyproject_version()


def test_source_tree_reports_baseline() -> None:
    """源码态没有 version.txt；万一被打包产物/手误写进来，这里会立刻炸出来。"""
    assert humanity_memory.__version__ == _BASE_VERSION
