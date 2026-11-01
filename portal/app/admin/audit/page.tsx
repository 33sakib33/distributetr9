"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import DataTable, { type Column } from "@/components/DataTable";
import { Empty, ErrorBox, Loading } from "@/components/States";
import { api, type AuditList, type AuditRow } from "@/lib/api";
import Time from "@/components/Time";

type Filters = { action: string; actor: string; target_type: string; target_id: string; since: string; until: string };
const EMPTY: Filters = { action: "", actor: "", target_type: "", target_id: "", since: "", until: "" };
const TARGET_TYPES = ["node", "job", "alert", "alert_kind", "schedule", "user", "project", "environment", "rollout"];

function query(f: Filters, cursor: string | null) {
  const qs = new URLSearchParams({ limit: "50" });
  (Object.keys(f) as (keyof Filters)[]).forEach((k) => {
    const v = f[k].trim();
    if (v) qs.set(k, v);
  });
  if (cursor) qs.set("cursor", cursor);
  return qs.toString();
}

function valueText(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "object") {
    try {
      return JSON.stringify(v);
    } catch {
      return String(v);
    }
  }
  return String(v);
}

/** Details as one "key: value" line each. */
function Details({ details }: { details: Record<string, unknown> | null }) {
  const entries = Object.entries(details ?? {});
  if (!entries.length) return <span className="muted">—</span>;
  return (
    <ul className="kv mono">
      {entries.map(([k, v]) => (
        <li key={k}>
          <span className="kv-key">{k}:</span> {valueText(v)}
        </li>
      ))}
    </ul>
  );
}

/** Where a target type has a page of its own, link the id there. */
function targetHref(type: string, id: string): string | null {
  if (type === "node") return `/admin/nodes/${id}`;
  if (type === "job") return `/jobs/${id}`;
  return null;
}

export default function AdminAuditPage() {
  const [draft, setDraft] = useState<Filters>(EMPTY);
  const [applied, setApplied] = useState<Filters>(EMPTY);
  const [rows, setRows] = useState<AuditRow[]>([]);
  const [actions, setActions] = useState<string[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [more, setMore] = useState(false);
  const [error, setError] = useState("");

  const loadFirst = useCallback(async (f: Filters) => {
    setLoading(true);
    try {
      const data = (await api(`/api/admin/audit?${query(f, null)}`)) as AuditList;
      setRows(data.items);
      setNext(data.next_cursor);
      setActions(data.actions);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load the audit log");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadFirst(applied);
  }, [applied, loadFirst]);

  async function loadMore() {
    if (!next) return;
    setMore(true);
    try {
      const data = (await api(`/api/admin/audit?${query(applied, next)}`)) as AuditList;
      setRows((cur) => {
        const seen = new Set(cur.map((r) => r.id));
        return [...cur, ...data.items.filter((r) => !seen.has(r.id))];
      });
      setNext(data.next_cursor);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load more");
    } finally {
      setMore(false);
    }
  }

  const set = (k: keyof Filters) => (e: { target: { value: string } }) => setDraft((d) => ({ ...d, [k]: e.target.value }));
  // Families such as "node.*" are offered too, from the dotted prefixes of the known actions.
  const families = Array.from(new Set(actions.map((a) => a.split(".")[0]))).map((p) => `${p}.*`);
  const known = draft.action === "" || actions.includes(draft.action) || families.includes(draft.action);
  const dateInvalid = !!draft.since && !!draft.until && draft.since > draft.until;

  const columns: Column<AuditRow>[] = [
    {
      key: "at",
      header: "When",
      render: (r) => <span className="nowrap"><Time iso={r.at} /></span>,
    },
    { key: "actor", header: "Who", render: (r) => <span className="email-cell">{r.actor_email ?? <span className="muted">—</span>}</span> },
    { key: "action", header: "Action", render: (r) => <span className="mono">{r.action}</span> },
    {
      key: "target",
      header: "Target",
      render: (r) => (
        <>
          {r.target_type}
          {r.target_id ? (
            <div className="nodes-sub mono">
              {targetHref(r.target_type, r.target_id) ? (
                <Link href={targetHref(r.target_type, r.target_id) as string} title={r.target_id}>
                  {r.target_id.slice(0, 8)}
                </Link>
              ) : (
                <span className="muted wrap-any">{r.target_id}</span>
              )}
            </div>
          ) : null}
        </>
      ),
    },
    {
      key: "details",
      header: "Details",
      render: (r) => <Details details={r.details} />,
    },
  ];

  return (
    <div className="stack">
      <div>
        <h1>Audit log</h1>
        <p className="muted">Every change made by an admin, and member changes made by project owners. Newest first.</p>
      </div>
      <form
        className="card filters"
        onSubmit={(e) => {
          e.preventDefault();
          if (dateInvalid) return;
          setApplied({ ...draft });
        }}
      >
        <label>
          Action
          <select value={draft.action} onChange={set("action")}>
            <option value="">Any action</option>
            {known || !draft.action ? null : <option value={draft.action}>{draft.action}</option>}
            {families.length ? (
              <optgroup label="Families">
                {families.map((f) => (
                  <option key={f} value={f}>
                    {f}
                  </option>
                ))}
              </optgroup>
            ) : null}
            <optgroup label="Actions">
              {actions.map((a) => (
                <option key={a} value={a}>
                  {a}
                </option>
              ))}
            </optgroup>
          </select>
        </label>
        <label>
          Who (email contains)
          <input value={draft.actor} onChange={set("actor")} />
        </label>
        <label>
          Target type
          <select value={draft.target_type} onChange={set("target_type")}>
            <option value="">Any</option>
            {TARGET_TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>
        <label>
          Target ID
          <input value={draft.target_id} onChange={set("target_id")} className="mono" />
        </label>
        <label>
          From
          <input type="date" value={draft.since} onChange={set("since")} />
        </label>
        <label>
          To
          <input type="date" value={draft.until} onChange={set("until")} />
        </label>
        <div className="nodes-actions">
          <button type="submit" disabled={dateInvalid}>
            Apply
          </button>
          <button
            type="button"
            className="ghost"
            onClick={() => {
              setDraft(EMPTY);
              setApplied({ ...EMPTY });
            }}
          >
            Clear
          </button>
        </div>
        {dateInvalid ? (
          <p className="err" role="alert">
            The start date is after the end date.
          </p>
        ) : null}
      </form>

      {error ? <ErrorBox message={error} onRetry={() => void loadFirst(applied)} /> : null}
      {loading && rows.length === 0 ? (
        <Loading label="Loading audit log…" />
      ) : error && rows.length === 0 ? null : (
        <div className="card">
          <DataTable
            columns={columns}
            rows={rows}
            rowKey={(r) => r.id}
            caption="Audit log"
            hasMore={next !== null}
            onLoadMore={() => void loadMore()}
            loading={more}
            empty={<Empty title="No matching entries" hint="Change the filters or widen the date range." />}
          />
        </div>
      )}
    </div>
  );
}
