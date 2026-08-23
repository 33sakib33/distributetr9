import type { Metadata } from "next";
import { cookies } from "next/headers";
import Link from "next/link";
import "./globals.css";
import type { Me } from "@/lib/api";

export const metadata: Metadata = {
  title: "Lab jobs",
  description: "Campus lab ML job portal",
};

const CONTROL = process.env.CONTROL_API || "http://127.0.0.1:8080";

async function loadMe(): Promise<Me | null> {
  const jar = await cookies();
  const session = jar.get("lab_session")?.value;
  const email = jar.get("lab_user")?.value;
  const headers: Record<string, string> = {};
  if (session) {
    headers.Cookie = `lab_session=${session}`;
    headers.Authorization = `Bearer ${session}`;
  } else if (email) {
    headers["X-Dev-User"] = email;
  } else {
    return null;
  }
  try {
    const res = await fetch(`${CONTROL}/me`, {
      headers,
      cache: "no-store",
    });
    if (!res.ok) return null;
    return (await res.json()) as Me;
  } catch {
    return null;
  }
}

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const me = await loadMe();
  return (
    <html lang="en">
      <body>
        <div className="shell">
          <header className="top">
            <div className="brand">
              Lab <span>jobs</span>
            </div>
            <nav>
              <Link href="/">Queue</Link>
              <Link href="/submit">Submit</Link>
              {me?.is_admin ? <Link href="/admin">Nodes</Link> : null}
              {me?.email ? (
                <>
                  <span className="muted">{me.email}</span>
                  <Link href="/auth/logout">Sign out</Link>
                </>
              ) : (
                <Link href="/login">Sign in</Link>
              )}
            </nav>
          </header>
          {children}
        </div>
      </body>
    </html>
  );
}
