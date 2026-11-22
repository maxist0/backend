# Adding a module

How to add a feature to the backend as a module, step by step, with a complete example. For developers who build an app on this boilerplate. Form: how-to, followed by a reference section on what the tests enforce.

A module is a package under `src/app/modules/` plus one registration line. You never edit the core ([ADR 0001](adr/0001-core-and-module-contract.md), [ADR 0008](adr/0008-module-layout-and-users-auth-split.md)). The rules this guide follows are in [AGENTS.md](../AGENTS.md): Core vs modules, Module layout, Architecture rules, Errors and responses, Database and migrations, Caching and background jobs and Testing. Read them once; this page shows them applied.

## The example

The guide builds a `notes` module: each signed-in user has private notes, and a background task stores the word count of every note. It is a generic example, not a feature of the boilerplate, and it does **not** live in the repository.

| Method | Path | Status | Purpose |
|---|---|---|---|
| POST | `/api/v1/notes` | 201 | Create a note; queues the word count |
| GET | `/api/v1/notes` | 200 | List your notes, newest first (`limit` 1 to 100, `offset`) |
| GET | `/api/v1/notes/{note_id}` | 200 | Read one note |
| PATCH | `/api/v1/notes/{note_id}` | 200 | Change the title or the text |
| DELETE | `/api/v1/notes/{note_id}` | 204 | Delete a note |

**Verified.** On 2026-11-22 the code below was added to a throwaway git worktree of commit `6d01f6d`. `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run pyright` and the full `uv run pytest` suite passed. The same day, after the schema conventions of [ADR 0012](adr/0012-schema-conventions-uuidv7-value-enums-usernames.md) (mixins, naming convention, migration baseline `b799370af501`), the model and the migration were updated and checked again: the code blocks were written into the working tree with the module registered, `alembic revision --autogenerate` ran against a throwaway Postgres 18, the migration ran through `upgrade`, `downgrade -1`, `upgrade` and `alembic check`, and ruff, mypy, pyright and the notes tests passed. The code blocks are copies of those files.

## Step 1: create the package

Create only the files you need, with these names and no others (a unit test enforces it):

```
src/app/modules/notes/
  __init__.py     the Module(...) object
  models.py       the Note table
  schemas.py      request and response models
  errors.py       NoteNotFoundError
  crud.py         queries
  service.py      business rules and the transaction
  tasks.py        the word-count task
  router.py       the HTTP routes
  admin.py        a read-only admin view
```

A module without tasks or an admin view has no `tasks.py` or `admin.py`.

## Step 2: the model

