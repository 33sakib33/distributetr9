"use client";

import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { Loading } from "@/components/States";

type AuthConfig = {
  local_login: boolean;
  campus_login: boolean;
  login_url: string | null;
};

function Form() {
  const router = useRouter();
  const params = useSearchParams();
  const [email, setEmail] = useState("alice@uni.edu");
  const [error, setError] = useState(params.get("error") || "");
  const [config, setConfig] = useState<AuthConfig | null>(null);
  const [origin, setOrigin] = useState("");

  useEffect(() => {
    setOrigin(window.location.origin);
    fetch("/api/auth/config")
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (!data) {
          setConfig({ local_login: true, campus_login: false, login_url: null });
          return;
        }
        setConfig({
          local_login: data.local_login ?? data.dev_auth ?? true,
          campus_login: data.campus_login ?? false,
          login_url: data.login_url ?? null,
        });
      })
      .catch(() => setConfig({ local_login: true, campus_login: false, login_url: null }));
  }, []);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    const res = await fetch("/api/session", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ email }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      setError(body.message || body.error || "Could not sign in");
      return;
    }
    router.push("/");
    router.refresh();
  }

  const campusHref =
    config?.campus_login && config.login_url && origin
      ? `${config.login_url}?next=${encodeURIComponent(origin + "/")}`
      : "";

  return (
    <div className="card stack" style={{ maxWidth: 420 }}>
      <h1>Sign in</h1>
      <p className="muted">Use your university email. Jobs you submit will belong to this account.</p>
      {error ? <p className="err" role="alert">{error}</p> : null}
      {campusHref ? (
        <a className="btn" href={campusHref}>
          Sign in with university
        </a>
      ) : null}
      {(config?.local_login ?? true) ? (
        <form className="stack" onSubmit={onSubmit}>
          <label>
            Email
            <input value={email} onChange={(e) => setEmail(e.target.value)} type="email" required />
          </label>
          <button type="submit">Sign in</button>
        </form>
      ) : null}
      {!config ? <Loading label="Loading sign-in options…" /> : null}
    </div>
  );
}

export default function LoginForm() {
  return (
    <Suspense fallback={<Loading />}>
      <Form />
    </Suspense>
  );
}
