import { NextRequest } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const CONTROL = process.env.CONTROL_API || "http://127.0.0.1:8080";

async function proxy(req: NextRequest, path: string[]) {
  const dest = `${CONTROL}/${path.join("/")}${req.nextUrl.search}`;
  const headers = new Headers();
  const session = req.cookies.get("lab_session")?.value;
  const devUser = req.cookies.get("lab_user")?.value;
  if (session) {
    headers.set("Cookie", `lab_session=${session}`);
    headers.set("Authorization", `Bearer ${session}`);
  } else if (devUser) {
    headers.set("X-Dev-User", devUser);
  }
  const contentType = req.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);
  const accept = req.headers.get("accept");
  if (accept) headers.set("accept", accept);
  headers.set("X-Request-ID", req.headers.get("x-request-id") || crypto.randomUUID());
  const lastEvent = req.headers.get("last-event-id");
  if (lastEvent) headers.set("Last-Event-ID", lastEvent);

  const init: RequestInit = { method: req.method, headers, redirect: "manual" };
  if (req.method !== "GET" && req.method !== "HEAD") {
    init.body = req.body;
    Object.assign(init, { duplex: "half" });
  }

  const res = await fetch(dest, init);
  const out = new Headers();
  for (const key of ["content-type", "content-disposition", "cache-control", "x-accel-buffering", "location", "x-request-id"]) {
    const value = res.headers.get(key);
    if (value) out.set(key, value);
  }
  return new Response(res.body, { status: res.status, headers: out });
}

type Ctx = { params: Promise<{ path?: string[] }> };

export async function GET(req: NextRequest, ctx: Ctx) {
  return proxy(req, (await ctx.params).path || []);
}
export async function POST(req: NextRequest, ctx: Ctx) {
  return proxy(req, (await ctx.params).path || []);
}
export async function PUT(req: NextRequest, ctx: Ctx) {
  return proxy(req, (await ctx.params).path || []);
}
export async function PATCH(req: NextRequest, ctx: Ctx) {
  return proxy(req, (await ctx.params).path || []);
}
export async function DELETE(req: NextRequest, ctx: Ctx) {
  return proxy(req, (await ctx.params).path || []);
}
