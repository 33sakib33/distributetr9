# Post-pilot plan: specifications and progress

This file is the single source of truth for the work after the first real lab
PC joined (2026-11-01). Part 1 holds the rules every implementer follows, Part 2
the specifications, Part 3 the execution packages, Part 4 the progress list.
**Update Part 4 whenever an item changes state.**

IDs: **B** backend (control plane, agent, deploy), **F** frontend (portal),
**H** hardware or people (not code).

---

## Part 1: Rules for implementers

### Hard rules

1. **Never change `AGENT_VERSION`** in `agent/agent.py`. A real lab PC
   (DESKTOP-5MROQ4T) is connected to the dev server and self-updates when the
   version changes. One release bump happens at the very end.
2. **Backend packages run one at a time.** They share `models.py`,
   `config.py`, `routers/*.py`, the migration chain and the `lab_test` database
   (the test suite drops and recreates it).
3. **Frontend agents never run `next build` or `npm run build`** while the dev
   portal runs (it shares `portal/.next`). Type-check with
   `cd portal && npx tsc --noEmit`. A production build runs once at the end.
4. **Do not commit, push, or start/stop long-running servers** (the dev API on
   :8080, the portal on :3000 and the Docker services stay as they are).
5. **Every behaviour change gets a test** in `tests/`. Bug fixes get a test that
   fails on the old code. The full suite must pass:
   `control/.venv/bin/pytest tests -q -p no:cacheprovider` and
   `control/.venv/bin/ruff check control agent harness tests scripts sim`.
6. **Schema changes go through Alembic.** Take the next free revision number
   in `control/migrations/versions/` (`0004`, `0005`, ...), set
   `down_revision` to the previous one, generate with
   `cd control && ../control/.venv/bin/alembic revision --autogenerate --rev-id 00NN -m "..."`
   against a scratch database (see how 0002/0003 were made), delete the
   "auto generated" comments, and confirm `alembic check` is clean on `lab_test`
   after the suite runs.
7. **The agent stays standard-library only.** No new imports outside the
   Python standard library in `agent/`.
8. **Match the surrounding code**: naming, comment density, error style
   (`AppError(status, code, message)`), small functions, no new frameworks.

### Backend conventions

- Settings live in `control/app/config.py`; every new knob has a default that
  works in dev and is documented in `.env.example`.
- Admin-only endpoints use `Depends(require_admin)`; agent endpoints use
  `Depends(get_current_node)`.
- New admin mutations call `record_audit(...)` once B21 exists (earlier items
  add the call when they are revisited in P5).
- List endpoints return `{"items": [...], "next_cursor": str | null}` with
  `limit` (default 50, max 200) and opaque `cursor`, unless an existing endpoint
  already has a shape (keep existing shapes).
- Timestamps are ISO 8601 strings in UTC.
- Background loops go in `control/app/main.py` next to the lease and alert
  workers, each wrapped so one exception never kills the loop.

### Frontend conventions

- Next.js app router, client components fetching through `api()` in
  `portal/lib/api.ts` (requests go to `/api/...`, proxied to the control plane).
  Add types for every new response in `lib/api.ts`.
- Reuse the existing dark theme and classes in `app/globals.css` (`card`,
  `chip <state>`, `ghost`, `muted`, `mono`, `stack`, `err`). Add CSS there, not
  inline style objects, for anything reused.
- **No new runtime dependencies.** Charts are small hand-written SVG
  components in `portal/components/` (create the folder). Playwright is allowed
  as a dev dependency for F18 only.
- Admin pages live under `app/admin/`; navigation links for admins are added
  to `app/layout.tsx`.
- Every page handles loading, empty and error states.

---

## Part 2: Specifications

### Stage 1: Fix what the real PC showed

**B1. One builder serves every GPU class.**
- `envbuild.claim_build`: remove the class filter; any build-role node may
  claim any pending build.
- `executor.build_custom_image`: run the import test always; assert
  `torch.cuda.is_available()` only when the builder itself has a working
  `nvidia-smi` *and* its compatibility class equals the build's `compat_class`.
  Otherwise log `cuda check skipped: builder has no <class> GPU`.
- Build payload gains nothing; the agent knows its own class.
- Tests: a CPU builder claims a `turing-6g` build and completes it; the job
  becomes claimable by a `turing-6g` trainer.

**B2. Catalog images served from the lab registry ("mirror builds").**
- `environment_builds` gains `kind` (`"pip"` default, `"mirror"`) and
  `environment_id` (nullable FK, the catalog row to update). Environments gain
  `upstream_image` (text, nullable). Migration.
- When `REGISTRY_URL` is set, a startup task (and the lease worker every 10
  minutes) creates one pending mirror build per catalog environment whose
  `image_digest` is not a registry digest on the current registry and that has
  no pending/building mirror build.
- Build payload for a mirror: `{"kind": "mirror", "source_image": ..., "image_tag": "<registry>/lab/<env-name>:<sanitised-tag>"}`.
  The agent pulls the source, tags, pushes, and returns the RepoDigest (no pip,
  no import test).
- On a successful mirror complete: set `environments.upstream_image` (if
  empty) to the old value and `image_digest` to the registry digest. Failed
  mirrors leave the catalog unchanged and log the error.
- The existing stale-registry logic (`on_current_registry`) must not reset
  catalog environments; for them a new mirror build is created instead.
- Tests: with a fake builder, a mirror build is created for each catalog env,
  completing it rewrites `image_digest`; jobs then receive the digest.

**B3. Agents prune their own image cache.**
- Heartbeat response gains `disk_watermark_bytes`.
- The agent records `last_used` per image reference in its state file whenever
  a job or build uses it.
- Before claiming, if `disk_free_bytes < 2 * disk_watermark_bytes`, remove
  images LRU-first until above `3 * disk_watermark_bytes` or none left:
  only images whose repository contains `/lab-env/` or `/lab/`, or that are
  catalog images; never an image used by a running container. Log each removal.
- Tests: unit test of the selection function (pure function over a list of
  images, usage map, running set).

**B4. Real TensorFlow image and a CPU catalog image.**
- New catalog row `python-cpu` (fixed UUID `33333333-...`), image
  `python:3.12.7-slim`, `gpu_archs = ["cpu"]`.
- `tensorflow-stable` becomes `tensorflow/tensorflow:2.17.0-gpu`, archs
  `8.9, 8.6, 7.5, cpu`. Seed updates an existing row only if its image is still
  the old placeholder `python:3.12.7-slim`.
- `sim/exam.py` and `scripts/smoke.py` (hello job) use `python-cpu`.
- Tests: seed produces three catalog rows; existing placeholder row upgraded.

**B5. Agent reports why it last restarted.**
- Launcher writes `/opt/lab-agent/last_exit` (`<exit-code> <unix-ts>`) after
  every agent exit.
- Agent: wraps `run_forever` so an uncaught exception writes the last 40
  traceback lines to `<state dir>/last_crash.txt` before exiting; records the
  kernel boot id (`/proc/sys/kernel/random/boot_id`) in its state file.
