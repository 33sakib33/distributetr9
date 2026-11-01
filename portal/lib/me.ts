import { cookies } from "next/headers";
import type { Me } from "@/lib/api";

const CONTROL = process.env.CONTROL_API || "http://127.0.0.1:8080";

/** The signed-in user for this request (server side), or null. */
export async function loadMe(): Promise<Me | null> {
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
