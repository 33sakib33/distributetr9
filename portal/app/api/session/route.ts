import { NextRequest, NextResponse } from "next/server";

const CONTROL = process.env.CONTROL_API || "http://127.0.0.1:8080";

export async function POST(req: NextRequest) {
  const body = await req.json().catch(() => ({}));
  const email = String(body.email || "").trim().toLowerCase();
  if (!email.includes("@")) {
    return NextResponse.json({ error: "Enter an email address" }, { status: 400 });
  }
  const upstream = await fetch(`${CONTROL}/auth/local`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ email, name: body.name || "" }),
    cache: "no-store",
  });
  const data = await upstream.json().catch(() => ({}));
  if (!upstream.ok) {
    return NextResponse.json(data, { status: upstream.status });
  }
  const token = String(data.token || "");
  if (!token) {
    return NextResponse.json({ error: "Could not sign in" }, { status: 502 });
  }
  const res = NextResponse.json({ email: data.email || email });
  res.cookies.set("lab_session", token, {
    path: "/",
    httpOnly: true,
    sameSite: "lax",
    maxAge: 60 * 60 * 12,
  });
  res.cookies.delete("lab_user");
  return res;
}
