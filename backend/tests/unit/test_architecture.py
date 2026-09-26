"""Architecture rules as tests (docs/design/15 §26.4, non-negotiables #2 and #14)."""

import ast
import re
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[2] / "app"
FORBIDDEN_IN_INNER_LAYERS = ("fastapi", "starlette", "sqlalchemy")


def python_files(*parts: str) -> list[Path]:
    return sorted(path for path in APP_DIR.glob("/".join(parts)) if path.is_file())


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


INNER_LAYER_FILES = python_files("modules", "*", "domain", "**", "*.py") + python_files(
    "modules", "*", "application", "**", "*.py"
)


@pytest.mark.parametrize("path", INNER_LAYER_FILES, ids=lambda p: str(p.relative_to(APP_DIR)))
def test_domain_and_application_layers_import_no_framework(path: Path) -> None:
    offending = {
        module
        for module in imported_modules(path)
        if module.split(".")[0] in FORBIDDEN_IN_INNER_LAYERS
    }
    assert not offending, f"{path.relative_to(APP_DIR)} imports {sorted(offending)}"


def test_only_the_unit_of_work_commits_or_rolls_back() -> None:
    pattern = re.compile(r"\.(commit|rollback)\(")
    allowed = APP_DIR / "platform" / "db.py"
    offenders = [
        f"{path.relative_to(APP_DIR)}:{number}"
        for path in APP_DIR.rglob("*.py")
        if path != allowed
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if pattern.search(line)
    ]
    assert not offenders, f"commit()/rollback() outside platform/db.py: {offenders}"
