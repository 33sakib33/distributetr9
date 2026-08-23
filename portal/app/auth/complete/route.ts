import { NextRequest, NextResponse } from "next/server";

const CONTROL = process.env.CONTROL_API || "http://127.0.0.1:8080";

export async function GET(req: NextRequest) {
  const ticket = req.nextUrl.searchParams.get("ticket") || "";
  const next = req.nextUrl.searchParams.get("next") || "/";
  if (!ticket) {
    return NextResponse.redirect(new URL("/login?error=missing+ticket", req.url));
  }
  const exchange = await fetch(`${CONTROL}/auth/exchange`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ ticket }),
    cache: "no-store",
  });
  if (!exchange.ok) {
    return NextResponse.redirect(new URL("/login?error=login+expired", req.url));
  }
  const body = (await exchange.json()) as { token?: string };
  if (!body.token) {
    return NextResponse.redirect(new URL("/login?error=login+expired", req.url));
  }
  const dest = next.startsWith("/") ? new URL(next, req.url) : new URL("/", req.url);
  const res = NextResponse.redirect(dest);
  res.cookies.set("lab_session", body.token, {
    path: "/",
    httpOnly: true,
    sameSite: "lax",
    maxAge: 60 * 60 * 12,
  });
  res.cookies.delete("lab_user");
  return res;
}
