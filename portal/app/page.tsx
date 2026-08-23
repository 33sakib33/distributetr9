"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, fmtMs, type Job } from "@/lib/api";

export default function JobsPage() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [error, setError] = useState("");

  async function refresh() {
    try {
      const data = (await api("/api/jobs")) as { jobs: Job[] };
      setJobs(data.jobs);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed to load jobs");
    }
  }

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 4000);
    return () => clearInterval(t);
  }, []);

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
        <p className="err">
          {error}{" "}
          {/sign in|university|unauthorized/i.test(error) ? <Link href="/login">Sign in</Link> : null}
        </p>
      ) : null}
      <div className="card" style={{ padding: 0 }}>
        <table>
          <thead>
            <tr>
              <th>Status</th>
              <th>Script</th>
              <th>Node</th>
              <th>Wait / run</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {jobs.length === 0 ? (
              <tr>
                <td colSpan={5} className="muted">
                  No jobs yet.
                </td>
              </tr>
            ) : (
              jobs.map((job) => (
                <tr key={job.id}>
                  <td>
                    <Link href={`/jobs/${job.id}`}>
                      <span className={`chip ${job.status}`}>{job.status}</span>
                    </Link>
                  </td>
                  <td>
                    <Link href={`/jobs/${job.id}`}>
                      <code>{job.entrypoint}</code>
                    </Link>
                  </td>
                  <td className="muted">{job.latest_attempt?.hostname || "—"}</td>
                  <td className="muted">
                    {fmtMs(job.timings.queued_ms)} / {fmtMs(job.timings.running_ms)}
                  </td>
                  <td className="muted">{job.pending_reason || ""}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
