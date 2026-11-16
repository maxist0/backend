# tests/common/test_dev_compose_parity.py
"""Test containers and the local dev stack run the same images (review 2026-11, N3).

A test database that differs from the one developers run (or production runs)
hides version-specific SQL behaviour, so the pins in ``tests/conftest.py`` and
``compose.dev.yaml`` must not drift apart.
"""

import re

from tests.conftest import BACKEND_DIR, POSTGRES_IMAGE, REDIS_IMAGE


def _compose_image(service: str) -> str:
    text = (BACKEND_DIR / "compose.dev.yaml").read_text(encoding="utf-8")
    block = re.search(rf"^  {service}:\n(.*?)(?=^  \S|\Z)", text, re.M | re.S)
    assert block, f"service {service!r} not found in compose.dev.yaml"
    image = re.search(r"^\s+image:\s*(\S+)", block.group(1), re.M)
    assert image, f"service {service!r} has no image"
    return image.group(1)


def test_postgres_test_image_matches_the_dev_compose_file():
    assert POSTGRES_IMAGE == _compose_image("postgres")


def test_redis_test_image_matches_the_dev_compose_file():
    assert REDIS_IMAGE == _compose_image("redis")


def test_postgres_is_the_production_major():
    assert POSTGRES_IMAGE.startswith("postgres:18")
