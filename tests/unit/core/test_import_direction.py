"""Imports point one way: `core <- users <- auth <- app modules` (ADR 0001, 0008).

The allowed module-to-module direction comes from `MODULES` itself: a module
is listed after the modules it depends on, so it may import only modules
listed before it. No second list has to be kept in sync.
"""

import ast
from pathlib import Path

import app.core
import app.modules
from app.modules import MODULES

CORE_DIR = Path(app.core.__file__).resolve().parent
MODULES_DIR = Path(app.modules.__file__).resolve().parent
MODULES_PACKAGE = "app.modules"


def _package_of(path: Path) -> str:
    """Dotted package containing `path`, for resolving relative imports."""
    src_dir = CORE_DIR.parents[1]
    return ".".join(path.parent.relative_to(src_dir).parts)


def _imported_modules(path: Path) -> list[tuple[int, str]]:
    """Every module `path` imports, as absolute dotted names with line numbers."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                package = _package_of(path).split(".")
                anchor = ".".join(package[: len(package) - node.level + 1])
                base = f"{anchor}.{base}" if base else anchor
            # `from app.modules import users` imports `app.modules.users`.
            names = [f"{base}.{alias.name}" for alias in node.names]
            if base != MODULES_PACKAGE:
                names.insert(0, base)
            found.extend((node.lineno, name) for name in names)
    return found


def _module_packages() -> list[str]:
    return sorted(
        p.name
        for p in MODULES_DIR.iterdir()
        if p.is_dir() and (p / "__init__.py").exists()
    )


def test_core_does_not_import_modules():
    files = sorted(CORE_DIR.rglob("*.py"))
    assert files, f"no core files found under {CORE_DIR}"

    violations = [
        f"{path.relative_to(CORE_DIR)}:{line}: {name}"
        for path in files
        for line, name in _imported_modules(path)
        if name == MODULES_PACKAGE or name.startswith(f"{MODULES_PACKAGE}.")
    ]

    assert violations == []


def test_every_module_package_is_registered_under_its_name():
    """The order check below needs a position for every module package."""
    assert sorted(module.name for module in MODULES) == _module_packages()


def test_modules_import_only_modules_listed_before_them():
    position = {module.name: index for index, module in enumerate(MODULES)}

    violations = []
    for name in _module_packages():
        package_dir = MODULES_DIR / name
        for path in sorted(package_dir.rglob("*.py")):
            for line, imported in _imported_modules(path):
                if imported == MODULES_PACKAGE:
                    # The registry imports every module: always a cycle.
                    violations.append(f"{name}/{path.name}:{line}: {imported}")
                    continue
                if not imported.startswith(f"{MODULES_PACKAGE}."):
                    continue
                target = imported.split(".")[2]
                if (
                    target != name
                    and position.get(target, len(MODULES)) >= position[name]
                ):
                    violations.append(f"{name}/{path.name}:{line}: {imported}")

    assert violations == []
