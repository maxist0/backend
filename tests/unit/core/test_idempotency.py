"""The idempotency guard's pure parts and the conventions for marked routes.

The database flow is tested on the containers
(`tests/integration/core/test_idempotency.py`); here: the header parser, the
fingerprint, the response recorder, what may be stored, the OpenAPI step,
and the rules every module's marked routes follow.
"""

import hashlib
import hmac
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import params
from fastapi.dependencies.models import Dependant
from fastapi.routing import RouteContext, iter_route_contexts

from app.core import idempotency
from app.core.idempotency import (
    IdempotencyKeyInvalidError,
    marks_idempotent,
    parse_key,
    request_fingerprint,
)
from app.main import create_app
from tests.helpers import SettingsFactory

UUID_KEY = "019abcde-f012-7345-8678-9abcdef01234"
SECRET = "s" * 40


class TestParseKey:
    def test_no_header_is_no_key(self) -> None:
        assert parse_key([]) is None

    @pytest.mark.parametrize(
        ("value", "key"),
        [
            (UUID_KEY, UUID_KEY),
            (f'"{UUID_KEY}"', UUID_KEY),
            ("a" * 16, "a" * 16),
            ("b" * 255, "b" * 255),
            ("AZaz09+/=_.:~-AZaz09", "AZaz09+/=_.:~-AZaz09"),
        ],
    )
    def test_bare_and_quoted_keys(self, value: str, key: str) -> None:
        assert parse_key([value]) == key

    @pytest.mark.parametrize(
        "value",
        [
            "",
            '""',
            "a" * 15,
            "a" * 256,
            f'"{UUID_KEY}',
            f'{UUID_KEY}"',
            f"'{UUID_KEY}'",
            f'"{UUID_KEY}", "{UUID_KEY}"',
            f"{UUID_KEY} ",
            "with space inside the key",
            'with\\"escape-0000000',
            "tab\tinside-0000000000",
        ],
    )
    def test_malformed_values_are_refused(self, value: str) -> None:
        with pytest.raises(IdempotencyKeyInvalidError):
            parse_key([value])

    def test_two_header_lines_are_refused(self) -> None:
        with pytest.raises(IdempotencyKeyInvalidError):
            parse_key([UUID_KEY, UUID_KEY])


class TestFingerprint:
    def _print(self, **overrides: Any) -> bytes:  # noqa: ANN401 - mixed str/bytes parts
        parts: dict[str, Any] = {
            "secret": SECRET,
            "method": "POST",
            "path": "/api/v1/notes",
            "query": b"",
            "body": b'{"a":1}',
            **overrides,
        }
        return request_fingerprint(**parts)

    def test_it_is_a_full_sha256_hmac_and_deterministic(self) -> None:
        assert len(self._print()) == 32
        assert self._print() == self._print()

    @pytest.mark.parametrize(
        "change",
        [
            {"secret": "t" * 40},
            {"method": "PUT"},
            {"path": "/api/v1/notes/1"},
            {"query": b"x=1"},
            {"body": b'{"a":2}'},
        ],
    )
    def test_every_part_changes_it(self, change: dict[str, Any]) -> None:
        assert self._print(**change) != self._print()

    def test_parts_are_length_prefixed(self) -> None:
        """Moving bytes between parts never gives the same input."""
        assert self._print(path="/a", query=b"b") != self._print(path="/ab", query=b"")

    def test_its_key_is_derived_not_the_secret(self) -> None:
        plain = hmac.new(SECRET.encode(), digestmod=hashlib.sha256).digest()
        assert self._print() != plain


class TestRecorder:
    def _recorded(self, *messages: dict[str, Any]) -> idempotency.RecordedResponse:
        recorder = idempotency.RecordedResponse()
        for message in messages:
            recorder.record(message)
        return recorder

    def test_start_and_body_chunks(self) -> None:
        recorder = self._recorded(
            {"type": "http.response.start", "status": 201, "headers": [(b"a", b"b")]},
            {"type": "http.response.body", "body": b"ab", "more_body": True},
            {"type": "http.response.body", "body": b"cd"},
        )

        assert (recorder.status, recorder.headers) == (201, [(b"a", b"b")])
        assert recorder.complete
        assert recorder.body == b"abcd"

    def test_an_unfinished_body_is_incomplete(self) -> None:
        recorder = self._recorded(
            {"type": "http.response.start", "status": 200, "headers": []},
            {"type": "http.response.body", "body": b"ab", "more_body": True},
        )

        assert not recorder.complete

    def test_a_body_above_the_cap_is_dropped(self) -> None:
        chunk = b"x" * (32 * 1024)
        recorder = self._recorded(
            {"type": "http.response.start", "status": 200, "headers": []},
            *[
                {"type": "http.response.body", "body": chunk, "more_body": True}
                for _ in range(3)
            ],
            {"type": "http.response.body", "body": b""},
        )

        assert recorder.too_large
        assert recorder.body == b""


class TestWhatIsStored:
    @pytest.mark.parametrize("status", [200, 201, 204, 400, 401, 409, 422])
    def test_answers_below_500_are_stored(self, status: int) -> None:
        assert idempotency.is_storable_status(status)

    @pytest.mark.parametrize("status", [429, 500, 503])
    def test_rate_limits_and_server_errors_are_not(self, status: int) -> None:
        assert not idempotency.is_storable_status(status)

    def test_only_allowlisted_headers_are_kept(self) -> None:
        raw = [
            (b"content-type", b"application/json"),
            (b"Location", b"/api/v1/notes/1"),
            (b"WWW-Authenticate", b"Bearer"),
            (b"set-cookie", b"a=b"),
            (b"x-request-id", b"r"),
            (b"content-length", b"2"),
            (b"retry-after", b"5"),
        ]

        assert idempotency.replayed_headers(raw) == [
            ["content-type", "application/json"],
            ["Location", "/api/v1/notes/1"],
            ["WWW-Authenticate", "Bearer"],
        ]
        assert idempotency.sets_cookie(raw)
        assert not idempotency.sets_cookie(raw[:3])