`models.py` subclasses `Base` from `app.core` and takes the primary key and the timestamps from the core mixins ([database.md → Model conventions](database.md#model-conventions), [ADR 0012](adr/0012-schema-conventions-uuidv7-value-enums-usernames.md)):

| Mixin or helper | Gives the table |
|---|---|
| `UUIDPrimaryKey` | `id`, a UUIDv7 generated in Python |
| `Timestamps` | `created_at` and `updated_at`, `timestamptz` with the server default `now()`; the ORM updates `updated_at` |
| `CreatedAt` | only `created_at`, for rows that never change (logs, tokens) |
| `value_enum(MyEnum)` | a column type that stores the enum's values in a VARCHAR with a CHECK constraint, never a PostgreSQL enum type |

Add an explicit index for every foreign key and filter column. The foreign key to `users.id` names the table as a string, so the module does not need to import the `users` models. Constraint names come from the core's naming convention.

A UUIDv7 reveals when its row was created. If that must stay private for your table, leave out `UUIDPrimaryKey` and declare `id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)` yourself.

`src/app/modules/notes/models.py`:

```python
"""ORM model of the notes module."""

import uuid

from sqlalchemy import ForeignKey, Index, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core import Base, Timestamps, UUIDPrimaryKey


class Note(UUIDPrimaryKey, Timestamps, Base):
    """A note that belongs to one user."""

    __tablename__ = "notes"
    # Serves the owner's list (newest first) and the foreign key's lookups.
    __table_args__ = (Index("ix_notes_owner_id_created_at", "owner_id", "created_at"),)

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Set by the `notes.count_words` task; None until it has run.
    word_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    def __str__(self) -> str:
        """Label of the note in the admin panel."""
        return self.title
```

## Step 3: the schemas

`schemas.py` holds the Pydantic models. Request models set `extra="forbid"` and bound every input. Create, update and response models are separate, and the API never returns an ORM object.

`src/app/modules/notes/schemas.py`:

```python
"""Request and response schemas of the notes module."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CreateNoteRequest(BaseModel):
    """A new note."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=10_000)


class UpdateNoteRequest(BaseModel):
    """Fields of a note to change; omitted fields stay unchanged."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = Field(default=None, max_length=10_000)


class NoteResponse(BaseModel):
    """A note as the API returns it."""

    id: str
    title: str
    body: str
    word_count: int | None
    created_at: datetime
    updated_at: datetime
```

## Step 4: the errors

`errors.py` subclasses the `AppError` family from `app.core` (`BadRequest`, `Unauthorized`, `Forbidden`, `NotFound`, `Conflict`) and sets a stable `code`. Clients branch on the `code`; the handlers render the problem response ([api.md](api.md#error-format)). Services raise the error; routes never catch `Exception`.

`src/app/modules/notes/errors.py`:

```python
"""Error codes of the notes module."""

from app.core import NotFound


class NoteNotFoundError(NotFound):
    """The note does not exist or belongs to another user.

    One answer for both, so the response does not reveal that a note exists.
    """

    code = "note_not_found"
    detail = "The note was not found."
```

## Step 5: the queries

`crud.py` contains queries and no business rules. It does not commit: the service owns the transaction.

`src/app/modules/notes/crud.py`:

```python
"""Data-access layer of the notes module: queries on `notes`."""

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.notes.models import Note


async def add_note(db: AsyncSession, note: Note) -> Note:
    """Add `note` and flush, so its defaults are set. Does not commit."""
    db.add(note)
    await db.flush()
    return note


async def get_note(db: AsyncSession, note_id: uuid.UUID) -> Note | None:
    """Return the note with this id, or None."""
    result = await db.execute(select(Note).where(Note.id == note_id))
    return result.scalars().first()


async def list_notes(
    db: AsyncSession, owner_id: uuid.UUID, *, limit: int, offset: int
) -> Sequence[Note]:
    """Return a page of the owner's notes, newest first."""
    result = await db.execute(
        select(Note)
        .where(Note.owner_id == owner_id)
        .order_by(Note.created_at.desc(), Note.id)
        .limit(limit)
        .offset(offset)
    )
    return result.scalars().all()


async def delete_note(db: AsyncSession, note: Note) -> None:
    """Delete `note`. Does not commit."""
    await db.delete(note)
```

## Step 6: the service

`service.py` holds the rules and the transaction. It checks ownership (an unknown id and another user's id give the same 404), commits, and only then enqueues the task, so the worker finds the row. `CurrentUser` is resolved in the route; the service takes the `User`.

`src/app/modules/notes/service.py`:

```python
"""Business logic of the notes module: a user's notes and their word count."""

import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.notes import crud
from app.modules.notes.errors import NoteNotFoundError
from app.modules.notes.models import Note
from app.modules.notes.schemas import NoteResponse
from app.modules.notes.tasks import count_words_task
from app.modules.users import User


def note_to_response(note: Note) -> NoteResponse:
    """Convert a Note ORM object to its response model."""
    return NoteResponse(
        id=str(note.id),
        title=note.title,
        body=note.body,
        word_count=note.word_count,
        created_at=note.created_at,
        updated_at=note.updated_at,
    )


async def _get_owned_note(db: AsyncSession, owner: User, note_id: uuid.UUID) -> Note:
    """Return the note, or raise `NoteNotFoundError` if it is missing or not owned."""
    note = await crud.get_note(db, note_id)
    if note is None or note.owner_id != owner.id:
        raise NoteNotFoundError
    return note


async def create_note(db: AsyncSession, owner: User, *, title: str, body: str) -> Note:
    """Create a note for `owner` and queue its word count.

    Args:
        db: The request's session.
        owner: The signed-in user.
        title: The note's title.
        body: The note's text.

    Returns:
        The created note; `word_count` is set by the task later.
    """
    note = await crud.add_note(db, Note(owner_id=owner.id, title=title, body=body))
    await db.commit()
    # After the commit: the worker must find the row.
    await count_words_task.kiq(str(note.id))
    return note


async def list_notes(
    db: AsyncSession, owner: User, *, limit: int, offset: int
) -> Sequence[Note]:
    """Return a page of the owner's notes, newest first."""
    return await crud.list_notes(db, owner.id, limit=limit, offset=offset)


async def get_note(db: AsyncSession, owner: User, note_id: uuid.UUID) -> Note:
    """Return one of the owner's notes.

    Raises:
        NoteNotFoundError: The note does not exist or belongs to another user.
    """
    return await _get_owned_note(db, owner, note_id)


async def update_note(
    db: AsyncSession,
    owner: User,
    note_id: uuid.UUID,
    *,
    title: str | None,
    body: str | None,
) -> Note:
    """Change the given fields of one of the owner's notes.

    A new body queues a new word count.

    Raises:
        NoteNotFoundError: The note does not exist or belongs to another user.
    """
    note = await _get_owned_note(db, owner, note_id)
    if title is not None:
        note.title = title
    if body is not None:
        note.body = body
        note.word_count = None
    await db.commit()
    if body is not None:
        await count_words_task.kiq(str(note.id))
    return note


async def delete_note(db: AsyncSession, owner: User, note_id: uuid.UUID) -> None:
    """Delete one of the owner's notes.

    Raises:
        NoteNotFoundError: The note does not exist or belongs to another user.
    """
    note = await _get_owned_note(db, owner, note_id)
    await crud.delete_note(db, note)
    await db.commit()


async def store_word_count(db: AsyncSession, note_id: uuid.UUID) -> bool:
    """Store the number of words of a note; the task's unit of work.

    Idempotent: a second run stores the same value.

    Returns:
        False if the note no longer exists.
    """
    note = await crud.get_note(db, note_id)
    if note is None:
        return False
    note.word_count = len(note.body.split())
    await db.commit()
    return True
```

## Step 7: the task

`tasks.py` declares tasks with `@task(task_name=...)` from `app.core`. Rules the example follows:

- The task gets the row id as a string, never the note's content or any personal data: task arguments are stored in Redis and logged by taskiq at DEBUG.
- `db: TaskDbSession = TaskiqDepends()` gives the task a database session ([ADR 0010](adr/0010-token-emails-rekeyed-in-the-worker.md)). The default only lets `kiq()` type-check; `TaskiqDepends` comes from `taskiq`.
- Delivery is at least once ([ADR 0009](adr/0009-taskiq-redis-stream-broker-and-delayed-retries.md)), so the task is idempotent: running it twice stores the same value.
- `retry_on_error=True` retries the task when it raises `TransientTaskError` or hits a transient database error. A scheduler must run for retries ([redis.md](redis.md)).
- The service imports the task to enqueue it, so the task imports the service inside the function. Without that, the two modules would import each other.

`src/app/modules/notes/tasks.py`:

```python
"""Background tasks of the notes module."""

import uuid

from taskiq import TaskiqDepends

from app.core import TaskDbSession, get_logger, task

logger = get_logger(__name__)


@task(task_name="notes.count_words", retry_on_error=True)
async def count_words_task(note_id: str, db: TaskDbSession = TaskiqDepends()) -> None:
    """Store the word count of one note.

    The task gets the note's id only and reads the text itself, so no note
    content is stored in Redis. Idempotent: a redelivery stores the same
    value.

    Args:
        note_id: The note's id as a string.
        db: The task's database session.
    """
    from app.modules.notes import service  # noqa: PLC0415 - service imports tasks

    found = await service.store_word_count(db, uuid.UUID(note_id))
    logger.info("notes.word_count_stored", note_id=note_id, found=found)
```

A task that must run on a schedule adds `schedule=[{"cron": "0 3 * * *"}]` (UTC). The only scheduled task of the built-in modules is `auth.cleanup` in `src/app/modules/auth/tasks.py`.

## Step 8: the routes

`router.py` is thin: it validates through the models, calls one service function and returns a typed model. Every route declares `status_code`, a `summary` (imperative, at most 60 characters), a return annotation and the errors it can produce through `problem_responses(...)`. The docstring renders in Swagger, so write it for API consumers. The router carries its own `prefix` and `tags`; `create_app()` adds `API_PREFIX`.

`src/app/modules/notes/router.py`:

```python
"""HTTP routes of the notes module: the signed-in user's notes (`/notes`)."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.core import DbSession, Unauthorized, problem_responses
from app.modules.notes import service
from app.modules.notes.errors import NoteNotFoundError
from app.modules.notes.schemas import (
    CreateNoteRequest,
    NoteResponse,
    UpdateNoteRequest,
)
from app.modules.users import CurrentUser

router = APIRouter(prefix="/notes", tags=["notes"])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Create a note",
    responses=problem_responses(Unauthorized, 422),
)
async def create_note(
    body: CreateNoteRequest, db: DbSession, current_user: CurrentUser
) -> NoteResponse:
    """Create a note for the signed-in user.

    The word count is computed in the background: it is `null` in this
    answer and set shortly after.
    """
    note = await service.create_note(db, current_user, title=body.title, body=body.body)
    return service.note_to_response(note)


@router.get(
    "",
    status_code=status.HTTP_200_OK,
    summary="List the current user's notes",
    responses=problem_responses(Unauthorized, 422),
)
async def list_notes(
    db: DbSession,
    current_user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[NoteResponse]:
    """Return a page of the signed-in user's notes, newest first."""
    notes = await service.list_notes(db, current_user, limit=limit, offset=offset)
    return [service.note_to_response(note) for note in notes]


@router.get(
    "/{note_id}",
    status_code=status.HTTP_200_OK,
    summary="Get a note",
    responses=problem_responses(Unauthorized, NoteNotFoundError, 422),
)
async def get_note(
    note_id: uuid.UUID, db: DbSession, current_user: CurrentUser
) -> NoteResponse:
    """Return one of the signed-in user's notes."""
    note = await service.get_note(db, current_user, note_id)
    return service.note_to_response(note)


@router.patch(
    "/{note_id}",
    status_code=status.HTTP_200_OK,
    summary="Update a note",
    responses=problem_responses(Unauthorized, NoteNotFoundError, 422),
)
async def update_note(
    note_id: uuid.UUID,
    body: UpdateNoteRequest,
    db: DbSession,
    current_user: CurrentUser,
) -> NoteResponse:
    """Change the title or the text of one of the signed-in user's notes.

    Omitted or null fields stay unchanged. A new text resets the word count,
    which is computed again in the background.
    """
    note = await service.update_note(
        db, current_user, note_id, title=body.title, body=body.body
    )
    return service.note_to_response(note)


@router.delete(
    "/{note_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete a note",
    responses=problem_responses(Unauthorized, NoteNotFoundError, 422),
)
async def delete_note(
    note_id: uuid.UUID, db: DbSession, current_user: CurrentUser
) -> None:
    """Delete one of the signed-in user's notes."""
    await service.delete_note(db, current_user, note_id)
```

## Step 9: the admin view

`admin.py` registers a read-only sqladmin view. Views never write directly: a change is an action that calls a service function and writes an audit row (see `src/app/modules/auth/admin.py` and [ADR 0006](adr/0006-slim-admin-via-services.md)). The panel is mounted only when `ADMIN_ENABLED` allows it.

`src/app/modules/notes/admin.py`:

```python
"""Admin panel view of the notes module, registered via `Module.admin_views`."""

from typing import ClassVar

from sqladmin import ModelView

from app.modules.notes.models import Note


class NoteAdmin(ModelView, model=Note):
    """Notes, read-only."""

    name_plural = "Notes"
    icon = "fa-solid fa-note-sticky"

    can_create = False
    can_edit = False
    can_delete = False

    column_list: ClassVar = [
        Note.id,
        Note.owner_id,
        Note.title,
        Note.word_count,
        Note.created_at,
    ]
    column_sortable_list: ClassVar = [Note.title, Note.word_count, Note.created_at]
    column_default_sort = (Note.created_at, True)


VIEWS: tuple[type[ModelView], ...] = (NoteAdmin,)
```

## Step 10: register the module

`__init__.py` builds the `Module`. `Module` is a frozen dataclass; `models`, `tasks` and `admin_views` are optional.

`src/app/modules/notes/__init__.py`:

```python
"""Notes module: the private notes of a user."""

from app.core import Module
from app.modules.notes import models, tasks
from app.modules.notes.admin import VIEWS
from app.modules.notes.router import router

module = Module(
    name="notes",
    router=router,
    models=models,
    tasks=tasks,
    admin_views=VIEWS,
)

__all__ = ["module"]
```

Then add the module to `MODULES` in `src/app/modules/__init__.py`: the import and one entry, after the modules it depends on. There is no auto-discovery. The `name` of the `Module` must equal the folder name, and a unit test fails when a package under `src/app/modules/` is not listed. The order is the dependency order, and a unit test checks that a module imports only modules listed before it.

`src/app/modules/__init__.py`:

```python
"""The registered modules. Adding a module is one line in `MODULES`.

The order is the dependency order: a module may import only modules listed
before it (`tests/unit/core/test_import_direction.py` enforces this).
Importing this package reads no settings and opens no connection, so Alembic
can import it to see every module's tables.
"""

from app.core import Module
from app.modules import auth, notes, users

MODULES: tuple[Module, ...] = (users.module, auth.module, notes.module)
```

From now on `create_app()` mounts the router under `API_PREFIX`, the worker imports `tasks`, Alembic imports `models` and the admin registers `admin_views`.

## Step 11: the migration

Start the local database, apply the existing migrations, let Alembic compare the models with the database and write the revision, then check it:

```bash
docker compose -f compose.dev.yaml up -d --wait
export DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/api   # PowerShell: $env:DATABASE_URL = "..."
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "add_notes"
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head
uv run alembic check
```

`ENVIRONMENT=dev` in place of the `export` reads `DATABASE_URL` from `.env.local` ([README → Quick start](../README.md#quick-start)). Apply migrations to your local database only. The post-write hooks in `alembic.ini` run ruff `--fix` and `ruff format` on the new file, so it already follows the house style. Read it anyway: autogenerate misses some changes (renamed columns, data migrations, server defaults you want, the values of a `value_enum` CHECK constraint) and its output is a draft. The constraint names come from the naming convention and are written out with `op.f(...)`. The revision id and the date differ on your machine. The result for the example:

`alembic/versions/20261122_1923_a79585c2bce8_add_notes.py`:

```python
"""add_notes

Revision ID: a79585c2bce8
Revises: b799370af501
Create Date: 2026-11-22 19:23:17.341824+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a79585c2bce8"
down_revision: str | Sequence[str] | None = "b799370af501"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "notes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("word_count", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_notes_owner_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notes")),
    )
    op.create_index(
        "ix_notes_owner_id_created_at",
        "notes",
        ["owner_id", "created_at"],
        unique=False,
    )
    # ### end Alembic commands ###


def downgrade() -> None:
    """Downgrade schema."""
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index("ix_notes_owner_id_created_at", table_name="notes")
    op.drop_table("notes")
    # ### end Alembic commands ###
```

Never edit an applied migration; add a new one. The downgrade must work, as the commands above prove.

## Step 12: the tests

Every new endpoint gets a happy path, a validation error, an auth failure, a not-found or conflict case and one test that proves the side effect. A test must fail without the change it covers. Put the tests in `tests/unit/modules/notes/` and `tests/integration/modules/notes/`, each with an `__init__.py` that holds a one-line docstring (ruff `D104`), for example `"""Unit tests for the notes module."""`. Module-specific fixtures go into a `conftest.py` in your folder; never edit the root `conftest.py`.

**Unit tests** need no Docker. Rule: if the code under test needs no database or Redis, it is a unit test.

`tests/unit/modules/notes/test_schemas_and_responses.py`:

```python
"""Notes schemas bound their input; `note_to_response` maps a note without I/O."""

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.modules import MODULES
from app.modules.notes import module
from app.modules.notes.models import Note
from app.modules.notes.schemas import CreateNoteRequest, UpdateNoteRequest
from app.modules.notes.service import note_to_response


def test_the_module_is_registered() -> None:
    assert module in MODULES


@pytest.mark.parametrize(
    "payload",
    [
        {"title": ""},
        {"title": "x" * 201},
        {"title": "ok", "body": "x" * 10_001},
        {"title": "ok", "owner_id": str(uuid.uuid4())},
    ],
    ids=["empty title", "long title", "long body", "unknown field"],
)
def test_create_request_rejects_invalid_input(payload: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        CreateNoteRequest.model_validate(payload)


def test_create_request_defaults_to_an_empty_body() -> None:
    assert CreateNoteRequest(title="Groceries").body == ""


def test_update_request_leaves_omitted_fields_unset() -> None:
    request = UpdateNoteRequest.model_validate({"title": "New"})

    assert request.title == "New"
    assert request.body is None


def test_response_carries_the_note_without_orm_types() -> None:
    created = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
    note = Note(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        title="Groceries",
        body="milk eggs",
        word_count=2,
        created_at=created,
        updated_at=created,
    )

    response = note_to_response(note)

    assert response.id == str(note.id)
    assert response.title == "Groceries"
    assert response.word_count == 2
    assert response.created_at == created
```

**Integration tests** run against Postgres and Redis Testcontainers (Docker must run). They use the shared helpers in `tests/helpers.py`:

| Helper | Use |
|---|---|
| `register_and_login(client, username=...)` | register and sign in; returns the access token |
| `bearer(token)` | the `Authorization` header |
| `Db` (the `db` fixture) | plain SQL for assertions: `scalar`, `rows`, `one`, `execute` |
| `run_task(client, task, *args)` | run a task outside a request, in-process |
| `redis_dump()`, `assert_nothing_personal_in_redis(...)` | prove what is, and is not, stored in Redis |
| `log_events(caplog, name)` | read structured log events |

Fixtures: `client` runs tasks in-process (a failing task fails the test), `redis_broker_client` leaves them in the Redis stream, `captured_emails` replaces the mailer. Registering a user needs `captured_emails`. The database is truncated before each test. Do not mock the database. Add a helper to `tests/helpers.py` instead of copying one into your test module.

`tests/integration/modules/notes/test_notes.py`:

```python
"""Integration tests for `/api/v1/notes`: CRUD, ownership and the word-count task.

Real PostgreSQL and Redis come from the integration fixtures. The task runs
in-process, so a count is stored before the request answers. `captured_emails`
is needed for register.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.modules.notes.tasks import count_words_task
from tests.helpers import (
    Db,
    assert_nothing_personal_in_redis,
    bearer,
    redis_dump,
    register_and_login,
    run_task,
)

NOTES = "/api/v1/notes"
SECRET_BODY = "dentist appointment on the third"


def _create(client: TestClient, token: str, title: str, body: str = "") -> str:
    r = client.post(NOTES, json={"title": title, "body": body}, headers=bearer(token))
    assert r.status_code == 201, r.text
    note_id: str = r.json()["id"]
    return note_id


@pytest.mark.usefixtures("captured_emails")
class TestCreateNote:
    def test_creates_a_note_and_counts_its_words(self, client: TestClient) -> None:
        token = register_and_login(client, username="alice")
        note_id = _create(client, token, "Groceries", "milk eggs  and\nflour")

        r = client.get(f"{NOTES}/{note_id}", headers=bearer(token))

        assert r.status_code == 200
        assert r.json()["title"] == "Groceries"
        assert r.json()["word_count"] == 4

    def test_rejects_an_empty_title(self, client: TestClient) -> None:
        token = register_and_login(client, username="alice")
        r = client.post(NOTES, json={"title": ""}, headers=bearer(token))

        assert r.status_code == 422
        assert r.json()["code"] == "validation_error"
        assert r.json()["errors"][0]["field"] == "title"

    def test_requires_authentication(self, client: TestClient) -> None:
        r = client.post(NOTES, json={"title": "x"})

        assert r.status_code == 401
        assert r.json()["code"] == "unauthorized"

    def test_queued_task_holds_the_note_id_and_no_content(
        self, redis_broker_client: TestClient
    ) -> None:
        token = register_and_login(redis_broker_client, username="alice")
        note_id = _create(redis_broker_client, token, "Appointment", SECRET_BODY)

        assert note_id.encode() in redis_dump()
        assert_nothing_personal_in_redis(SECRET_BODY, "Appointment")


@pytest.mark.usefixtures("captured_emails")
class TestOwnership:
    def test_lists_only_own_notes_newest_first(self, client: TestClient) -> None:
        alice = register_and_login(client, username="alice")
        bob = register_and_login(client, username="bob")
        _create(client, alice, "first")
        _create(client, alice, "second")
        _create(client, bob, "bobs")

        r = client.get(NOTES, headers=bearer(alice))

        assert [note["title"] for note in r.json()] == ["second", "first"]

    def test_answers_404_for_another_users_note(
        self, client: TestClient, db: Db
    ) -> None:
        alice = register_and_login(client, username="alice")
        bob = register_and_login(client, username="bob")
        note_id = _create(client, alice, "private")

        read = client.get(f"{NOTES}/{note_id}", headers=bearer(bob))
        write = client.patch(
            f"{NOTES}/{note_id}", json={"title": "mine now"}, headers=bearer(bob)
        )
        delete = client.delete(f"{NOTES}/{note_id}", headers=bearer(bob))

        assert [read.status_code, write.status_code, delete.status_code] == [404] * 3
        assert read.json()["code"] == "note_not_found"
        assert db.scalar("SELECT title FROM notes") == "private"

    def test_deletes_own_note(self, client: TestClient, db: Db) -> None:
        token = register_and_login(client, username="alice")
        note_id = _create(client, token, "gone")

        r = client.delete(f"{NOTES}/{note_id}", headers=bearer(token))

        assert r.status_code == 204
        assert db.scalar("SELECT count(*) FROM notes") == 0


@pytest.mark.usefixtures("captured_emails")
class TestCountWordsTask:
    def test_is_idempotent(self, client: TestClient, db: Db) -> None:
        token = register_and_login(client, username="alice")
        note_id = _create(client, token, "t", "one two three")

        run_task(client, count_words_task, note_id)
        run_task(client, count_words_task, note_id)

        assert db.scalar("SELECT word_count FROM notes") == 3

    def test_skips_a_note_that_was_deleted(self, client: TestClient) -> None:
        run_task(client, count_words_task, str(uuid.uuid4()))
```

## Step 13: regenerate the OpenAPI snapshot

Your routes change the API contract, so the snapshot test fails until you regenerate the committed document:

```bash
UPDATE_OPENAPI_SNAPSHOT=1 uv run pytest tests/unit/core/test_openapi_contract.py
```

Review the diff of `tests/unit/core/openapi_snapshot.json`: it must contain only your paths and schemas.

No other core file changes. The core tests read the tables and tasks from `Module.models` and `Module.tasks` and fail only when a table or task exists outside a registered module. To pin your module's own tables and task names, add a test like `tests/unit/modules/auth/test_module_contents.py`, with the helpers `module_tables` and `declared_tasks` from `tests/helpers.py`:

```python
from app.modules.notes import module
from tests.helpers import declared_tasks, module_tables


def test_declares_its_table_and_task() -> None:
    assert module_tables(module) == {"notes"}
    assert [t.task_name for t in declared_tasks(module.tasks)] == ["notes.count_words"]
```

Queued messages carry task names, so renaming a task strands them: choose the name once.

## Step 14: run the gates

```bash
uv run ruff check src tests run.py alembic
uv run ruff format --check src tests run.py alembic
uv run mypy
uv run pyright
uv run pytest
```

`uv run pytest tests/unit --no-cov` is the fast loop without Docker. Then start `uv run python run.py dev`, open `http://localhost:8000/docs` and try the routes.

## What the tests enforce

| Test | Rule |
|---|---|
| `tests/unit/core/test_module_layout.py` | A module contains only the vocabulary files and a flat `templates/` folder (no `base.html`). |
| `tests/unit/core/test_import_direction.py` | Every module package is listed in `MODULES` under its folder name; modules import `app.core`, not its submodules; the core never imports `app.modules`; a module imports only modules listed before it. |
| `tests/unit/core/test_model_registry.py` | Importing `app.modules` registers exactly the tables of the modules' `models` files, without `ENVIRONMENT` and without reading settings; no table exists outside a registered module; a declared `models` file maps at least one table. |
| `tests/unit/core/test_openapi_contract.py` | Every module path is under the prefix; every module route declares problem-detail errors; the document equals the snapshot. |
| `tests/unit/core/test_taskiq_and_logging.py` | The worker serves exactly the core's tasks and the tasks of every `Module.tasks`, each name once; the scheduler finds every `schedule` label. |

## Docstrings and comments

House style ([AGENTS.md → Comments and docstrings](../AGENTS.md#comments-and-docstrings)), as in the example: English, Google-style docstrings with single backticks, `Args:`, `Returns:` and `Raises:` where they add something, and an inline comment only for a WHY the code cannot express. Public functions and classes need a docstring (ruff `D`, google convention); route docstrings are user-facing. No ADR, review or phase IDs in code: they belong in commit messages and ADRs.

## More module features

| Need | How |
|---|---|
| A FastAPI dependency | `dependencies.py` with `Annotated` aliases, for example `CurrentUser` in `src/app/modules/users/dependencies.py` |
| Email | Put Jinja templates into `templates/` (never `base.html`; that is the core layout in `src/app/core/templates/`) and call `send_templated_email(..., template_dir=...)` from a task. It returns `False` when mail is not configured. See `src/app/modules/auth/tasks.py` |
| Caching | `@cached(ttl, key)` and `invalidate(pattern)` from `app.core`; the key holds every input that changes the result, `{user_id}` included ([redis.md](redis.md)) |
| Roles and permissions, user events, per-module settings | Not offered yet (open decision D3, [ADR 0001](adr/0001-core-and-module-contract.md)). A new setting is an edit to `src/app/core/config.py`, which needs the owner's approval. Do not build workarounds. |

If a task seems to need an edit to the core, stop and ask: either the core has a gap, which is a core change with an ADR, or the code belongs in a module.
