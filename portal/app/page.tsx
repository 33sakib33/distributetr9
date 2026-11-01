"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import DataTable, { type Column } from "@/components/DataTable";
import { Empty, ErrorBox, Loading } from "@/components/States";
import { api, fmtMs, type Job } from "@/lib/api";

const FILTERS: { value: string; label: string; statuses: string[] }[] = [
  { value: "", label: "Any status", statuses: [] },
  { value: "waiting", label: "Waiting (queued, building)", statuses: ["queued", "building"] },
  { value: "active", label: "Active (starting, running, uploading)", statuses: ["starting", "running", "uploading", "unknown", "cancelling"] },
  { value: "completed", label: "Completed", statuses: ["completed"] },
  { value: "failed", label: "Failed", statuses: ["failed"] },
  { value: "cancelled", label: "Cancelled", statuses: ["cancelled"] },
];

export default function JobsPage() {
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("");

  const refresh = useCallback(async () => {
    try {
      const data = (await api("/api/jobs")) as { jobs: Job[] };
      setJobs(data.jobs);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed to load jobs");
    }
  }, []);

  useEffect(() => {
    void refresh();
    const t = setInterval(() => void refresh(), 4000);
    return () => clearInterval(t);
  }, [refresh]);

  const wanted = FILTERS.find((f) => f.value === filter)?.statuses ?? [];
  const needle = search.trim().toLowerCase();
  const shown = (jobs ?? []).filter(
    (j) =>
      (!wanted.length || wanted.includes(j.status)) && (!needle || j.entrypoint.toLowerCase().includes(needle)),
  );

  const columns: Column<Job>[] = [
    {
      key: "status",
      header: "Status",
      render: (job) => (
        <Link href={`/jobs/${job.id}`}>
          <span className={`chip ${job.status}`}>{job.status}</span>
        </Link>
      ),
    },
    {
      key: "script",
      header: "Script",
      render: (job) => (
        <Link href={`/jobs/${job.id}`}>
          <code className="wrap-any">{job.entrypoint}</code>
        </Link>
      ),
    },
    { key: "node", header: "Node", render: (job) => <span className="muted">{job.latest_attempt?.hostname || "—"}</span> },
    {
      key: "time",
      header: "Wait / run",
      render: (job) => (
        <span className="muted">
          {fmtMs(job.timings.queued_ms)} / {fmtMs(job.timings.running_ms)}
        </span>
      ),
    },
    { key: "reason", header: "Reason", render: (job) => <span className="muted">{job.pending_reason || ""}</span> },
    {
      key: "actions",
      header: "",
      render: (job) => (
        <Link className="btn ghost small" href={`/submit?clone=${job.id}`} aria-label={`Clone ${job.entrypoint}`}>
          Clone
        </Link>
      ),
    },
  ];

  const signIn = /sign in|university|unauthorized/i.test(error);

  return (
    <div className="stack">
      <div className="row">
        <div>
          <h1>Queue</h1>
          <p className="muted">Jobs for projects you belong to. Status refreshes every few seconds.</p>
        </div>
        <Link className="btn" href="/submit">
          Submit job
        </Link>
      </div>
      {error ? (
        <div>
          <ErrorBox message={error} onRetry={signIn ? undefined : () => void refresh()} />
          {signIn ? (
            <p>
              <Link href="/login">Sign in</Link>
            </p>
          ) : null}
        </div>
      ) : null}
      {jobs === null && !error ? <Loading label="Loading jobs…" /> : null}
      {jobs !== null ? (
        <>
          <div className="card filters">
            <label>
              Search by script
              <input
                type="search"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="train.py"
                autoComplete="off"
              />
            </label>
            <label>
              Status
              <select value={filter} onChange={(e) => setFilter(e.target.value)}>
                {FILTERS.map((f) => (
                  <option key={f.value} value={f.value}>
                    {f.label}
                  </option>
                ))}
              </select>
            </label>
            {search || filter ? (
              <button
                type="button"
                className="ghost"
                onClick={() => {
                  setSearch("");
                  setFilter("");
                }}
              >
                Clear
              </button>
            ) : null}
          </div>
          <div className="card">
            <DataTable
              columns={columns}
              rows={shown}
              rowKey={(j) => j.id}
              caption="Your jobs"
              empty={
                jobs.length === 0 ? (
                  <Empty
                    title="No jobs yet"
                    hint="Submit a Python script to see it here."
                    action={
                      <Link className="btn" href="/submit">
                        Submit job
                      </Link>
                    }
                  />
                ) : (
                  <Empty title="No jobs match" hint="Change the search or the status filter." />
                )
              }
            />
          </div>
        </>
      ) : null}
    </div>
  );
}