class TestOpenApiMerge:
    def _problem(self, description: str) -> dict[str, Any]:
        return {"description": description, "content": {}}

    def test_codes_are_merged_into_existing_statuses_and_new_ones_added(self) -> None:
        operation: dict[str, Any] = {
            "responses": {
                "201": {"description": "Successful Response"},
                "409": self._problem("Conflict: `user_exists`"),
                "422": self._problem("Unprocessable Content: `validation_error`"),
            }
        }

        idempotency.merge_error_responses(operation)

        responses = operation["responses"]
        assert list(responses) == ["201", "400", "409", "422"]
        assert responses["400"]["description"] == (
            "Bad Request: `idempotency_key_invalid`"
        )
        assert responses["409"]["description"] == (
            "Conflict: `user_exists`, `idempotency_key_in_use`, "
            "`idempotency_outcome_unknown`"
        )
        assert responses["422"]["description"] == (
            "Unprocessable Content: `validation_error`, `idempotency_key_reused`"
        )


def _contexts(settings_factory: SettingsFactory) -> Iterator[RouteContext]:
    app = create_app(settings_factory())
    for context in iter_route_contexts(app.routes):
        dependant: Dependant | None = getattr(context, "dependant", None)
        if dependant is not None and marks_idempotent(dependant):
            yield context


def _call_name(dependant: Dependant) -> str:
    return getattr(dependant.call, "__name__", "")


class TestMarkedRouteConventions:
    """Rules for every marked route of every module."""

    def test_marked_routes_change_state(
        self, settings_factory: SettingsFactory
    ) -> None:
        for context in _contexts(settings_factory):
            assert context.methods
            assert context.methods <= {"POST", "PUT", "PATCH", "DELETE"}, (
                context.path_format
            )

    def test_marked_routes_take_no_form_body(
        self, settings_factory: SettingsFactory
    ) -> None:
        """The fingerprint reads the raw body; a parsed form has none left."""
        for context in _contexts(settings_factory):
            dependant: Dependant = context.dependant
            assert not any(
                isinstance(field.field_info, params.Form)
                for field in dependant.body_params
            ), context.path_format

    def test_a_marked_route_has_a_rate_limit_before_the_marker(
        self, settings_factory: SettingsFactory
    ) -> None:
        """A limit bounds the rows keyed requests write.

        A refused request costs one Redis call and no idempotency insert.
        """
        for context in _contexts(settings_factory):
            names = [_call_name(d) for d in context.dependant.dependencies]
            guard = names.index("idempotency_guard")
            limits = [i for i, name in enumerate(names) if name == "check_rate_limit"]
            assert limits, context.path_format
            assert all(i < guard for i in limits), context.path_format

    def test_the_marker_is_the_last_route_dependency(
        self, settings_factory: SettingsFactory
    ) -> None:
        """Checks a replay must pass are route dependencies before the marker."""
        for context in _contexts(settings_factory):
            dependencies: list[params.Depends] = context.dependencies
            last = getattr(dependencies[-1].dependency, "__name__", "")
            assert last == "idempotency_guard", context.path_format

    def test_marked_operations_document_the_header_and_errors(
        self, settings_factory: SettingsFactory
    ) -> None:
        app = create_app(settings_factory())
        document = app.openapi()
        marked = list(_contexts(settings_factory))
        assert marked
        for context in marked:
            assert context.path_format is not None
            for method in context.methods or ():
                operation = document["paths"][context.path_format][method.lower()]
                names = [p["name"] for p in operation.get("parameters", [])]
                assert names.count("Idempotency-Key") == 1
                responses = operation["responses"]
                assert "`idempotency_key_invalid`" in responses["400"]["description"]
                assert "`idempotency_key_in_use`" in responses["409"]["description"]
                assert (
                    "`idempotency_outcome_unknown`" in responses["409"]["description"]
                )
                assert "`idempotency_key_reused`" in responses["422"]["description"]

    def test_the_header_text_states_the_configured_retention(
        self, settings_factory: SettingsFactory
    ) -> None:
        document = create_app(settings_factory(IDEMPOTENCY_RETENTION_HOURS=6)).openapi()

        operation = document["paths"]["/api/v1/auth/register"]["post"]
        (parameter,) = [
            p for p in operation["parameters"] if p["name"] == "Idempotency-Key"
        ]
        assert "kept for 6 hours" in parameter["description"]

    def test_unmarked_operations_do_not_mention_the_header(
        self, settings_factory: SettingsFactory
    ) -> None:
        document = create_app(settings_factory()).openapi()
        marked = {
            (context.path_format, method.lower())
            for context in _contexts(settings_factory)
            for method in context.methods or ()
        }
        # Any: the OpenAPI document is arbitrary JSON.
        paths: dict[str, dict[str, Any]] = document["paths"]
        for path, item in paths.items():
            for method, operation in item.items():
                if (path, method) in marked:
                    continue
                parameters: list[dict[str, Any]] = operation.get("parameters", [])
                names = [parameter["name"] for parameter in parameters]
                assert "Idempotency-Key" not in names, (path, method)
