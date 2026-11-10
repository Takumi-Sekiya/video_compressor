"""モジュール間の不変条件を ast で検査する（design.md「守るべき不変条件」）。

1. policy.py はプロジェクト内の他モジュールを import しない
2. policy.py は subprocess / shutil / os を import しない。Path の問い合わせメソッドも呼ばない
3. ffmpeg_io.py / workspace.py / runlog.py は互いを import しない
4. compress.py は判定を書かない（素材の性質を条件式で見ない。ヒューリスティック）
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest

from policy import VideoInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
PROJECT_MODULES = frozenset({"policy", "ffmpeg_io", "workspace", "runlog", "compress"})
IO_MODULES = frozenset({"subprocess", "shutil", "os"})
ADAPTER_MODULES = ("ffmpeg_io", "workspace", "runlog")

# Path / ファイルへの問い合わせ・変更。policy.py は Path を値として持つだけなので呼ばない
FILESYSTEM_METHODS = frozenset({
    "exists", "stat", "lstat", "is_file", "is_dir", "is_symlink", "iterdir", "glob", "rglob",
    "open", "read_text", "read_bytes", "write_text", "write_bytes", "mkdir", "rmdir",
    "unlink", "rename", "touch", "resolve", "chmod", "samefile", "readlink", "absolute",
})
DYNAMIC_IMPORT_CALLS = frozenset({"__import__", "import_module"})

# compress.py の条件式に現れてはいけない「素材の性質」。size_bytes は空き容量の確認に使うので除く
MATERIAL_FIELDS = frozenset(f.name for f in dataclasses.fields(VideoInfo)) - {"path", "size_bytes"}


def read_module(name: str) -> str:
    return (REPO_ROOT / f"{name}.py").read_text(encoding="utf-8")


def imported_modules(source: str) -> set[str]:
    """import しているトップレベルのモジュール名。相対 import は "." として返す。"""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                found.add(".")
            elif node.module is not None:
                found.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            if name in DYNAMIC_IMPORT_CALLS and node.args and isinstance(node.args[0], ast.Constant):
                found.add(str(node.args[0].value).split(".")[0])
    return found


def filesystem_calls(source: str) -> list[str]:
    """Path / ファイルの問い合わせ・変更にあたる呼び出し。"""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in FILESYSTEM_METHODS:
            found.append(f"{func.attr}() at line {node.lineno}")
        elif isinstance(func, ast.Name) and func.id == "open":
            found.append(f"open() at line {node.lineno}")
    return found


def material_conditions(source: str) -> list[str]:
    """条件式（if / while / 条件演算子 / 内包表記の if / assert）の中で参照している素材の性質。"""
    tests: list[ast.expr] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.If, ast.While, ast.IfExp, ast.Assert)):
            tests.append(node.test)
        elif isinstance(node, ast.comprehension):
            tests.extend(node.ifs)
    found: list[str] = []
    for test in tests:
        for node in ast.walk(test):
            if isinstance(node, ast.Attribute) and node.attr in MATERIAL_FIELDS:
                found.append(f".{node.attr} at line {node.lineno}")
    return found


# ---------------------------------------------------------------------------
# 不変条件
# ---------------------------------------------------------------------------


def test_policy_imports_no_project_module() -> None:
    imported = imported_modules(read_module("policy"))
    assert imported & (PROJECT_MODULES | {"."}) == set()


def test_policy_imports_no_io_module() -> None:
    assert imported_modules(read_module("policy")) & IO_MODULES == set()


def test_policy_calls_no_filesystem_method() -> None:
    assert filesystem_calls(read_module("policy")) == []


@pytest.mark.parametrize("module", ADAPTER_MODULES)
def test_adapters_do_not_import_each_other(module: str) -> None:
    others = set(ADAPTER_MODULES) - {module}
    assert imported_modules(read_module(module)) & (others | {"compress", "."}) == set()


def test_compress_does_not_judge_material() -> None:
    assert material_conditions(read_module("compress")) == []


# ---------------------------------------------------------------------------
# 検査器自体が違反を見逃さないこと
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "import os",
        "import os.path",
        "from os import path",
        "from shutil import copy",
        "import subprocess as sp",
        "__import__('subprocess')",
        "importlib.import_module('os')",
        "def f():\n    import os",
    ],
)
def test_detects_io_import(source: str) -> None:
    assert imported_modules(source) & IO_MODULES


@pytest.mark.parametrize("source", ["import workspace", "from runlog import append_result", "from . import x"])
def test_detects_project_import(source: str) -> None:
    assert imported_modules(source) & (PROJECT_MODULES | {"."})


@pytest.mark.parametrize("source", ["Path('a').exists()", "p.stat().st_size", "open('x')", "p.read_text()"])
def test_detects_filesystem_call(source: str) -> None:
    assert filesystem_calls(source)


@pytest.mark.parametrize(
    "source",
    [
        "if info.video_codec == 'hevc':\n    pass",
        "x = 1 if info.color_transfer else 2",
        "while info.fps > 0:\n    break",
        "[s for s in xs if s.rotation == 90]",
    ],
)
def test_detects_material_condition(source: str) -> None:
    assert material_conditions(source)
