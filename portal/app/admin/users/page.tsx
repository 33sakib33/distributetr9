"use client";

import { useCallback, useEffect, useState } from "react";
import ConfirmButton from "@/components/ConfirmButton";
import DataTable, { type Column } from "@/components/DataTable";
import { Empty, ErrorBox, Loading } from "@/components/States";
import { api, jsonInit, type AdminUser, type Page, type QuotaItem } from "@/lib/api";
import Time from "@/components/Time";

type Filters = { q: string; blocked: "" | "true" | "false"; invited: "" | "1" | "0" };
const EMPTY: Filters = { q: "", blocked: "", invited: "" };

function query(f: Filters, cursor: string | null) {
  const qs = new URLSearchParams({ limit: "50" });
  if (f.q.trim()) qs.set("q", f.q.trim());
  if (f.blocked) qs.set("blocked", f.blocked);
  if (f.invited) qs.set("invited", f.invited);
  if (cursor) qs.set("cursor", cursor);
  return qs.toString();
}

function QuotaCell({ user, onSaved, onError }: { user: AdminUser; onSaved: (n: number) => void; onError: (m: string) => void }) {
  const [value, setValue] = useState(String(user.max_concurrent_jobs));
  const [busy, setBusy] = useState(false);
  useEffect(() => setValue(String(user.max_concurrent_jobs)), [user.max_concurrent_jobs]);
  const n = Number(value);
  const valid = value.trim() !== "" && Number.isInteger(n) && n >= 0 && n <= 1000;

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!valid) return onError("The quota must be a whole number from 0 to 1000.");
    setBusy(true);
    try {
      const res = (await api(`/api/admin/quotas/user/${user.id}`, jsonInit("PUT", { max_concurrent_jobs: n }))) as QuotaItem;
      onSaved(res.max_concurrent_jobs);
    } catch (err) {
      onError(err instanceof Error ? err.message : "could not save the quota");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="inline-form" onSubmit={save}>
      <input
        type="number"
        min={0}
        max={1000}
        aria-label={`Concurrent job quota for ${user.email}`}
        value={value}
        onChange={(e) => setValue(e.target.value)}
      />
      <button type="submit" className="ghost small" disabled={busy || !valid || n === user.max_concurrent_jobs}>
        Save
      </button>
    </form>
  );
}

export default function AdminUsersPage() {
  const [draft, setDraft] = useState<Filters>(EMPTY);
  const [applied, setApplied] = useState<Filters>(EMPTY);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [more, setMore] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");

  const loadFirst = useCallback(async (f: Filters) => {
    setLoading(true);
    try {
      const data = (await api(`/api/admin/users?${query(f, null)}`)) as Page<AdminUser>;
      setUsers(data.items);
      setNext(data.next_cursor);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load users");
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
      const data = (await api(`/api/admin/users?${query(applied, next)}`)) as Page<AdminUser>;
      setUsers((cur) => {
        const seen = new Set(cur.map((u) => u.id));
        return [...cur, ...data.items.filter((u) => !seen.has(u.id))];
      });
      setNext(data.next_cursor);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load more users");
    } finally {
      setMore(false);
    }
  }

  const patch = (id: string, change: Partial<AdminUser>) =>
    setUsers((cur) => cur.map((u) => (u.id === id ? { ...u, ...change } : u)));

  async function setBlocked(u: AdminUser, block: boolean) {
    try {
      const res = (await api(`/api/admin/users/${u.id}/${block ? "block" : "unblock"}`, { method: "POST" })) as AdminUser;
      patch(u.id, res);
      setNote(`${u.email} ${block ? "blocked" : "unblocked"}.`);
    } catch (err) {
      setNote(err instanceof Error ? err.message : "action failed");
    }
  }

  const columns: Column<AdminUser>[] = [
    {
      key: "user",
      header: "User",
      render: (u) => (
        <>
          {u.display_name} {u.is_admin ? <span className="chip admin">admin</span> : null}
          <div className="muted nodes-sub wrap-any">{u.email}</div>
          {u.invited ? <span className="chip invited">Invited — joins on first sign-in</span> : null}
        </>
      ),
    },
    {
      key: "status",
      header: "Status",
      render: (u) =>
        u.blocked ? (
          <>
            <span className="chip blocked">blocked</span>
            <div className="muted nodes-sub">
              <Time iso={u.disabled_at} />
            </div>
          </>
        ) : (
          <span className="chip active">active</span>
        ),
    },
    { key: "created", header: "Joined", render: (u) => (u.invited ? <span className="muted">not yet</span> : <Time iso={u.created_at} />) },
    {
      key: "quota",
      header: "Jobs at once",
      render: (u) => <QuotaCell user={u} onSaved={(n) => { patch(u.id, { max_concurrent_jobs: n }); setNote(`Quota for ${u.email} saved.`); }} onError={setNote} />,
    },
    {
      key: "actions",
      header: "",
      render: (u) =>
        u.blocked ? (
          <button type="button" className="ghost small" onClick={() => void setBlocked(u, false)}>
            Unblock
          </button>
        ) : (
          <ConfirmButton className="ghost danger small" message={`Block ${u.email}?`} onConfirm={() => setBlocked(u, true)}>
            Block
          </ConfirmButton>
        ),
    },
  ];

  return (
    <div className="stack">
      <div>
        <h1>Users</h1>
        <p className="muted">Blocked users get an error on every request; jobs already running finish.</p>
      </div>
      <form
        className="card filters"
        onSubmit={(e) => {
          e.preventDefault();
          setApplied({ ...draft });
        }}
      >
        <label>
          Search (email or name)
          <input value={draft.q} onChange={(e) => setDraft({ ...draft, q: e.target.value })} />
        </label>
        <label>
          Status
          <select value={draft.blocked} onChange={(e) => setDraft({ ...draft, blocked: e.target.value as Filters["blocked"] })}>
            <option value="">Everyone</option>
            <option value="true">Blocked only</option>
            <option value="false">Active only</option>
          </select>
        </label>
        <label>
          Invited
          <select value={draft.invited} onChange={(e) => setDraft({ ...draft, invited: e.target.value as Filters["invited"] })}>
            <option value="">Everyone</option>
            <option value="1">Invited, never signed in</option>
            <option value="0">Signed in at least once</option>
          </select>
        </label>
        <div className="nodes-actions">
          <button type="submit">Apply</button>
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
      </form>
      {note ? (
        <p role="status" className="muted">
          {note}
        </p>
      ) : null}
      {error ? <ErrorBox message={error} onRetry={() => void loadFirst(applied)} /> : null}
      {loading && users.length === 0 ? (
        <Loading label="Loading users…" />
      ) : error && users.length === 0 ? null : (
        <div className="card">
          <DataTable
            columns={columns}
            rows={users}
            rowKey={(u) => u.id}
            caption="Users"
            hasMore={next !== null}
            onLoadMore={() => void loadMore()}
            loading={more}
            empty={<Empty title="No users match" hint="Try a different search." />}
          />
        </div>
      )}
    </div>
  );
}
