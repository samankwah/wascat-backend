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

## Deploy

The API runs on Vercel as a single Python function. `pyproject.toml`'s
`[tool.vercel]` names the entrypoint, `vercel.json` pins it to `dub1`, next to
the database, and `.vercelignore` keeps tests, seed data and migrations out of
the bundle. Postgres is a Supabase project, and object storage is any
S3-compatible endpoint (Cloudflare R2 in production).

Database (Supabase):

1. In the project, copy **Connect → Transaction pooler** (port 6543). It suits
   serverless, where every instance would otherwise hold its own pool.
2. Turn it into `postgresql+asyncpg://…:6543/postgres?ssl=require`, and set
   `WASCAT_DB_PGBOUNCER=true` beside it, which turns off asyncpg's
   prepared-statement cache. From a workstation, the session pooler (5432)
   works without the flag.
3. Run migrations from a workstation before deploying code that needs them:
   `uv run alembic upgrade head`, then `uv run wascat doctor`. Vercel never
   runs them; a cold start must not race another one to migrate.
4. Supabase also serves `public` over its REST API to the `anon` key. This
   service never uses that API, so migration `3c9f1a7e2b80` revokes those
   roles' access and `doctor` fails if any comes back. Turning off **Data API**
   in the project settings closes it at the edge as well.

Tests still need a local Postgres (`docker compose up`), because they create
and drop databases.

Vercel project environment variables (Production):

| Variable | Value |
| --- | --- |
| `WASCAT_ENVIRONMENT` | `production` |
| `WASCAT_DATABASE_URL` | the transaction-pooler URL from step 2 (secret) |
| `WASCAT_DB_PGBOUNCER` | `true` |
| `WASCAT_SECRET_KEY` | 48+ random characters (secret) |
| `WASCAT_PUBLIC_BASE_URL` | the frontend's origin, e.g. `https://wascat-frontend.netlify.app` |
| `WASCAT_ADMIN_ORIGINS` | `["https://wascat-frontend.netlify.app"]` |
| `WASCAT_S3_ENDPOINT_URL`, `WASCAT_S3_ACCESS_KEY`, `WASCAT_S3_SECRET_KEY` | from the R2 API token (secret) |
| `WASCAT_S3_BUCKET` / `WASCAT_S3_REGION` | `wascat` / `auto` |
| `WASCAT_FRONTEND_REVALIDATE_URL` | `https://wascat-frontend.netlify.app/api/revalidate` |
| `WASCAT_MAX_UPLOAD_BYTES` | `4000000` |
| `WASCAT_FRONTEND_REVALIDATE_SECRET` | must equal the frontend's `WASCAT_REVALIDATE_SECRET` (secret) |

The frontend's `WASCAT_API_ORIGIN` then points at the Vercel deployment's
URL.

Vercel caps a request body at 4.5 MB, below `WASCAT_MAX_UPLOAD_BYTES`' 32 MB
default, so set it to `4000000` there (4 MB, leaving room for the multipart
framing). A larger frame then gets the API's own error rather than a bare 413
from the platform. Bulk ingest goes through the CLI from a workstation,
which has no such limit.

`Dockerfile` (and `render.yaml`, for Render) still build the same app as a
long-running container, should it ever need a host other than Vercel.

`WASCAT_ENVIRONMENT=production` makes the app refuse to start on a
development default: see `Settings._refuse_dev_defaults_in_production` in
`src/wascat/core/config.py` before assuming a misconfigured deploy will just
work with the wrong secret.
