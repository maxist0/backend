"""The OpenAPI document describes the `/api/v1` contract and its errors.

`openapi_snapshot.json` next to this file is the committed contract: the
snapshot test compares the generated document with it exactly. After an
intended contract change, regenerate it and review the diff:

    UPDATE_OPENAPI_SNAPSHOT=1 uv run pytest tests/unit/core/test_openapi_contract.py
"""

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.main import create_app
from tests.helpers import SettingsFactory

SNAPSHOT = Path(__file__).with_name("openapi_snapshot.json")

_CORE_PATHS = {"/", "/health", "/health/ready"}
_PROBLEM = {"$ref": "#/components/schemas/ProblemDetail"}
_METHODS = {"get", "post", "put", "patch", "delete"}

# Any: an OpenAPI document is arbitrary JSON.
type OpenApi = dict[str, Any]


@pytest.fixture
def openapi(settings_factory: SettingsFactory) -> OpenApi:
    return create_app(settings_factory()).openapi()


def _module_operations(doc: OpenApi) -> Iterator[tuple[str, str, OpenApi]]:
    for path, item in doc["paths"].items():
        if path in _CORE_PATHS:
            continue
        for method, operation in item.items():
            if method in _METHODS:
                yield path, method, operation


def test_document_matches_the_committed_snapshot(
    settings_factory: SettingsFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every path, schema, status code and error of the contract, exactly.

    The settings that reach the document are fixed here, so neither the
    environment nor a release version changes it.
    """
    settings = settings_factory(
        APP_NAME="api",
        APP_DESCRIPTION="API Description",
        APP_VERSION="0.0.0",
        API_PREFIX="/api/v1",
    )
    # The password flow's token URLs read the process settings.
    monkeypatch.setattr("app.modules.users.dependencies.get_settings", lambda: settings)
    doc = create_app(settings).openapi()

    if os.environ.get("UPDATE_OPENAPI_SNAPSHOT") == "1":
        SNAPSHOT.write_text(
            json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        pytest.skip(f"wrote {SNAPSHOT.name}; review the diff before committing")

    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert doc == expected, (
        "the OpenAPI document differs from the snapshot; if the change is "
        "intended, run with UPDATE_OPENAPI_SNAPSHOT=1 and review the diff"
    )


def test_core_paths_are_unprefixed_and_metrics_is_hidden(openapi: OpenApi) -> None:
    assert set(openapi["paths"]) >= _CORE_PATHS
    assert "/metrics" not in openapi["paths"]
    assert openapi["paths"]["/health/ready"]["get"]["tags"] == ["health"]


def test_every_module_path_is_under_the_api_prefix(openapi: OpenApi) -> None:
    module_paths = set(openapi["paths"]) - _CORE_PATHS

    assert module_paths
    assert all(path.startswith("/api/v1/") for path in module_paths), module_paths


def test_every_module_route_declares_problem_detail_errors(openapi: OpenApi) -> None:
    for path, method, operation in _module_operations(openapi):
        errors = {
            status: response
            for status, response in operation["responses"].items()
            if status[0] in "45"
        }
        assert errors, f"{method.upper()} {path} declares no error responses"
        for status, response in errors.items():
            assert response["content"] == {
                "application/problem+json": {"schema": _PROBLEM}
            }, f"{method.upper()} {path} {status}"
        assert operation.get("summary"), f"{method.upper()} {path} has no summary"


def test_fastapis_default_validation_schema_is_not_used(openapi: OpenApi) -> None:
    """Every route with input declares its 422 as a problem itself."""
    schemas = openapi["components"]["schemas"]

    assert "HTTPValidationError" not in schemas
    assert schemas["ProblemDetail"]["required"] == [
        "title",
        "status",
        "detail",
        "code",
    ]
    assert schemas["FieldError"]["required"] == ["field", "code", "message"]


def test_bodyless_success_responses_have_no_content(openapi: OpenApi) -> None:
    """202 and 204 answers carry no body."""
    for path, method, operation in _module_operations(openapi):
        for status in ("202", "204"):
            if status in operation["responses"]:
                assert "content" not in operation["responses"][status], (
                    f"{method.upper()} {path} {status}"
                )


@pytest.mark.parametrize("prefix", ["/api/v1", "/api/v2"])
def test_swagger_token_urls_follow_the_api_prefix(
    settings_factory: SettingsFactory, monkeypatch: pytest.MonkeyPatch, prefix: str
) -> None:
    settings = settings_factory(API_PREFIX=prefix)
    # The scheme reads the process settings when the document is built.
    monkeypatch.setattr("app.modules.users.dependencies.get_settings", lambda: settings)

    doc = create_app(settings).openapi()

    flow = doc["components"]["securitySchemes"]["OAuth2PasswordBearer"]["flows"]
    assert flow["password"]["tokenUrl"] == f"{prefix}/auth/login"
    assert flow["password"]["refreshUrl"] == f"{prefix}/auth/refresh"
    assert f"{prefix}/auth/login" in doc["paths"]
