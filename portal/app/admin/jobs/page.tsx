"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import ConfirmButton from "@/components/ConfirmButton";
import DataTable, { type Column } from "@/components/DataTable";
import ProjectPicker from "@/components/ProjectPicker";
import { Empty, ErrorBox, Loading } from "@/components/States";
import Time from "@/components/Time";
import { api, fmtMs, type AdminJob, type AdminJobList, type BulkCancelResult } from "@/lib/api";


type Filters = {
  status: string;
  user: string;
  project_id: string;
  gpu_class: string;
  node: string;
  since: string;
  until: string;
};

const EMPTY: Filters = { status: "", user: "", project_id: "", gpu_class: "", node: "", since: "", until: "" };
const FINAL = ["completed", "failed", "cancelled"];
const PRESETS: { value: string; label: string }[] = [
  { value: "", label: "Any status" },
  { value: "queued,building", label: "Waiting (queued, building)" },
  { value: "starting,running,uploading", label: "Active (starting, running, uploading)" },
  { value: "queued", label: "Queued" },
  { value: "building", label: "Building" },
  { value: "running", label: "Running" },
  { value: "uploading", label: "Uploading" },
  { value: "unknown", label: "Unknown (node lost)" },
  { value: "completed", label: "Completed" },
  { value: "failed", label: "Failed" },
  { value: "cancelled", label: "Cancelled" },
  { value: "failed,cancelled", label: "Failed or cancelled" },
];

function query(f: Filters, cursor: string | null) {
  const qs = new URLSearchParams({ limit: "50" });
  (Object.keys(f) as (keyof Filters)[]).forEach((k) => {
    const v = f[k].trim();
    if (v) qs.set(k, v);
  });
  if (cursor) qs.set("cursor", cursor);
  return qs.toString();
}

function fromUrl(): Filters {
  const p = new URLSearchParams(window.location.search);
  const out = { ...EMPTY };
  (Object.keys(out) as (keyof Filters)[]).forEach((k) => {
    out[k] = p.get(k) ?? "";
  });
  return out;
}