- On register it sends `last_restart: {"reason": "first_start|update|crash|reboot|manual", "exit_code": int|null, "detail": str|null, "at": iso|null}`:
  reboot if the boot id changed; update if the previous exit code was 75;
  crash if `last_crash.txt` is newer than the last register (then deletes it);
  manual otherwise.
- Server stores `nodes.last_restart` (JSONB) and appends it to the node's
  history (B8 table, or a `node_events` table if B8 is not yet built). Migration.
- `/nodes` includes `last_restart`.
- Tests: reason decision is a pure function, unit-tested for every case;
  register stores the value.

### Stage 2: Telemetry

**B6. Agent samples job usage.**
- Every renew, the agent collects: `gpu_pct`, `vram_mb` (`nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader,nounits`),
  `cpu_pct`, `ram_mb` (`docker stats --no-stream --format '{{json .}}' <container>`).
  Missing tools give `null` fields, never an error.
- Sent as `usage: {...}` in the renew body (keep the existing optional
  `resource_usage` field untouched; it holds log counters server-side).

**B7. Store usage.**
- Table `attempt_usage(id, attempt_id FK, at, gpu_pct, vram_mb, cpu_pct, ram_mb)`,
  index `(attempt_id, at)`. Attempts gain `peak_vram_mb`, `peak_ram_mb`,
  `avg_gpu_pct` (updated on each sample). Migration.
- After an attempt ends, keep at most 300 samples (evenly thinned) by the
  lease worker.
- `GET /jobs/{id}/usage` → `{"attempt_id", "samples": [{at, gpu_pct, vram_mb, cpu_pct, ram_mb}], "peak_vram_mb", "peak_ram_mb", "avg_gpu_pct", "vram_total_mb"}` for the latest attempt.
- `serialize_attempt` includes the three peak fields.
- Tests: renew with usage stores samples and peaks; thinning keeps ≤300.

**B8. Node health.**
- Heartbeat body gains optional `health: {gpu_temp_c, gpu_pct, vram_used_mb, driver_version, uptime_s, docker_ok}`.
  Agent fills it (nvidia-smi `temperature.gpu`, `/proc/uptime`, `docker info` ok).
- Table `node_health(id, node_id, at, gpu_temp_c, gpu_pct, disk_free_bytes, busy bool)`;
  at most one row per node per 60 s; rows older than 7 days pruned by the lease
  worker. Migration.
- `GET /nodes/{id}` (admin) → node fields + `health_history` (last 24 h) +
  `recent_attempts` (last 20 with job id, state, times) + `last_restart`.
- Tests: throttling, pruning, detail endpoint.

**B9. Job stages with progress.**
- Attempts gain `stages` (JSONB):
  `{"<name>": {"started_at", "finished_at", "progress": {"done": int, "total": int, "unit": str}|null}}`.
  Names: `pulling_image`, `building_environment` (server-side, from the build),
  `running`, `uploading`. Migration.
- Agent reports stage changes in renew bodies: `stage: {"name", "state": "started|finished", "progress": {...}|null}`.
  During `docker pull` it parses lines ending in `Pull complete` /
  `Already exists` against the layer count to report `{done, total, unit: "layers"}`.
- `GET /jobs/{id}` includes `stages` for the latest attempt and, while
  building, `building_environment` from the build row.
- Tests: renew with stage events builds the map; job API exposes it.

**B10. Prometheus metrics.**
- Dependency `prometheus-client` (pin a version) in `control/requirements.txt`.
- `GET /metrics`, text format. If `METRICS_TOKEN` is set, require
  `Authorization: Bearer <token>`.
- Gauges computed at scrape time: `lab_queue_jobs{gpu_class}`, `lab_nodes{state}`,
  `lab_running_attempts`, `lab_builds{state}`.
- Counters incremented in code: `lab_jobs_finished_total{outcome}`,
  `lab_lease_lost_total`, `lab_claims_total{result=job|build|empty}`.
- Histograms: `lab_time_to_start_seconds`, `lab_image_pull_seconds` (from B9),
  `lab_job_runtime_seconds`.
- Tests: endpoint returns the metric names; token enforced.

**B11. Prometheus + Grafana in production.**
- `deploy/docker-compose.prod.yml` adds `prometheus` (scrapes
  `control:8080/metrics` with the token, 30-day retention) and `grafana`
  (admin password from `GRAFANA_ADMIN_PASSWORD`, provisioned datasource and
  dashboard from `deploy/grafana/`), Grafana served by Caddy at
  `{$GRAFANA_HOST}`. `prod.env.example` documents the new variables.
- Dashboard panels: queue per class, nodes by state, running jobs, time to
  start p50/p95, lease losses, job outcomes, image pull time.
- Validation: `docker compose ... config -q` passes; the dashboard JSON loads
  (validated by a small test that parses it and checks panel queries name real
  metrics).

**B12. Usage reports.**
- `GET /admin/usage?from=&to=&group=user|project|class` →
  `{"rows": [{"key", "label", "gpu_hours", "jobs", "succeeded", "failed"}], "lab": {"online_hours", "busy_hours", "utilisation"}}`.
  GPU-hours: sum of attempt run durations for jobs with `min_vram_mb > 0`;
  online hours from `node_health` sample counts; busy from attempt durations.
- `GET /me/usage?from=&to=` → the same row shape for the caller, grouped by project.
- `?format=csv` on both returns CSV.
- Tests: fixtures with known attempts give known totals.

**B13. Agent logs on demand.**
- The agent mirrors its stdout lines into `<state dir>/agent.log`
  (rotating at 2 MB, keep 1 old file).
- `POST /nodes/{id}/logs/request` (admin) sets a pending request on the node;
  the heartbeat response gains `requests: ["upload_logs"]` once.
- Agent then `POST /agent/logs-upload` (text body, last 500 lines) → stored in
  MinIO `nodes/<id>/agent-log-<ts>.txt`.
- `GET /nodes/{id}/logs` (admin) → `{"requested_at", "uploaded_at", "text"}`.
- Tests: request → heartbeat carries it → upload → GET returns text.

### Stage 3: Admin essentials

**B14. Admin jobs API.**
- `GET /admin/jobs` with filters `status, user (email substring), project_id, gpu_class, node (hostname), since, until`, paging per conventions;
  items are `serialize_job` plus `user_email`, `project_name`.
- Admins may cancel any job via the existing `POST /jobs/{id}/cancel`.
- `POST /admin/jobs/{id}/requeue`: a failed or cancelled job becomes runnable
  again (`desired_state=run`, `accepted_attempt_id=null`, `max_attempts`
  raised to `latest attempt_number + 3`, pending reason "requeued by admin").
- `POST /admin/jobs/cancel` with `{"job_ids": [...]}` (max 200).
- Tests for each.

