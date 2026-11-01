import { NextRequest, NextResponse } from "next/server";
import { publicOrigin } from "@/lib/origin";

const CONTROL = process.env.CONTROL_API || "http://127.0.0.1:8080";

export async function GET(req: NextRequest) {
  await fetch(`${CONTROL}/auth/logout`, { cache: "no-store" }).catch(() => undefined);
  // Back to the origin the browser came from, not a fixed host.
  const res = NextResponse.redirect(new URL("/login", publicOrigin(req)));
  res.cookies.delete("lab_session");
  res.cookies.delete("lab_user");
  return res;
}