export default function AdminJobsPage() {
  const [draft, setDraft] = useState<Filters>(EMPTY);
  const [applied, setApplied] = useState<Filters | null>(null);
  const [jobs, setJobs] = useState<AdminJob[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [more, setMore] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const [selected, setSelected] = useState<string[]>([]);

  // The dashboard links here with filters in the query string.
  useEffect(() => {
    const initial = fromUrl();
    setDraft(initial);
    setApplied(initial);
  }, []);

  const loadFirst = useCallback(async (f: Filters) => {
    setLoading(true);
    try {
      const data = (await api(`/api/admin/jobs?${query(f, null)}`)) as AdminJobList;
      setJobs(data.items);
      setNext(data.next_cursor);
      setSelected((cur) => cur.filter((id) => data.items.some((j) => j.id === id)));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load jobs");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (applied) void loadFirst(applied);
  }, [applied, loadFirst]);

  async function loadMore() {
    if (!applied || !next) return;
    setMore(true);
    try {
      const data = (await api(`/api/admin/jobs?${query(applied, next)}`)) as AdminJobList;
      setJobs((cur) => {
        const seen = new Set(cur.map((j) => j.id));
        return [...cur, ...data.items.filter((j) => !seen.has(j.id))];
      });
      setNext(data.next_cursor);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load more jobs");
    } finally {
      setMore(false);
    }
  }

  async function cancel(ids: string[]) {
    try {
      const res = (await api("/api/admin/jobs/cancel", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ job_ids: ids }),
      })) as BulkCancelResult;
      const skipped = res.skipped.length ? `, ${res.skipped.length} skipped (${res.skipped[0].reason})` : "";
      setNote(`Cancelled ${res.cancelled.length}${skipped}.`);
      setSelected([]);
    } catch (err) {
      setNote(err instanceof Error ? err.message : "cancel failed");
    }
    if (applied) await loadFirst(applied);
  }

  async function requeue(id: string) {
    try {
      await api(`/api/admin/jobs/${id}/requeue`, { method: "POST" });
      setNote("Job requeued.");
    } catch (err) {
      setNote(err instanceof Error ? err.message : "requeue failed");
    }
    if (applied) await loadFirst(applied);
  }

  // Selects, dates and the project apply at once; text boxes apply when typing pauses.
  const change = (k: keyof Filters, value: string) => {
    setDraft((d) => ({ ...d, [k]: value }));
    setApplied((a) => ({ ...(a ?? EMPTY), [k]: value }));
  };
  const setText = (k: "user" | "node" | "gpu_class") => (e: { target: { value: string } }) =>
    setDraft((d) => ({ ...d, [k]: e.target.value }));

  useEffect(() => {
    if (!applied) return;
    if (draft.user === applied.user && draft.node === applied.node && draft.gpu_class === applied.gpu_class) return;
    const timer = setTimeout(
      () => setApplied((a) => (a ? { ...a, user: draft.user, node: draft.node, gpu_class: draft.gpu_class } : a)),
      400,
    );
    return () => clearTimeout(timer);
  }, [draft.user, draft.node, draft.gpu_class, applied]);
  const known = PRESETS.some((p) => p.value === draft.status);
  const activeSelected = selected.filter((id) => {
    const j = jobs.find((x) => x.id === id);
    return j && !FINAL.includes(j.status);
  });

  const columns: Column<AdminJob>[] = [
    {
      key: "status",
      header: "Status",
      render: (j) => (
        <>
          <span className={`chip ${j.status}`}>{j.status}</span>
          {j.pending_reason && !FINAL.includes(j.status) ? <div className="muted nodes-sub">{j.pending_reason}</div> : null}
          {j.latest_attempt?.failure_category ? (
            <div className="muted nodes-sub">{j.latest_attempt.failure_category}</div>
          ) : null}
        </>
      ),
    },
    {
      key: "job",
      header: "Job",
      render: (j) => (
        <>
          <Link href={`/jobs/${j.id}`} className="mono">
            {j.entrypoint}
          </Link>
          <div className="muted nodes-sub">
            {j.project_name} · {j.user_email}
          </div>
        </>
      ),
    },
    {
      key: "where",
      header: "Node",
      render: (j) => (
        <>
          {j.latest_attempt?.hostname ?? <span className="muted">—</span>}
          <div className="muted nodes-sub mono">
            {typeof j.resources?.gpu_class === "string" ? j.resources.gpu_class : "any class"}
          </div>
        </>
      ),
    },
    {
      key: "when",
      header: "Created",
      render: (j) => (
        <>
          <Time iso={j.created_at} />
          <div className="muted nodes-sub">
            wait {fmtMs(j.timings.queued_ms)} · run {fmtMs(j.timings.running_ms)}
          </div>
        </>
      ),
    },
    {
      key: "actions",
      header: "",
      render: (j) => (
        <div className="nodes-actions">
          {FINAL.includes(j.status) ? null : (
            <ConfirmButton className="ghost danger small" message="Cancel this job?" onConfirm={() => cancel([j.id])}>
              Cancel
            </ConfirmButton>
          )}
          {j.status === "failed" || j.status === "cancelled" ? (
            <button type="button" className="ghost small" onClick={() => void requeue(j.id)}>
              Requeue
            </button>
          ) : null}
        </div>
      ),
    },
  ];

  return (
    <div className="stack">
      <div>
        <h1>All jobs</h1>
        <p className="muted">Every user&apos;s jobs, newest first. Filters apply as you change them.</p>
      </div>
      <form className="card filters" onSubmit={(e) => e.preventDefault()} aria-label="Filter jobs">
        <label htmlFor="jobs-status">
          Status
          <select id="jobs-status" value={draft.status} onChange={(e) => change("status", e.target.value)}>
            {known ? null : <option value={draft.status}>{draft.status}</option>}
            {PRESETS.map((p) => (
              <option key={p.value} value={p.value}>
                {p.label}
              </option>
            ))}
          </select>
        </label>
        <label htmlFor="jobs-user">
          User (email contains)
          <input id="jobs-user" type="search" value={draft.user} onChange={setText("user")} autoComplete="off" />
        </label>
        <label htmlFor="jobs-node">
          Node (hostname contains)
          <input id="jobs-node" type="search" value={draft.node} onChange={setText("node")} autoComplete="off" />
        </label>
        <label htmlFor="jobs-class">
          GPU class
          <input id="jobs-class" type="search" value={draft.gpu_class} onChange={setText("gpu_class")} autoComplete="off" />
        </label>
        <ProjectPicker value={draft.project_id} onChange={(id) => change("project_id", id)} />
        <label htmlFor="jobs-since">
          From
          <input id="jobs-since" type="date" value={draft.since} onChange={(e) => change("since", e.target.value)} />
        </label>
        <label htmlFor="jobs-until">
          To
          <input id="jobs-until" type="date" value={draft.until} onChange={(e) => change("until", e.target.value)} />
        </label>
        <div className="nodes-actions">
          <button
            type="button"
            className="ghost"
            onClick={() => {
              setDraft(EMPTY);
              setApplied({ ...EMPTY });
            }}
          >
            Clear filters
          </button>
        </div>
      </form>

      {note ? (
        <p role="status" className="muted">
          {note}
        </p>
      ) : null}
      {error ? <ErrorBox message={error} onRetry={() => applied && void loadFirst(applied)} /> : null}

      {selected.length ? (
        <div className="card bulkbar">
          <span>{selected.length} selected</span>
          <ConfirmButton
            className="ghost danger small"
            disabled={activeSelected.length === 0}
            message={`Cancel ${activeSelected.length} unfinished job${activeSelected.length === 1 ? "" : "s"}?`}
            onConfirm={() => cancel(activeSelected)}
          >
            Cancel selected
          </ConfirmButton>
          <button type="button" className="ghost small" onClick={() => setSelected([])}>
            Clear selection
          </button>
        </div>
      ) : null}

      {loading && jobs.length === 0 ? (
        <Loading label="Loading jobs…" />
      ) : (
        <div className="card">
          <DataTable
            columns={columns}
            rows={jobs}
            rowKey={(j) => j.id}
            caption="Jobs"
            selectable
            selected={selected}
            onSelectionChange={setSelected}
            hasMore={next !== null}
            onLoadMore={() => void loadMore()}
            loading={more}
            empty={
              error ? null : <Empty title="No jobs match" hint="Loosen the filters, or wait for someone to submit one." />
            }
          />
        </div>
      )}
    </div>
  );
}
