"""Catch test definitions Python would silently replace before pytest collection."""

import ast
from pathlib import Path


def test_test_definitions_do_not_shadow_other_tests():
    duplicates = []
    for path in sorted(Path(__file__).parent.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for scope in [tree, *(node for node in ast.walk(tree) if isinstance(node, ast.ClassDef))]:
            seen = {}
            for node in scope.body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.name.startswith("test_"):
                    continue
                if node.name in seen:
                    duplicates.append(f"{path.name}:{seen[node.name]},{node.lineno} {getattr(scope, 'name', 'module')}.{node.name}")
                seen[node.name] = node.lineno
    assert not duplicates, "Test definitions shadowed before collection:\n" + "\n".join(duplicates)
