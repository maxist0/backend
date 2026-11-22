"""The `Module` contract."""

import dataclasses

import pytest
from fastapi import APIRouter

from app.core import Module


def test_module_defaults() -> None:
    router = APIRouter()

    module = Module(name="items", router=router)

    assert module.router is router
    assert (module.models, module.tasks, module.admin_views) == (None, None, ())


def test_module_is_frozen() -> None:
    module = Module(name="items", router=APIRouter())

    with pytest.raises(dataclasses.FrozenInstanceError):
        module.name = "other"  # type: ignore[misc]
