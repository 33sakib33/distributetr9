# M09 — Environment builder

## Goal

Build a user environment once, reuse by digest, only queue GPU work after CUDA/import tests pass. **Not v1.** Ship after catalog-image training works.

## In scope

- Dedicated build queue (not GPU training nodes, or tagged build nodes)
- Normalize `requirements.txt` / `environment.yml`
- Hash: normalized spec + base image digest + compatibility class
- Cache hit returns existing digest
- Build, CUDA preflight, import test
- Push to campus registry
- Build logs separate from training logs
- Compatibility-specific images (CUDA, PyTorch, Python, GPU arch)
- Exact lockfiles stored

## Out of scope

- Arbitrary Dockerfiles from students in the first iteration of M09
- Non-Docker env archives (the old 6B path) unless Docker is abandoned

## Dependencies

M08, M10 (build log chunks).

## Unlocks

User-specified dependencies. Portal environment-build status.

## Contracts

Job create may send `requirements.txt` instead of `environment_id`. Control API inserts `environment_builds` in `pending`. Job derived status `building` until `ready`, then `queued`.

Hash input: canonical JSON of deps + `base_digest` + `compat_class`.

CUDA preflight: run a short container on a builder that has the target GPU class, or a representative GPU. Image is not `ready` until this passes.

## Behavior

1. Normalize spec (pinned versions where possible).
2. Lookup hash. Ready → attach digest to job.
3. Else build from the matching base image, write lockfile, test, push, store digest.
4. Failed build: job `failed`, category `environment`, logs downloadable.

## Failure modes

- Build uses a GPU node and stalls training: do not. Dedicated builder or off-GPU docker build plus GPU test slot with a small quota.
- Cache poison: only mark ready after tests; never overwrite a digest.

## Implementation steps

1. Schema already exists; wire job create.
2. Builder worker.
3. Portal: build status + separate logs (M11 follow-up).
4. GC policy with M13 image cache quotas.

## Acceptance criteria

- Two jobs with the same hash skip a second build.
- Failed import never reaches `queued` for GPU claim.
- Training logs do not contain pip/build output; build logs are a separate artifact stream.
- `environments` catalog still works without a build row.
