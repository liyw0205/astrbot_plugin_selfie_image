"""Detect accidental no-op function bodies in production Python modules."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class NoopFinding:
    path: str
    line: int
    function: str


ALLOWED_NOOP_FUNCTIONS = {
    ("scripts/video_prompt_frames.py", "debug"),
}


def _function_body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.stmt]:
    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return body


def _is_noop_statement(node: ast.stmt) -> bool:
    if isinstance(node, ast.Pass):
        return True
    if isinstance(node, ast.Return):
        return node.value is None or (
            isinstance(node.value, ast.Constant) and node.value.value is None
        )
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and node.value.value is Ellipsis
    )


def _python_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*.py")):
        if any(part in {".venv", "__pycache__", "tests"} for part in path.parts):
            continue
        if path.name == "index_fallback.py":
            continue
        yield path


def find_unapproved_noop_functions(root: Path) -> list[NoopFinding]:
    root = Path(root).resolve()
    findings: list[NoopFinding] = []
    for path in _python_files(root):
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = _function_body(node)
            if body and not all(_is_noop_statement(statement) for statement in body):
                continue
            if (relative, node.name) in ALLOWED_NOOP_FUNCTIONS:
                continue
            findings.append(NoopFinding(relative, node.lineno, node.name))
    return findings