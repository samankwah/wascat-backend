# wascat-backend

The WASCAT archive: the catalogue, its imagery, the measurements taken from it,
and the API both the public site and the dashboard read.

FastAPI, PostgreSQL, SQLAlchemy 2.0 async, MinIO (or any S3), Python 3.13.

## Running it

Docker Desktop has to be running; `tasks.ps1 up` will start it if it is not.

```powershell
.\tasks.ps1 up        # PostgreSQL + MinIO
.\tasks.ps1 migrate
uv run wascat db seed --frames ..\wascat-frontend\public\frames
uv run wascat users create you@example.org --role admin   # prints a password once
.\tasks.ps1 dev       # http://localhost:8000, docs at /api/v1/docs
```

`uv run wascat doctor` reports on Python, the database, object storage,
migrations and configuration when something is not working.

## Accounts

The first one is made here, because the dashboard needs an account to sign in
with. Everything after that is done in the dashboard, under **People**.

```powershell
uv run wascat users list                      # who exists, and their roles
uv run wascat users create you@example.org --role admin
uv run wascat users passwd you@example.org    # locked out: prints a new one, ends their sessions
uv run wascat users grant you@example.org --role admin
```

There is no sign-up. Accounts are granted, not self-served, and no mail is ever
sent to a sign-in address - it is an identifier, not a destination. A password
is generated and shown once; if it is lost, reset it rather than recovering it.

Roles: `admin` (everything, including publishing and managing people),
`curator` (edits the catalogue and vocabularies), `viewer` (read-only).

## What the archive holds

All-sky camera frames from eleven capture sequences, some paired with a binary
cloud-segmentation mask. Cloud cover is measured from the mask as a share of
the camera's circular field of view, and reported in oktas.

The rule everything else follows from: **an unmeasured value is absent, never
zero.** A frame with no mask has no cloud cover, so it reports none - not a
clear sky nobody observed. That shows up as a CHECK constraint, as two partial
indexes, as a write schema that will not accept a measurement, and as a dashed
"unsegmented" chip rather than "0/8".

## Layout

```
src/wascat/
  core/       config, db, errors, the response envelope, security, deps
  api/        app assembly, middleware, CSRF
  domains/
    catalog/  records, collections, releases - the public read API and the
              dashboard's write API
    vocab/    the controlled vocabularies the filters and the API validate on
    iam/      users, roles, sessions
    audit/    who changed what
    ingest/   loading imagery: the legacy import and the backfill
    meteorology/ climate/ advisory/   reserved for the science to come
  storage/    object store: S3, filesystem, imaging
  cli/        the `wascat` command
```

`.importlinter` enforces the layering (`api → domains → storage|jobs → core`)
and keeps the reserved science domains from entangling with the catalogue.

## The rules, and where they live

Domain invariants are enforced in the database, not only in service code, so
they hold whichever path writes:

| | |
|---|---|
| A record holds a source, a mask, or both — never neither | deferred trigger |
| Cloud cover requires a mask | deferred trigger |
| Fraction and okta bucket present together or not at all | CHECK |
| The bucket follows from the fraction | CHECK |
| Artifact dimensions match the record (derivatives exempt) | deferred trigger |
| One artifact of each type per record | unique index |
| Published releases are immutable | trigger |
| Cloud-cover and date filters exclude records without one | partial indexes |
| A draft release is not part of the public archive | query scoping |

## Ingest

```powershell
# Load the generated catalogue and its frames.
uv run wascat db seed --frames <dir>

# Add the delivered frames the original sampled catalogue left out.
uv run wascat ingest backfill --source <corner_mask dir> --into-version 1.1 --dry-run
```

Neither recomputes a measurement. The seed copies what the original pipeline
published; the backfill only handles frames with no mask, which have nothing to
measure. Re-measuring is a separate, deliberate operation that produces a new
release, because the published numbers are the output of a specific toolchain
and must not shift because storage moved.

## Checks

```powershell
.\tasks.ps1 check     # ruff, format, mypy --strict, import-linter
.\tasks.ps1 test      # pytest
```

Tests use a throwaway database, rebuilt each session. Most run without Docker
by using the filesystem object store; the ones that need PostgreSQL are marked
`db`.