**B15. Node lifecycle and alert hygiene.**
- Nodes gain `decommissioned_at`. `POST /nodes/{id}/forget` revokes the
  credential, fails active attempts (as quarantine does) and sets it.
  Decommissioned nodes are excluded from `/nodes` (unless
  `?include_decommissioned=1`), scheduling, alerts and metrics.
- Table `alerts(id, key unique, kind, severity, message, first_seen, last_seen, resolved_at, acknowledged_at, acknowledged_by, muted_until)`.
  `kind` is the key prefix (`offline`, `disk`, `failures`, `lease_losses`,
  `queue_age`). The alert worker upserts current alerts, sets `resolved_at` on
  ones no longer present, and re-opens resolved keys that come back.
  The notifier sends only new, unmuted, unacknowledged alerts.
- `GET /admin/alerts` (replace the current shape) →
  `{"groups": [{"kind", "count", "severity", "alerts": [...]}]}` (open only;
  `?include_resolved=1` adds recent resolved).
  `POST /admin/alerts/{id}/ack`, `POST /admin/alerts/{id}/mute` `{"minutes"}`,
  `POST /admin/alerts/ack-kind` `{"kind"}`.
- Migration. Tests: forget, alert lifecycle, grouping, notifier filtering.

**B16. Rooms and labels.**
- Nodes gain `room` (text) and `labels` (JSONB list of strings).
- Agent config key `room` and installer flag `--room` (`setup-lab-node.sh`
  writes it into `/etc/lab-agent/config.json`; `bootstrap.ps1 -Room`). Sent at
  register; applied only when the node's room is empty or was set by the agent
  (track `room_source = agent|admin`).
- `POST /nodes/{id}/meta` `{"room", "labels"}` (admin, sets source admin).
- `/nodes?room=` filter; `/nodes` items include room and labels.
- Migration. Tests.

### Stage 4: Lab policy and people

**B17. Availability schedules ("class hours").**
- Setting `LAB_TIMEZONE` (default `UTC`).
- Table `availability_windows(id, room nullable, node_id nullable, weekday 0-6 Mon=0, start_minute, end_minute, note)`;
  exactly one of room/node_id set. Migration.
- `node_can_claim` returns `"schedule"` while the node (or its room) is inside
  a window. In `claim`, a job is not started on a node if
  `now + max_runtime_seconds` reaches the start of the node's next window
  within 24 h (skip that job for that node).
- Pending reason when every fitting node is blocked by a schedule:
  `lab reserved for teaching until HH:MM`.
- CRUD: `GET/POST /admin/schedules`, `PATCH/DELETE /admin/schedules/{id}`.
- Tests with a frozen clock (inject "now"), including windows across midnight
  being rejected (end must be after start; split into two windows instead).

**B18. People and quotas.**
- `GET /projects/{id}/members`, `PATCH /projects/{id}/members/{user_id}` `{"role": "owner|member"}`,
  `DELETE /projects/{id}/members/{user_id}` (owners and admins; cannot remove
  the last owner).
- `GET /admin/users?q=` (paged), `POST /admin/users/{id}/block` and
  `/unblock` using the existing `users.disabled_at`; blocked users get 403
  on every user endpoint.
- `GET /admin/quotas?subject_type=&subject_id=`, `PUT /admin/quotas/{subject_type}/{subject_id}` `{"max_concurrent_jobs"}`.
- Tests.

**B19. Catalog management.**
- Environments gain `retired_at`. Migration.
- `POST /admin/environments`, `PATCH /admin/environments/{id}` (name,
  framework, python_version, cuda_version, gpu_archs, image_digest,
  retired true/false). Retired environments are hidden from `/environments`
  and rejected on submit; existing jobs keep running.
- `GET /admin/environments` includes retired ones and a `jobs_last_30d` count.
- Tests.

**B20. Rollout settings in the database.**
- Table `platform_settings(key primary, value JSONB, updated_at, updated_by)`.
  Migration. Keys: `agent_target_version`, `agent_rollout_ring`,
  `agent_min_version`. Environment variables are the defaults when no row
  exists. Values are cached for 10 s in-process.
