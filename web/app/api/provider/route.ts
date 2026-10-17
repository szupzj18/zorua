import { connection } from "next/server";
import { guard } from "@/lib/guard";
import { readProvider, readRegistry, ZoruaError } from "@/lib/zorua";

const NAME = /^[A-Za-z0-9_-]{1,32}$/;

/**
 * POST so the same-origin guard also applies to provider metadata. Keys stay masked.
 */
export async function POST(request: Request) {
  await connection();
  const denied = guard(request, true);
  if (denied) return denied;
  let body: { name?: unknown; reveal?: unknown };
  try {
    body = await request.json();
  } catch {
    return Response.json({ error: "invalid JSON" }, { status: 400 });
  }
  if (typeof body.name !== "string" || !NAME.test(body.name)) {
    return Response.json({ error: "name is missing or invalid" }, { status: 400 });
  }
  try {
    const reg = await readRegistry();
    if (!reg.providers.some((p) => p.name === body.name)) {
      return Response.json({ error: `unknown provider '${body.name}'` }, { status: 400 });
    }
    const doc = await readProvider(body.name, body.reveal === true);
    return Response.json({ doc }, { headers: { "Cache-Control": "no-store" } });
  } catch (e) {
    if (e instanceof ZoruaError) return Response.json({ error: e.message }, { status: 422 });
    return Response.json({ error: "internal error" }, { status: 500 });
  }
}
