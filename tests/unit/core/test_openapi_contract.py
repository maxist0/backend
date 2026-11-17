"""The OpenAPI document describes the `/api/v1` contract and its errors."""

import pytest

import app.main as main

_CORE_PATHS = {"/", "/health", "/health/ready"}
_PROBLEM = {"$ref": "#/components/schemas/ProblemDetail"}
_METHODS = {"get", "post", "put", "patch", "delete"}


@pytest.fixture
def openapi(settings_factory) -> dict:
    return main.create_app(settings_factory()).openapi()


def _module_operations(doc: dict):
    for path, item in doc["paths"].items():
        if path in _CORE_PATHS:
            continue
        for method, operation in item.items():
            if method in _METHODS:
                yield path, method, operation


def test_core_paths_are_unprefixed_and_metrics_is_hidden(openapi):
    assert _CORE_PATHS <= set(openapi["paths"])
    assert "/metrics" not in openapi["paths"]
    assert openapi["paths"]["/health/ready"]["get"]["tags"] == ["health"]


def test_every_module_path_is_under_the_api_prefix(openapi):
    module_paths = set(openapi["paths"]) - _CORE_PATHS

    assert module_paths
    assert all(path.startswith("/api/v1/") for path in module_paths), module_paths


def test_every_module_route_declares_problem_detail_errors(openapi):
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


def test_fastapis_default_validation_schema_is_not_used(openapi):
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


def test_bodyless_success_responses_have_no_content(openapi):
    """202 and 204 answers carry no body."""
    for path, method, operation in _module_operations(openapi):
        for status in ("202", "204"):
            if status in operation["responses"]:
                assert "content" not in operation["responses"][status], (
                    f"{method.upper()} {path} {status}"
                )


@pytest.mark.parametrize("prefix", ["/api/v1", "/api/v2"])
def test_swagger_token_urls_follow_the_api_prefix(
    settings_factory, monkeypatch, prefix
):
    settings = settings_factory(API_PREFIX=prefix)
    # The scheme reads the process settings when the document is built.
    monkeypatch.setattr("app.modules.users.dependencies.get_settings", lambda: settings)

    doc = main.create_app(settings).openapi()

    flow = doc["components"]["securitySchemes"]["OAuth2PasswordBearer"]["flows"]
    assert flow["password"]["tokenUrl"] == f"{prefix}/auth/login"
    assert flow["password"]["refreshUrl"] == f"{prefix}/auth/refresh"
    assert f"{prefix}/auth/login" in doc["paths"]
