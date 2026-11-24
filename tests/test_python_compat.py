import ast
from pathlib import Path


def test_python_310_does_not_import_datetime_utc():
    root = Path(__file__).parents[1]
    offenders = []
    for package in (root / "server", root / "agent"):
        for path in package.rglob("*.py"):
            imports = ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
            if any(isinstance(node, ast.ImportFrom) and node.module == "datetime"
                   and any(alias.name == "UTC" for alias in node.names) for node in imports):
                offenders.append(str(path.relative_to(root)))
    assert offenders == []
