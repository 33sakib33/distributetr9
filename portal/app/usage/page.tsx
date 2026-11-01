"use client";

import { useCallback, useEffect, useState } from "react";
import DataTable, { type Column } from "@/components/DataTable";
import { Empty, ErrorBox, Loading } from "@/components/States";
import { api, type UsageReport, type UsageRow } from "@/lib/api";
import { defaultRange, rangeParams } from "@/lib/range";

const columns: Column<UsageRow>[] = [
  { key: "label", header: "Project", render: (r) => r.label || r.key },
  { key: "gpu", header: "GPU hours", align: "right", render: (r) => r.gpu_hours.toFixed(2) },
  { key: "jobs", header: "Jobs", align: "right", render: (r) => r.jobs },
  { key: "ok", header: "Succeeded", align: "right", render: (r) => r.succeeded },
  { key: "bad", header: "Failed", align: "right", render: (r) => r.failed },
];

export default function MyUsagePage() {
  const initial = defaultRange();
  const [from, setFrom] = useState(initial.from);
  const [to, setTo] = useState(initial.to);
  const [report, setReport] = useState<UsageReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    if (!from || !to || from > to) {
      setError("Choose a start date on or before the end date.");
      return;
    }
    setLoading(true);
    try {
      setReport((await api(`/api/me/usage?${rangeParams(from, to)}`)) as UsageReport);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed to load usage");
    } finally {
      setLoading(false);
    }
  }, [from, to]);

  useEffect(() => {
    void load();
  }, [load]);

  const rows = report?.rows ?? [];
  const total = rows.reduce(
    (a, r) => ({ gpu: a.gpu + r.gpu_hours, jobs: a.jobs + r.jobs, ok: a.ok + r.succeeded, bad: a.bad + r.failed }),
    { gpu: 0, jobs: 0, ok: 0, bad: 0 },
  );

  return (
    <div className="stack">
      <div>
        <h1>My usage</h1>
        <p className="muted">GPU hours are the time your jobs ran on a GPU, per project.</p>
      </div>
      <div className="toolbar">
        <label>
          From
          <input type="date" value={from} max={to} onChange={(e) => setFrom(e.target.value)} />
        </label>
        <label>
          To
          <input type="date" value={to} min={from} onChange={(e) => setTo(e.target.value)} />
        </label>
      </div>
      {error ? <ErrorBox message={error} onRetry={() => void load()} /> : null}
      {loading && !report ? <Loading label="Loading usage…" /> : null}
      {report ? (
        <>
          <div className="card">
            <div className="stats">
              <div>
                <div className="stat-label">GPU hours</div>
                <div className="stat-value">{total.gpu.toFixed(2)}</div>
              </div>
              <div>
                <div className="stat-label">Jobs</div>
                <div className="stat-value">{total.jobs}</div>
              </div>
              <div>
                <div className="stat-label">Succeeded</div>
                <div className="stat-value">{total.ok}</div>
              </div>
              <div>
                <div className="stat-label">Failed</div>
                <div className="stat-value">{total.bad}</div>
              </div>
            </div>
          </div>
          <div className="card" style={{ padding: 0 }}>
            <DataTable
              columns={columns}
              rows={rows}
              rowKey={(r) => r.key}
              loading={loading}
              caption="Usage by project"
              empty={<Empty title="No usage in this period" hint="Jobs that ran on a GPU show up here." />}
            />
          </div>
        </>
      ) : null}
    </div>
  );
}
