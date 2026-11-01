"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import ConfirmButton from "@/components/ConfirmButton";
import DataTable, { type Column } from "@/components/DataTable";
import { Empty, ErrorBox, Loading } from "@/components/States";
import {
  api,
  errorCodeOf,
  jsonInit,
  type AddedMember,
  type Me,
  type ProjectMember,
  type ProjectMembers,
  type QuotaItem,
} from "@/lib/api";

type Msg = { ok: boolean; text: string } | null;

/** "cannot remove the last owner" -> "Cannot remove the last owner." */
function sentence(text: string) {
  const t = text.trim();
  if (!t) return t;
  const cap = t.charAt(0).toUpperCase() + t.slice(1);
  return /[.!?]$/.test(cap) ? cap : `${cap}.`;
}

export default function ProjectPage() {
  const { id } = useParams<{ id: string }>();
  const [data, setData] = useState<ProjectMembers | null>(null);
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState("");
  const [addMsg, setAddMsg] = useState<Msg>(null);
  const [memberMsg, setMemberMsg] = useState<Msg>(null);
  const [quotaMsg, setQuotaMsg] = useState<Msg>(null);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<"member" | "owner">("member");
  const [adding, setAdding] = useState(false);
  const [quota, setQuota] = useState("");

  const refresh = useCallback(async () => {
    try {
      setData((await api(`/api/projects/${id}/members`)) as ProjectMembers);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load the project");
    }
  }, [id]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    (api("/api/me") as Promise<Me>).then(setMe).catch(() => setMe(null));
  }, []);

  useEffect(() => {
    if (data) setQuota(String(data.project.max_concurrent_jobs));
  }, [data]);

  /** Run a change, report the result through `say` (next to the control), then reload. */
  async function act<T>(run: () => Promise<T>, done: (result: T) => string, say: (m: Msg) => void): Promise<boolean> {
    say(null);
    let ok = false;
    try {
      const result = await run();
      say({ ok: true, text: done(result) });
      ok = true;
    } catch (err) {
      say({ ok: false, text: sentence(err instanceof Error ? err.message : "The change failed") });
    }
    await refresh();
    return ok;
  }

  const changeRole = (m: ProjectMember, next: string) => {
    setAddMsg(null);
    return act(
      () => api(`/api/projects/${id}/members/${m.user_id}`, jsonInit("PATCH", { role: next })),
      () => `${m.email} is now ${next === "owner" ? "an owner" : "a member"}.`,
      setMemberMsg,
    );
  };
  const remove = async (m: ProjectMember) => {
    setAddMsg(null);
    await act(
      () => api(`/api/projects/${id}/members/${m.user_id}`, { method: "DELETE" }),
      () => `Removed ${m.email}.`,
      setMemberMsg,
    );
  };

  async function add(e: React.FormEvent) {
    e.preventDefault();
    const target = email.trim().toLowerCase();
    setMemberMsg(null);
    if (!/^[^@\s]+@[^@\s]+$/.test(target)) return setAddMsg({ ok: false, text: "Enter a valid email address." });
    setAdding(true);
    const ok = await act(
      () => api(`/api/projects/${id}/members`, jsonInit("POST", { email: target, role })) as Promise<AddedMember>,
      (member) =>
        member?.invited
          ? `Invite sent to ${target}; they'll join when they first sign in.`
          : `Added ${target}.`,
      setAddMsg,
    ).catch(() => false);
    if (ok) setEmail("");
    setAdding(false);
  }

  async function saveQuota(e: React.FormEvent) {
    e.preventDefault();
    const n = Number(quota);
    if (!Number.isInteger(n) || n < 0 || n > 1000) {
      return setQuotaMsg({ ok: false, text: "The quota must be a whole number from 0 to 1000." });
    }
    await act(
      async () => (await api(`/api/admin/quotas/project/${id}`, jsonInit("PUT", { max_concurrent_jobs: n }))) as QuotaItem,
      () => "Quota saved.",
      setQuotaMsg,
    );
  }

  if (data === null) {
    return (
      <div className="stack">
        <h1>Project</h1>
        {error ? <ErrorBox message={error} onRetry={() => void refresh()} /> : <Loading label="Loading project…" />}
        <Link href="/projects">All projects</Link>
      </div>
    );
  }

  const columns: Column<ProjectMember>[] = [
    {
      key: "who",
      header: "Member",
      render: (m) => (
        <>
          {m.display_name}
          <div className="muted nodes-sub wrap-any">{m.email}</div>
        </>
      ),
    },
    {
      key: "role",
      header: "Role",
      render: (m) =>
        data.can_edit ? (
          <select aria-label={`Role of ${m.email}`} value={m.role} onChange={(e) => void changeRole(m, e.target.value)}>
            <option value="member">member</option>
            <option value="owner">owner</option>
          </select>
        ) : (
          <span className={`chip ${m.role}`}>{m.role}</span>
        ),
    },
    {
      key: "state",
      header: "",
      render: (m) => (
        <>
          {m.blocked ? <span className="chip blocked">blocked</span> : null}
          {m.invited ? <span className="chip invited">Invited — joins on first sign-in</span> : null}
        </>
      ),
    },
  ];
  if (data.can_edit) {
    columns.push({
      key: "actions",
      header: "",
      render: (m) => (
        <ConfirmButton className="ghost danger small" message={`Remove ${m.email}?`} onConfirm={() => remove(m)}>
          Remove
        </ConfirmButton>
      ),
    });
  }

  return (
    <div className="stack">
      <div>
        <p className="muted">
          <Link href="/projects">Projects</Link> /
        </p>
        <h1>{data.project.name}</h1>
        <p className="muted">
          You are {data.your_role === "owner" ? "an owner" : "a member"}.{" "}
          {data.can_edit ? "You can change members and roles." : "Only owners can change members."}
        </p>
      </div>
      {error ? <ErrorBox message={`Could not refresh: ${error}`} onRetry={() => void refresh()} /> : null}

      <div className="card stats">
        <div className="dash-stat">
          <div className="stat-label">Jobs at the same time (project quota)</div>
          <div className="stat-value tnum">{data.project.max_concurrent_jobs}</div>
        </div>
        {me?.is_admin ? (
          <form className="inline-form" onSubmit={saveQuota}>
            <label className="sr-only" htmlFor="project-quota">
              Project quota
            </label>
            <input id="project-quota" type="number" min={0} max={1000} value={quota} onChange={(e) => setQuota(e.target.value)} />
            <button type="submit" className="ghost small">
              Set quota
            </button>
          </form>
        ) : null}
        {quotaMsg ? (
          <p className={quotaMsg.ok ? "msg-ok" : "msg-err"} role={quotaMsg.ok ? "status" : "alert"}>
            {quotaMsg.text}
          </p>
        ) : null}
      </div>

      {data.can_edit ? (
        <form className="card filters" onSubmit={add}>
          <label htmlFor="member-email">
            Add by email
            <input
              id="member-email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="student@university.edu"
              aria-invalid={addMsg && !addMsg.ok ? true : undefined}
              aria-describedby={addMsg ? "member-add-msg" : undefined}
              required
            />
          </label>
          <label>
            Role
            <select value={role} onChange={(e) => setRole(e.target.value as "member" | "owner")}>
              <option value="member">member</option>
              <option value="owner">owner</option>
            </select>
          </label>
          <button type="submit" disabled={adding}>
            {adding ? "Adding…" : "Add member"}
          </button>
          {addMsg ? (
            <p id="member-add-msg" className={addMsg.ok ? "msg-ok" : "msg-err"} role={addMsg.ok ? "status" : "alert"}>
              {addMsg.text}
            </p>
          ) : null}
        </form>
      ) : null}

      <div className="card">
        {memberMsg ? (
          <p className={memberMsg.ok ? "msg-ok" : "msg-err"} role={memberMsg.ok ? "status" : "alert"} style={{ marginBottom: 8 }}>
            {memberMsg.text}
          </p>
        ) : null}
        <DataTable
          columns={columns}
          rows={data.items}
          rowKey={(m) => m.user_id}
          caption="Project members"
          empty={<Empty title="No members" />}
        />
      </div>
    </div>
  );
}