- `GET /admin/rollout` → `{"target_version", "bundle_version", "rollout_ring", "min_version", "nodes_by_version": {...}, "rejected": {...}}`
  (rejected from agents' reports if available, else omitted).
  `PUT /admin/rollout` `{"target_version"?, "rollout_ring"?, "min_version"?}`.
- `update_for` / `is_outdated` read the cached settings.
- Tests.

**B21. Audit log.**
- Table `audit_log(id, at, actor_user_id, actor_email, action, target_type, target_id, details JSONB)`, index on `at`. Migration.
- Helper `record_audit(session, actor, action, target_type, target_id, details=None)`.
  Called by every admin mutation: drain, quarantine, release, revoke, forget,
  ring, meta, requeue, bulk cancel, admin cancel of others' jobs, alert
  ack/mute, schedules, quotas, block/unblock, member changes, catalog, rollout.
- `GET /admin/audit?action=&actor=&target_type=&since=` (paged).
- Tests: a drain produces an audit row.

### Stage 5: Student experience

**B22. Flexible upload.**
- `POST /jobs` accepts a single `.py` file (wrapped server-side into a
  tar.gz; entrypoint defaults to its name), `.zip`, `.tar.gz`/`.tgz`/`.tar`.
- `POST /jobs/inspect` (multipart `archive`) →
  `{"entrypoints": ["train.py", "src/main.py", ...], "suggested_entrypoint", "requirements": "<text>|null"}`
  (`requirements.txt` at the archive root or next to the suggested entrypoint).
- Tests for each archive type.

**B23. Availability and wait estimate.**
- `GET /availability` (any user) →
  `{"classes": [{"gpu_class", "vram_mb", "nodes_total", "nodes_online", "nodes_free", "queued", "est_wait_seconds"}]}`
  for non-decommissioned classes. `est_wait_seconds`: 0 if a free node exists,
  else `ceil((queued + 1) / max(nodes_online, 1)) * median_runtime_last_7d`
  (fallback 600 s), capped at 24 h, `null` if no node of that class is online.
- Tests.

**B24. Queue position.**
- `GET /jobs/{id}` includes `queue_position` (1-based among queued, runnable
  jobs with the same `gpu_class`, by priority then age) and `est_start_seconds`
  (from B23 for its class), both `null` when not queued.
- Tests.

**B25. Failure explanations.**
- `explain_failure(attempt, log_tail) -> {"title", "advice"} | None`, in its own
  module, table-driven: CUDA out of memory, killed (exit 137), timeout,
  `ModuleNotFoundError` (suggest adding to requirements), `FileNotFoundError`
  for data paths, environment build failed, lease lost / node lost (retried
  automatically), cancelled.
- `GET /jobs/{id}` includes `failure_explanation` for failed jobs.
- Tests per pattern.

**B26. Notifications.**
- Settings `SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM,
  SMTP_STARTTLS`; disabled when `SMTP_HOST` is empty.
- Users gain `notify_email` (bool, default true) and `notify_webhook_url`
  (text). Jobs gain `notified_at`. Migration.
- Worker every 15 s: terminal jobs with `notified_at` null and completed in the
  last 24 h → send email (and POST JSON to the webhook if set), then set
  `notified_at`. Failures are logged and retried next loop at most 3 times.
- `GET/PUT /me/notifications` `{"notify_email", "notify_webhook_url"}`.
- Tests with a fake SMTP sender (dependency-inject the sender).

**B27. Clone a job.** `POST /jobs/{id}/clone` `{"resources"?, "args"?, "entrypoint"?, "environment_id"?}`
→ a new job owned by the caller (must be a member of the project) reusing the
submission archive (copied object) and requirements/build; returns the new job.
Tests.

**B28. Outputs as one zip.** `GET /jobs/{id}/outputs.zip` streams a zip of the
promoted outputs (paths relative to `promoted/`); 404 when there are none. Tests.

### Stage 6: Hardening

**B29. Rate limits.**
- In-process token buckets keyed by user (or IP when anonymous):
  `POST /jobs` 30/min, `POST /auth/local` and `/auth/login` 10/min,
  `POST /jobs/inspect` 30/min; configurable; 429 with `Retry-After`.
- Caddy: `request_body max_size` on the API host = `MAX_UPLOAD_BYTES` + margin.
- Tests.

**B30. Structured logs and request IDs.**
- Middleware: accept `X-Request-ID` or generate one; echo it in responses;
  log one JSON line per request (`ts, request_id, method, path, status,
  duration_ms, user, node`). Setting `LOG_FORMAT=json|text` (default text in
  dev, json in production compose).
- Portal proxy forwards `X-Request-ID` (generates one if absent).
- Tests: header echoed; log record contains fields.

**B31. Load test at fleet size.**
- Extend `harness` to report p50/p95/max claim latency, completed jobs/s and
  error counts; run 100 agents / 500 jobs / 60 s against the simulated control
  plane (`http://127.0.0.1:8090`, started with `make sim`), record the numbers
  in `PROGRESS.md`. Fix any defect found (with a test).

**B32. Client error reporting endpoint.** `POST /client-errors`
`{"message", "stack", "url", "user_agent"}` (any user, rate limited 20/min),
logged as structured log lines and counted in a metric. Tests.

### Frontend specifications

**F1. Job page: resource graphs and stage timeline.** Uses B7, B9.
`components/LineChart.tsx` (SVG, multiple series, axis labels, no deps).
Job page shows GPU %, VRAM (with total), CPU %, RAM over time, refreshing while
running; a "peak VRAM 3.1 / 6.0 GB" line with a hint when peak is far below the
request; a stage bar (`components/StageBar.tsx`) showing queued → building →
pulling image (with progress) → running → uploading → done.

**F2. Node detail page** `app/admin/nodes/[id]/page.tsx`. Uses B8, B5, B13.
Health (temperature, GPU %, disk, uptime) with 24 h charts, last restart reason,
recent attempts table linking to jobs, "Fetch logs" button that requests and
then polls `GET /nodes/{id}/logs`, showing the text in a scrollable pre.
Linked from every node row.

**F3. Usage pages.** `app/usage/page.tsx` ("My usage", B12 `/me/usage`) and
`app/admin/usage/page.tsx` (group by user/project/class, date range, lab
utilisation headline, CSV download link).

**F4. Lab dashboard** `app/admin/page.tsx` becomes the dashboard; the current
node table moves to `app/admin/nodes/page.tsx`. Dashboard: headline counts
(online, busy, idle, offline, quarantined, updating), a grid of PC tiles
grouped by room (B16) coloured by state with GPU % and job link, queue length
per class, open alerts summary (B15). Auto-refresh every 5 s.

**F5. Admin jobs view** `app/admin/jobs/page.tsx` (B14): filters, paging,
multi-select bulk cancel, requeue on failed/cancelled rows.

**F6. Alerts panel** `app/admin/alerts/page.tsx` + dashboard summary (B15):
grouped alerts, acknowledge, mute (15 min / 1 h / 1 day), acknowledge a whole
kind; "Forget node" action on offline-node alerts and in the node table.

**F7. Schedule editor** `app/admin/schedules/page.tsx` (B17): weekly grid per
room/node, add/edit/delete windows, timezone shown.

**F8. People and projects.** `app/projects/page.tsx` (my projects, create),
`app/projects/[id]/page.tsx` (members, roles, add by email, remove; quota shown;
owners edit), `app/admin/users/page.tsx` (search, block/unblock, quotas).

**F9. Catalog page** `app/admin/catalog/page.tsx` (B19): list incl. retired,
create/edit form, retire/restore, jobs in 30 days.

**F10. Rollout page** `app/admin/rollout/page.tsx` (B20): versions across the
fleet, set target version / ring / minimum version, ring selector per node
(link to node table).

**F11. Audit log page** `app/admin/audit/page.tsx` (B21): filterable, paged.

**F12. New submit flow** (B22, B23): drag-and-drop zone accepting a `.py`,
`.zip`, `.tar.gz` or a folder (zipped client-side with a tiny store-only zip
writer in `lib/zip.ts`, no dependency); calls `/jobs/inspect` and pre-fills the
entrypoint picker and packages box; GPU choices as cards from `/availability`
(class, memory, free now, estimated wait) plus a "CPU only" card; advanced
section for runtime, CPU, RAM, disk.

**F13. Waiting and progress view** (B24, B9): queue position and estimated
start while queued; stage bar from F1 on the job page.

**F14. Failure panel** (B25): explanation card above the logs for failed jobs.

**F15. Job list upgrades**: search by script/status, status filter, clone
button (B27, opens submit pre-filled), "Download all outputs" (B28) on the job
page.

**F16. Notification settings** `app/settings/page.tsx` (B26) plus browser
notifications (Notification API, opt-in) for the user's jobs while a portal tab
is open.

**F17. Polish**: consistent loading/empty/error states on every page, mobile
layout (tables scroll horizontally, nav collapses), focus styles and labels for
keyboard and screen readers, a light theme via `prefers-color-scheme`.

**F18. End-to-end tests** (Playwright, dev dependency): login, submit a
`python-cpu` job and see it complete (needs a running stack with a local
agent), cancel, admin drain/release; runnable with `npm run test:e2e`; a CI job
in `.github/workflows/tests.yml` that starts the stack with `scripts/dev.sh`.

**F19. Frontend error reporting** (B32): global error boundary and
`window.onerror`/`unhandledrejection` hooks posting to `/api/client-errors`;
API errors shown with their message and request ID.

---

## Part 3: Execution packages

Backend packages run strictly in order. Each frontend package starts once the
backend package it depends on has landed, and runs in parallel with the next
backend package.

| Package | Items | Depends on | Notes |
|---|---|---|---|
| P1 | B1–B5 | — | also fix `make sim-down` to remove only the sim services; then the simulated exam (agent changed) |
| P2 | B6–B9 | P1 | agent + server telemetry |
| P3 | B10–B13, X1, X2 | P2 | metrics, Grafana, usage, logs on demand; X1: pending reasons ignore offline and decommissioned nodes; X2: sim reliability (fresh `lab_sim` per `make sim`, stale dockerd pid cleanup on restart) |
| FA | F1–F3 | P3 | |
| P4 | B14–B16 | P3 | |
| FB | F4–F6 | P4 | |
| P5 | B17–B21 | P4 | also adds `record_audit` calls to earlier admin endpoints |
| FC | F7–F11 | P5 | |
| P6 | B22–B28, X3, X4 | P5 | X3: `GET /nodes` items include `gpu_pct`, `gpu_temp_c`, `busy`, `current_attempt` (`{attempt_id, job_id, state}` or null) so the dashboard needs one request; X4: `GET /admin/projects` (paged, `q` filter) for the admin jobs project filter. FD switches the dashboard and jobs filter to them. |
| FD | F12–F17 | P6 | |
| P7 | B29–B32, X5, X6 | P6 | X5: notification webhooks refuse loopback/private/link-local/metadata addresses (resolved at send time) unless `NOTIFY_ALLOW_PRIVATE_WEBHOOKS=1`; X6: build tests independent of leftover pending builds |
| FE | F18–F19 | P7 | |
| R | release | all | bump agent version once, full test suite, lint, `alembic check`, portal production build, simulated exam, docs (README, PROGRESS, runbooks, guide) |

---

## Part 4: Progress

Status: `[ ]` todo, `[~]` in progress, `[x]` done and verified (tests pass).
Each line gets a short note when it lands: what was built, test names, anything
deferred.

### Backend
- [x] B1 One builder serves every GPU class: `claim_build` has no class filter; `should_check_cuda` gates the CUDA assert (`test_cpu_builder_serves_any_gpu_class`, `test_cuda_check_needs_a_matching_gpu_on_the_builder`, `test_build_without_the_gpu_skips_the_cuda_check`)
- [x] B2 Catalog images from the lab registry (mirror builds): migration 0004, `ensure_mirror_builds` at startup and every 10 min, agent `mirror_image`, `MIRROR_CATALOG_IMAGES` knob (`tests/test_mirror.py`: `test_mirror_build_per_catalog_env_rewrites_digest`, `test_registry_move_requeues_mirror_without_resetting_catalog`)
- [x] B3 Agent image cache pruning: heartbeat `disk_watermark_bytes`, `image_last_used` in agent state, `select_images_to_prune` + `prune_images` (`tests/test_image_prune.py`)
- [x] B4 Real TensorFlow image + python-cpu catalog image: seed adds `python-cpu`, upgrades the TF placeholder; exam and smoke use `python-cpu` (`test_seed_has_three_catalog_rows`, `test_seed_upgrades_only_the_tensorflow_placeholder`)
- [x] B5 Agent restart reason: launcher `last_exit`, `last_crash.txt`, `decide_restart`, `nodes.last_restart` + `node_events` (migration 0004), `/nodes` shows it (`tests/test_restart_reason.py`)
- [x] B6 Agent usage sampling: `Telemetry` sampler thread in the agent (nvidia-smi, docker stats, 5 s timeouts, null fields when a tool is missing); renew bodies carry `usage` (`tests/test_telemetry.py`: sampler parsing, missing tools, short timeouts)
- [x] B7 Usage storage + `/jobs/{id}/usage`: migration 0005 (`attempt_usage`, peak/avg columns), peaks in `serialize_attempt`, lease worker thins finished attempts to 300 samples every 5 min (`test_renew_with_usage_stores_samples_and_peaks`, `test_finished_attempts_keep_at_most_300_samples_and_running_ones_are_left_alone`)
- [x] B8 Node health + node detail API: heartbeat `health`, `node_health` throttled to 1/min and pruned after 7 days, `GET /nodes/{id}` with health_history, recent_attempts, last_restart, events (`test_health_samples_are_throttled_to_one_a_minute`, `test_health_older_than_a_week_is_pruned`, `test_node_detail_has_health_history_attempts_and_restart`)
- [x] B9 Job stages with progress: `job_attempts.stages`, renew `stage`/`stages`, agent reports `pulling_image` layer progress (renew thread already runs during the pull; a stage change renews at once), server closes stages at uploading/finish, `GET /jobs/{id}` adds `stages` + `building_environment` (`test_renew_with_stage_events_builds_the_stage_map`, `test_building_environment_stage_comes_from_the_build_row`, `test_renew_runs_during_the_pull_and_reports_stages_and_usage`)
- [x] B10 Prometheus `/metrics`: `app/metrics.py` + `routers/reports.py`, `prometheus-client==0.21.1`, optional `METRICS_TOKEN` bearer; gauges computed at scrape, counters and histograms held back until the transaction commits (`tests/test_metrics.py`)
- [x] B11 Prometheus + Grafana in production: prod compose services (token file written by the start command), `deploy/prometheus/`, `deploy/grafana/` (datasource, dashboard of 11 panels), Caddy site for `GRAFANA_HOST` (and `/metrics` blocked on the API host); `compose config -q`, `caddy validate`, `promtool check config` pass; `test_every_dashboard_query_names_a_metric_the_endpoint_serves`
- [x] B12 Usage reports API: `app/usage.py`, `GET /admin/usage` (user/project/class + lab utilisation), `GET /me/usage`, `?format=csv` (formula-safe cells); attempts clipped to the window (`tests/test_usage.py`)
- [x] B13 Agent logs on demand: migration 0006 (node log request columns), agent `LogTee`/`LogFile` (2 MB rotation) mirrors stdout/stderr, heartbeat `requests`, `POST /agent/logs-upload`, `POST /nodes/{id}/logs/request`, `GET /nodes/{id}/logs`; unanswered requests resend after 2 min (`tests/test_agent_logs.py`)
- [x] B14 Admin jobs API: `routers/admin.py` `GET /admin/jobs` (filters, keyset paging via `app/paging.py`, status derived in Python and scanned in batches), `POST /admin/jobs/{id}/requeue`, `POST /admin/jobs/cancel`; admins cancel any job through `/jobs/{id}/cancel`; `derived_status` shows a requeued cancelled job as queued (`tests/test_admin_jobs.py`)
- [x] B15 Node forget + alert lifecycle: migration 0007 (`alerts`, `nodes.decommissioned_at`), `POST /nodes/{id}/forget`, decommissioned nodes out of `/nodes` (`include_decommissioned=1`), scheduling, alerts, metrics; `GET /admin/alerts` now `{groups}` with ack / mute / ack-kind; worker and notifier run on the table (`ALERT_CHECK_SECONDS`, waits before its first pass) (`tests/test_alerts.py`, `tests/test_node_lifecycle.py`)
- [x] B16 Rooms and labels: `nodes.room`, `room_source`, `labels` (0007), agent config key `room` sent at register, installer `--room` (kept on re-runs) and `bootstrap.ps1 -Room`, `POST /nodes/{id}/meta`, `/nodes?room=` plus `rooms` list (`tests/test_rooms.py`)
- [x] B17 Availability schedules: migration 0008 `availability_windows`, `app/schedule.py` (`clock()` is the injectable now; `Schedule` answers blocked / until / would-collide), `node_can_claim(..., schedule)` returns `schedule`, `claim` skips jobs whose max runtime reaches the next window within 24 h, pending reasons "lab reserved for teaching until HH:MM" / "... from HH:MM; lower the max runtime...", CRUD `/admin/schedules`, `LAB_TIMEZONE` (`tests/test_schedules.py`)
- [x] B18 People and quotas API: `routers/people.py` members list / role / remove with last-owner 409, `/admin/users` (search, `blocked` filter, paged) + block / unblock (existing `disabled_at` gate gives 403; `/environments` now needs a sign-in), `/admin/quotas` GET + PUT (`tests/test_people.py`)
- [x] B19 Catalog management API: `environments.retired_at` (0008), `routers/lab.py` `/admin/environments` GET/POST/PATCH, retired hidden from `/environments`, refused on submit, skipped by mirror builds (`tests/test_catalog_admin.py`, `test_a_retired_catalog_image_is_not_mirrored`)
- [x] B20 Rollout settings in the database: `platform_settings` (0008), `app/platform_settings.py` (10 s cache, cleared by PUT and `clear_cache()`), `agent_bundle.update_for` / `is_outdated` / `target_version` and the heartbeat, claim and `/nodes` paths read it, env vars stay the defaults, `GET/PUT /admin/rollout` (`tests/test_rollout.py`)
- [x] B21 Audit log: `audit_log` (0008), `app/audit.py` `record_audit` (same transaction as the change), called by every admin mutation incl. earlier ones (node drain/quarantine/release/revoke/forget/ring/meta/logs request, requeue, bulk cancel, admin cancel of others' jobs, alerts) and member changes by owners, `GET /admin/audit` (`tests/test_audit.py`, one test per action family)
- [x] B22 Flexible upload + `POST /jobs/inspect`: `.py` wrapped to tar.gz, `.zip`/`.tar`/`.tgz` stored as sent (agent already extracts zip and tar), entrypoint optional (script name, else likeliest script), unsafe member paths refused, `app/archive.py` + `app/jobspec.py` (`tests/test_upload.py`)
- [x] B23 `GET /availability`: per class total / online / free PCs, queued, `est_wait_seconds` (formula in spec; online = fresh heartbeat and eligible; free also passes `node_can_claim`; median of last 7 days, fallback 600 s) (`app/availability.py`, `tests/test_student_view.py`)
- [x] B24 `queue_position` + `est_start_seconds` on `GET /jobs/{id}` (null unless queued; PCs free now take the jobs ahead first) (`test_queue_position_follows_priority_then_age`, `test_a_queued_job_gets_an_estimated_start`)
- [x] B25 `app/failure_explain.py` rule table (cancelled, timeout, node lost, build failed, environment, CUDA OOM, killed/137, missing module, missing file, outputs too large, generic script error); `failure_explanation` on failed jobs, read from the last 40 log lines (`tests/test_failure_explain.py`)
- [x] B26 Notifications: migration 0009 (`users.notify_email/notify_webhook_url`, `jobs.notified_at/notify_tries`), `SMTP_*` knobs, `app/notify.py` `JobNotifier` (injected senders, worker every 15 s, 3 tries), `GET/PUT /me/notifications` (`tests/test_notifications.py`)
- [x] B27 `POST /jobs/{id}/clone` (copies the upload object, reuses the environment/build; resources merged key by key; args, entrypoint, catalog environment can change) (`test_a_clone_*`)
- [x] B28 `GET /jobs/{id}/outputs.zip`: zip built into a spooled temp file from streamed objects, then streamed with Content-Length; 404 without outputs (`test_outputs_come_as_one_zip_with_relative_paths`)
- [x] B29 Rate limits: `app/ratelimit.py` token buckets (burst = the per-minute limit, refill evenly), `RATE_LIMIT_*` settings, 429 + `Retry-After` on `POST /jobs`, `/jobs/inspect` (per user), `/auth/local` + `/auth/login` (shared, per IP), `/client-errors`; Caddy `request_body max_size` 600MB on the API host (2200MB for `/attempts/*/outputs/*`); `caddy validate` passes; tests off in `conftest.py` (`tests/test_ratelimit.py`)
- [x] B30 Structured logs + request IDs: `app/observability.py` pure-ASGI middleware (echoes or makes `X-Request-ID`, one `lab.request` line with ts/request_id/method/path/status/duration_ms/user/node), `LOG_FORMAT=json|text` (json in prod compose), portal proxy forwards and returns the id (`tests/test_observability.py`)
- [x] B31 Load test at fleet size: `python -m harness` runs one thread per agent and reports claim p50/p95/max, jobs/s and errors; 100 agents / 500 jobs on the sim control plane: 500 done in 61 s, claim p50 24 ms / p95 734 ms / max 2.2 s, 0 errors, no defect found (numbers in `PROGRESS.md`; `tests/test_harness_report.py`)
- [x] B32 Client error reporting endpoint: `POST /client-errors` (202, anyone, 20/min per user or IP, long values cut), `lab.client` warning line with the fields, counter `lab_client_errors_total` (`tests/test_observability.py`)
- [x] X1 Pending reasons ignore offline/decommissioned nodes: `is_online` filter in `explain_pending` (`test_stale_duplicate_node_does_not_change_the_arch_reason`, `test_waiting_for_the_class_when_every_node_of_it_is_offline`)
- [x] X2 Sim reliability: `SIM_FRESH_DB=1` drops and recreates `lab_sim`; node entry removes stale dockerd/containerd pid files and restarts dockerd once after 60 s; two exams in a row 13/13
- [x] X3 `/nodes` items carry `gpu_pct`, `gpu_temp_c` (newest health sample, null when older than 5 min), `busy`, `current_attempt`; two extra queries for any fleet size (`test_nodes_show_load_temperature_and_the_job_they_run`, `test_listing_nodes_needs_the_same_number_of_queries_for_any_fleet`)
- [x] X4 `GET /admin/projects` (paged by name, `q` filter; id, name, created_by_email, members, max_concurrent_jobs) in `routers/people.py` (`tests/test_student_view.py`)
- [x] X5 Webhook SSRF protection: `post_webhook` resolves at send time, refuses loopback/private/link-local/multicast/unspecified/reserved (any one bad record refuses), connects to the checked address (Host and SNI kept), never follows redirects; `PUT /me/notifications` rejects literal internal IPs (incl. 127.1 and decimal forms) and `localhost` with 422; `NOTIFY_ALLOW_PRIVATE_WEBHOOKS=1` opts out (`tests/test_webhook_safety.py`)
- [x] X6 Build tests independent of leftover pending builds: autouse fixture `no_leftover_builds` in `conftest.py` fails unfinished builds before and after every test; the stage test asserts it claimed its own build (`tests/test_build_isolation.py`); suite green 5 runs in a row

### Frontend
- [x] F1 Job page graphs + stage timeline: stage bar from `stages` (queued, building if any, pulling with layer progress, running, uploading, done/failed/cancelled), GPU/VRAM/CPU/RAM charts polled every 5 s while the job is live, peak VRAM line with an oversized-request hint; `tsc` clean
- [x] F2 Node detail page: `app/admin/nodes/[id]` with health headline, 24 h charts, restart reason text, recent attempts, events, fetch-logs button (built against the B13 spec); hostnames in the Nodes table link to it
- [x] F3 Usage pages: `/usage` (my usage, `/me/usage`) and `/admin/usage` (group, dates, lab utilisation, CSV link), nav links "Usage" and "Lab usage"; built against the B12 spec
- [x] F4 Lab dashboard: `app/admin/page.tsx` (headline counts, room tiles with GPU % and job link, waiting jobs per class from `/admin/jobs`, alerts summary, 5 s refresh); node table moved to `app/admin/nodes/page.tsx` with Forget and room/labels editing
- [x] F5 Admin jobs view: `app/admin/jobs/page.tsx` (filters read from the query string, cursor paging, bulk and per-row cancel, requeue on failed/cancelled)
- [x] F6 Alerts panel: `app/admin/alerts/page.tsx` (grouped, acknowledge, mute 15 min/1 h/1 day, acknowledge a kind, show resolved, Forget node on offline alerts); admin nav now Dashboard, Nodes, Jobs, Alerts, Lab usage
- [x] F7 Schedule editor: `app/admin/schedules/page.tsx` (Mon-Sun by hour grid per room or PC, click a block to edit, add one window per ticked day, delete, timezone shown, end-after-start validated client side); `tsc` clean
- [x] F8 People and projects pages: `app/projects/page.tsx` (list, create), `app/projects/[id]/page.tsx` (members, role select, add by email, remove, quota; edit controls only when `can_edit`; admins can set the project quota), `app/admin/users/page.tsx` (search, blocked filter, block/unblock, per-user quota, paging); nav gains Projects and Users
- [x] F9 Catalog page: `app/admin/catalog/page.tsx` (incl. retired, create/edit with comma list of GPU archs, retire/restore, jobs in 30 days, upstream image)
- [x] F10 Rollout page: `app/admin/rollout/page.tsx` (effective target/ring/min version with override vs default, edit form sending only changed fields, empty clears an override, versions across the fleet, link to Nodes for per-node rings)
- [x] F11 Audit log page: `app/admin/audit/page.tsx` (action select from `actions` plus `family.*`, actor, target type/id, date range, cursor paging, compact JSON details); admin nav now also Schedules, Users, Catalog, Rollout, Audit
- [x] F12 New submit flow: `app/submit/page.tsx` rewritten (drop zone or pickers for .py/.zip/.tar.gz/folder, folders zipped in the browser with caches and a lone top folder stripped, `/jobs/inspect` fills the script picker and packages box, GPU cards from `/availability` with free/wait/"No PC of this class online" and a CPU-only card, environment and project selects, Advanced for runtime/GPU memory/CPU/RAM/disk/args, `?clone=<id>` prefills and submits through `POST /jobs/{id}/clone`)
- [x] F13 Waiting and progress view: job page shows "Nth in line for <class>" and "estimated start in ~X" above the existing stage bar (`queue_position`, `est_start_seconds`)
- [x] F14 Failure panel: `failure_explanation` card (title, advice) above the logs of failed jobs
- [x] F15 Job list upgrades: queue page has script search, status filter, per-row Clone (`/submit?clone=<id>`), shared Loading/Empty/ErrorBox; job page has Clone and "Download all outputs" (`/api/jobs/{id}/outputs.zip`) when outputs exist
- [x] F16 Notification settings: `app/settings/page.tsx` (email toggle disabled with a reason when `email_available` is false, webhook URL with client and server validation errors, browser-notification opt-in stored in localStorage) plus `components/BrowserNotifier.tsx` in the layout, polling `/jobs` every 15 s and notifying when a watched job finishes; nav gains Settings
- [x] F17 Polish: `components/Nav.tsx` (menu button under 720px, current page marked), skip link, `parseIso` in `lib/format.ts` used for every timestamp parse (microseconds cut to 3 digits, zone-less taken as UTC), Loading/Empty/ErrorBox and `role=alert` on the remaining pages, scrollable tables, new CSS on theme variables only; follow-ups: dashboard now reads `gpu_pct`/`busy`/`current_attempt` from one `GET /nodes` (per-node calls dropped), admin jobs project filter is `components/ProjectPicker.tsx` over `GET /admin/projects`
- [x] F18 End-to-end tests: `@playwright/test` 1.64.1 (dev dependency), `portal/playwright.config.ts` (base URL `E2E_BASE_URL`, one worker), `portal/e2e/` (job complete, cancel, admin drain/release, smoke of every admin and student page with no console errors, client error report, request ID in error box); `npm run test:e2e`; locally set `E2E_AGENT_STATE` to the agent's state file (default `.dev/agent-train.json`, what `scripts/dev.sh` writes); new CI job `e2e` runs `scripts/dev.sh`
- [x] F19 Frontend error reporting: `app/error.tsx` + `app/global-error.tsx`, `components/ClientErrorReporter.tsx` (window error and unhandledrejection) over `lib/clientErrors.ts` (3 a minute per tab, repeats dropped), `ApiError` in `lib/api.ts` carries `X-Request-ID` and `ErrorBox` shows it everywhere a page shows an API message; job page and nodes action errors now use `ErrorBox`

### QA fixes (browser QA pass, 2026-11-02)
- [ ] Q1 Mobile: admin tables scroll inside their cards (nodes, jobs, audit); no page-level horizontal overflow at 375px
- [ ] Q2 Schedules: heading keeps the target name after deleting its last window
- [ ] Q3 Project page: errors styled as errors, shown next to the action
- [ ] Q4 Member invites: allowed email domains enforced; invitees shown as "invited, never signed in" in members and admin users; adder told it is an invite (backend done: `invited` on member add/list and `/admin/users` + `invited=1|0` filter, `email_domain` 422, emails normalised; invite = placeholder `university_sub` `invite:`, no migration)
- [ ] Q5 Submit: GPU cards only for classes with a node seen in 7 days, GPU model name shown, offline classes collapsed, singular/plural, errors near the drop zone (backend done: `/availability` drops classes unheard of for `AVAILABILITY_CLASS_MAX_AGE_DAYS`=7, always has a `cpu` row over all train nodes, `gpu_model` per class)
- [ ] Q6 Dashboard: capped lists with "show all", no repeated hostname/class text, no "GPU —" on idle nodes
- [ ] Q7 Readability: relative times instead of raw ISO everywhere; charts auto-scale the y axis and fill their card; durations rolled up (h/m)
- [ ] Q8 Admin nodes: per-row quarantine reason, Forget separated from routine actions, filter and paging
- [ ] Q9 Admin jobs: one project control, labelled inputs, filters apply on change
- [ ] Q10 Job page: stepper reflects cancelled/finished runs; submitter and project shown (backend done: `GET /jobs/{id}` has `user_email` and `project_name`; admin list already did)
- [ ] Q11 Alerts: relative times, Forget only on offline-node alerts, confirm "Acknowledge all"
- [ ] Q12 Schedules: 24-hour time inputs, 00:00 row, room picker from known rooms, messages near the action
- [ ] Q13 Rollout ring options named (0 canary, 1 early, 2 everyone); catalog archs sorted; audit target links and readable details
- [ ] Q14 Settings: disabled email toggle shows off when email is unavailable; browser-notification guidance
- [ ] Q15 Nav: admin links grouped in a menu, logo never wraps; logout returns to the current origin; signed-in users on /login are redirected
- [ ] Q16 CSS warnings (autoprefixer mixed-support end values) removed

### Release
- [ ] R1 Agent version bump (single release)
- [x] R2 Full verification (pytest, ruff, alembic check, portal build, sim exam): 510 tests pass 3 runs in a row, ruff and alembic check clean, migrations 0003 down/up clean, portal prod build ok on a copy, prod compose and Caddy valid, sim exam 13/13
- [ ] R3 Docs updated (README, PROGRESS, runbooks, guide)

### Hardware and people (not code)
- [ ] H1 Reboot the GTX 1660 PC without logging in; confirm it returns
- [ ] H2 Read `C:\lab\supervisor\lab-agent.log` for the 2026-11-01 ~20:30 restart
- [ ] H3 Real rollback test on ring 0
- [ ] H4 Inventory every lab PC; add classes for new GPU models
- [ ] H5 Always-on Linux server, DNS names, ports approved by IT
- [ ] H6 Campus OIDC client from IT
- [ ] H7 Five-PC failure exam by hand
- [ ] H8 Staged rollout 1 → 5 → 15 → 30 → 60

### Log
Newest first. One line per landed package: date, package, result.
- 2026-11-02 PAUSED by the user mid-way through the frontend QA fixes (Q1–Q16): partial portal edits are in the working tree and type-check; Q items are not ticked because the agent was stopped before verifying. To resume: re-dispatch the frontend QA package (it must first check which Q items are already done), rerun the e2e suite, then R1 (agent 0.8.0 bump + real-PC check) and R3 (docs). Nothing committed.

- 2026-11-02 QA-BE: Q4, Q5, Q10 backend done, no migration (invited = `invite:` subject, cleared by every sign-in incl. dev header); tests in `test_people.py` and `test_student_view.py`; frontend ticks the Q lines
- 2026-11-02 R2: full verification passed with no product fixes (510 tests x3, ruff, alembic check, scratch-DB upgrade/downgrade 0003/upgrade, portal build, compose and Caddy validation, sim exam 13/13); CI e2e job now sets `MIRROR_CATALOG_IMAGES=0` and `E2E_JOB_TIMEOUT_MS=360000` (job waits and the Playwright test timeout follow it; local default unchanged)
- 2026-11-02 FE: F18 and F19 done; 7 Playwright tests pass twice in a row against the dev stack with a temporary agent (stopped afterwards); no product bugs found by the tests; `tsc --noEmit` clean, ruff clean, no backend change; CI job `e2e` is untested on GitHub (needs the mirror build of `python-cpu` to finish inside the 3 minute job wait when `REGISTRY_URL` is set)
- 2026-11-02 P7: B29–B32, X5, X6 done (no migration); 510 tests pass, 5 runs in a row; rate limits are in-process and checked after the request body is read (a dependency, not a proxy rule), Caddy caps the body size; request logs replace uvicorn's access log; load test 100 agents / 500 jobs: 0 errors, claim p95 734 ms; FE can build F19 on `POST /client-errors` (202, `{message, stack, url, user_agent}`)
- 2026-11-02 FD: F12–F17 and both follow-ups done (submit flow with clone, queue position, failure card, job list search/clone/outputs zip, settings with browser notifications, polish, dashboard on `/nodes`, project picker); `tsc --noEmit` clean, no build run; pages return 200 on the dev portal, not rendered by an agent
- 2026-11-02 P6: B22–B28, X3, X4 done (migration 0009; new `routers/student.py` with `/jobs/inspect`, `/availability`, `/me/notifications`, `/jobs/{id}/clone`, `/jobs/{id}/outputs.zip`); 415 tests pass, 3 runs in a row; a test that leaves a build pending breaks `test_building_environment_stage_comes_from_the_build_row` (any builder claims the oldest pending build), so tests complete the builds they start; FD can switch the dashboard and jobs filter to `/nodes` and `/admin/projects`
- 2026-11-01 FC: F7-F11 done (schedules, projects and users, catalog, rollout, audit log; nav links); `tsc --noEmit` clean, no build run; pages return 200 on the dev portal
- 2026-11-01 P5: B17–B21 done (migration 0008: availability_windows, platform_settings, audit_log, environments.retired_at); 340 tests pass, 3 runs in a row; `/environments` now requires a sign-in (blocked users get 403); new endpoint groups for FC: `/admin/schedules`, `/projects/{id}/members`, `/admin/users`, `/admin/quotas`, `/admin/environments`, `/admin/rollout`, `/admin/audit`
- 2026-11-01 FB: F4–F6 done (lab dashboard, nodes page, admin jobs, alerts panel, admin nav); `tsc --noEmit` clean, no build run; `/nodes` has no GPU load or running job, so the dashboard reads `/admin/jobs` for jobs and `/nodes/{id}` for busy PCs' GPU %
- 2026-11-01 P4: flaky test `test_lost_leases_are_counted` fixed (an attempt left leased by the previous test expired inside its window and bumped the global lease-lost counter; it now waits for no active attempts first and the leaking test completes its attempt); two more flakes found and fixed on the way: `test_waiting_for_the_class_when_every_node_of_it_is_offline` (real bug: the offline sweep overwrote a heartbeat that landed while it ran; now conditional UPDATEs, heartbeat un-offlines on the current row) and `test_launcher_rolls_back_a_crashing_release` (test read `rejected` before the launcher wrote it); B14–B16 done (migration 0007); 277 tests pass, 10 runs in a row; `GET /admin/alerts` changed shape, portal admin page needs FB
- 2026-11-01 P3: B10–B13, X1, X2 done (metrics, Prometheus + Grafana, usage reports, agent logs on demand, migration 0006); 227 tests pass; sim exam 13/13 twice in a row
- 2026-11-01 FA: F1–F3 done (job graphs and stages, node detail, usage pages); `tsc --noEmit` clean, no build run
- 2026-11-01 P2: B6–B9 done; 168 tests pass; sim exam 13/13
- 2026-11-01 P1: B1–B5 done; 123 tests pass; sim exam 13/13
- 2026-11-01 Frontend foundations: shared components (LineChart, StageBar, States, DataTable, ConfirmButton), lib/zip.ts, lib/format.ts, theme variables
