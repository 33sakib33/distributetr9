# M02 — Control plane

## Goal

A FastAPI app with PostgreSQL and MinIO that stores jobs and submission bytes. No GPU execution yet. Jobs are created in `validating` then `queued`.

## In scope

- App skeleton, config, migrations
- MinIO bucket bootstrap
- Catalog `environments` seed
- `POST /jobs` (upload + validate)
- `GET /jobs`, `GET /jobs/{id}`
- `POST /jobs/{id}/cancel` (queued jobs only until M04)
- Default quotas rows
- Health: `GET /health`

## Out of scope

- Agent routes (M05)
- Scheduler workers (M04)
- Portal UI (M11)
- Custom image builds (M09)

## Dependencies

M01.

## Unlocks

M04, M10 (submission object keys exist).

## Contracts

Tables: all of [02-data-model.md](../02-data-model.md). Create them now even if unused.

Config:

```text
DATABASE_URL
MINIO_ENDPOINT, MINIO_ACCESS_KEY, MINIO_SECRET_KEY, MINIO_BUCKET
OIDC_*
MAX_UPLOAD_BYTES=524288000
GLOBAL_MAX_RUNTIME_SECONDS=86400
```

`POST /jobs` writes:

1. `artifacts` type=input
2. object `submissions/{job_id}/input.tar.gz`
3. `jobs` row with `desired_state=run`, `pending_reason=waiting for a matching GPU`

Validation:

- Archive is zip or tar.gz, not empty, entrypoint exists inside, suffix `.py`
- `environment_id` in catalog
- resources: positive ints, runtime ≤ global max, `min_vram_mb` ≤ 24576 in v1 unless admin

## Behavior

Validating is synchronous in v1 (archive inspect on the API host). Failures: job never created, 400 with field errors. (Do not leave orphan `submitted` rows.)

Cancel before any attempt: `desired_state=cancel`, derived status `cancelled`.

## Failure modes

- MinIO write fails: transaction abort, 503, no job row.
- Corrupt archive: 400.
- User not in project: 403.

## Implementation steps

1. FastAPI project, Alembic, compose for Postgres+MinIO.
2. Full schema migration.
3. Seed two environments (pytorch, tensorflow) with placeholder digests until M08.
4. Job endpoints + archive inspection.
5. Integration tests against compose.

## Acceptance criteria

- Compose stack starts; `GET /health` is 200.
- Creating a job stores a checksummed object and a `jobs` row.
- GET returns derived `status=queued` and a pending reason.
- Cancel of a queued job yields `cancelled`.
- No log or output bytes in Postgres.
