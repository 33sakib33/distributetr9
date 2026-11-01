import type { NextRequest } from "next/server";

/** The origin the browser used, also behind a proxy that rewrites the Host header. */
export function publicOrigin(req: NextRequest): string {
  const host = req.headers.get("x-forwarded-host") ?? req.headers.get("host");
  if (!host) return req.nextUrl.origin;
  const proto = req.headers.get("x-forwarded-proto")?.split(",")[0].trim() || req.nextUrl.protocol.replace(":", "");
  return `${proto}://${host.split(",")[0].trim()}`;
}
