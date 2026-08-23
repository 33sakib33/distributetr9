# M10 — Artifacts and logs

## Goal

Submissions, log chunks, and outputs live in MinIO. Postgres stores only metadata. Promotion happens only after the fencing token is verified. A node can be wiped without losing already-uploaded bytes.

## In scope

- Sequenced, compressed log chunks
- Log rate and size limits
- Output size limits
- SHA-256 for every object
- Attempt-specific keys
- Final promotion to `jobs/{job_id}/promoted/`
- Portal list/download APIs
- SSE live tail from ingested chunks

## Out of scope

- Checkpoint streaming during long jobs (later; design keys now, do not build uploader yet)
- Executed notebooks

## Dependencies

M02. Used by M05 complete path and M07/M08 upload.

## Unlocks

M11 artifact UI; safe retries.

## Contracts

Log chunk object: gzip JSONL, keys as in data model. DB `job_attempts.last_log_seq`.

Limits (v1 starting points, configurable):

| limit | value |
|-------|--------|
| log lines / second / attempt | 200 |
| log bytes / attempt | 32 MiB |
| output bytes / attempt | 5 GiB |
| single output file | 2 GiB |
| chunk lines | 500 |

Over limit: drop log lines with a single warning line; outputs fail the attempt as `resource` without promotion.

Complete payload lists `output_manifest`. Scheduler (M04) calls `artifacts.promote(job_id, attempt_id, token)` which:

1. Re-checks fencing / accepted attempt
2. Verifies each object exists and checksum matches
3. Copies or server-side copies to `promoted/`
4. Inserts artifact rows type=output bound to the job

Agents upload directly to presigned PUT URLs for attempt namespace. They cannot write `promoted/`.

## Behavior

- Logs: agent POSTs to API (small) or presigned PUT per chunk. v1: POST `/attempts/{id}/logs` is enough; API writes MinIO so SSE is easy.
- Duplicate seq range: 200 no-op.
- Gap in seq: 409 `log_gap`; agent resends.

## Failure modes

- Promote before checksum verify: forbidden.
- Node dies after upload, before complete: objects remain in attempt namespace; complete retry promotes; if attempt is fenced, objects stay unpromoted (orphan GC in M13).

## Implementation steps

1. Presign helper and key layout.
2. Log ingest + SSE.
3. Output presign + manifest verify + promote.
4. Limit tests.
5. Orphan listing query for ops.

## Acceptance criteria

- Killing MinIO mid-job does not duplicate seq after retry.
- User download of outputs is 404 until promotion.
- After promotion, deleting the node VHD does not delete MinIO objects.
- Checksums mismatch → complete rejected, job not `completed`.
- Postgres table `artifacts` never contains log text.
