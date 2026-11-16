"""Every module uses only the fixed file vocabulary (ADR 0008)."""

from pathlib import Path

import app.modules

MODULES_DIR = Path(app.modules.__file__).resolve().parent

ALLOWED_FILES = {
    "__init__.py",
    "router.py",
    "schemas.py",
    "service.py",
    "crud.py",
    "models.py",
    "dependencies.py",
    "errors.py",
    "tasks.py",
    "admin.py",
}
ALLOWED_DIRS = {"templates"}
IGNORED = {"__pycache__"}
# The core's email layout; a module template with this name would shadow it.
CORE_LAYOUT = "base.html"


def _module_dirs() -> list[Path]:
    return sorted(
        p for p in MODULES_DIR.iterdir() if p.is_dir() and p.name not in IGNORED
    )


def test_modules_use_only_the_file_vocabulary():
    module_dirs = _module_dirs()
    assert module_dirs, f"no modules found under {MODULES_DIR}"

    unexpected = [
        f"{module.name}/{entry.name}"
        for module in module_dirs
        for entry in module.iterdir()
        if entry.name not in IGNORED
        and not (entry.is_file() and entry.name in ALLOWED_FILES)
        and not (entry.is_dir() and entry.name in ALLOWED_DIRS)
    ]

    assert unexpected == []


def test_module_templates_are_flat_and_do_not_shadow_the_core_layout():
    problems = [
        f"{module.name}/templates/{entry.name}"
        for module in _module_dirs()
        if (module / "templates").is_dir()
        for entry in (module / "templates").iterdir()
        if entry.is_dir() or entry.name == CORE_LAYOUT
    ]

    assert problems == []
