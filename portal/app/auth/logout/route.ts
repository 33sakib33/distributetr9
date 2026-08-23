import { NextRequest, NextResponse } from "next/server";

const CONTROL = process.env.CONTROL_API || "http://127.0.0.1:8080";

export async function GET(req: NextRequest) {
  await fetch(`${CONTROL}/auth/logout`, { cache: "no-store" }).catch(() => undefined);
  const res = NextResponse.redirect(new URL("/login", req.url));
  res.cookies.delete("lab_session");
  res.cookies.delete("lab_user");
  return res;
}
