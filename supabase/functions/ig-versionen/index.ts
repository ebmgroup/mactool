// IG Versionen — Steuerpult-Schnittstelle fürs Dashboard (läuft im Kontroll-Projekt).
//
// Das Dashboard hat kein eigenes Backend. Damit "nur Admins laden hoch" und "jeder
// Mitarbeiter darf updaten" echte Regeln sind und keine Optik, geht jeder Schreibzugriff
// hier durch:
//   1. Login prüfen: JWT der Dashboard-Datenbank (Haupt-DB) gegen deren /auth/v1/user
//   2. Rolle lesen: user_roles der Haupt-DB, mit dem Token des Nutzers selbst
//   3. Erst dann mit dem Service-Key dieses Projekts schreiben
//
// Aktionen (POST JSON {action, ...}):
//   list                       alle   Versionen, Handys, Aufträge, Mac-Status
//   request_update             alle   {server_name, serial?, package?}
//   cancel_job                 alle   {id}  (nur queued/running)
//   upload_start               Admin  {version_name, version_code, package, abi, file_name, bytes, sha256, parts:[{bytes, sha256}]}
//   upload_finish              Admin  {id}
//   set_standard               Admin  {id}
//   delete_version             Admin  {id}  (nicht die Standard-Version)
//
// Secrets: MAIN_SUPABASE_URL, MAIN_SUPABASE_ANON_KEY (öffentlicher Schlüssel der Haupt-DB).
// SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY stellt Supabase selbst bereit.

const URL_SELF = Deno.env.get("SUPABASE_URL")!;
const SERVICE = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const MAIN_URL = Deno.env.get("MAIN_SUPABASE_URL")!;
const MAIN_ANON = Deno.env.get("MAIN_SUPABASE_ANON_KEY")!;

const BUCKET = "apk";
const PART_BYTES = 40 * 1024 * 1024;
const CLONE_RE = /^com\.instagram\.androi[e-z]$/;
const ORIGINAL = "com.instagram.android";

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, content-type, apikey, x-client-info",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

class HttpError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { ...cors, "Content-Type": "application/json" } });
}

// ── Kontroll-DB (Service-Key) ───────────────────────────────────────────

async function db(method: string, path: string, body?: unknown, prefer = "return=representation") {
  const res = await fetch(`${URL_SELF}/rest/v1/${path}`, {
    method,
    headers: {
      apikey: SERVICE,
      Authorization: `Bearer ${SERVICE}`,
      "Content-Type": "application/json",
      Prefer: prefer,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  if (!res.ok) throw new HttpError(502, `Datenbank: ${res.status} ${text.slice(0, 300)}`);
  return text ? JSON.parse(text) : null;
}

async function storage(method: string, path: string, body?: BodyInit, contentType = "application/json") {
  const res = await fetch(`${URL_SELF}/storage/v1/${path}`, {
    method,
    headers: { apikey: SERVICE, Authorization: `Bearer ${SERVICE}`, "Content-Type": contentType, "x-upsert": "true" },
    body,
  });
  const text = await res.text();
  if (!res.ok) throw new HttpError(502, `Speicher: ${res.status} ${text.slice(0, 300)}`);
  return text ? JSON.parse(text) : null;
}

// ── Wer ruft? ───────────────────────────────────────────────────────────

async function caller(req: Request): Promise<{ email: string; admin: boolean }> {
  const token = (req.headers.get("Authorization") || "").replace(/^Bearer\s+/i, "");
  if (!token) throw new HttpError(401, "Nicht angemeldet");
  const userRes = await fetch(`${MAIN_URL}/auth/v1/user`, {
    headers: { apikey: MAIN_ANON, Authorization: `Bearer ${token}` },
  });
  if (!userRes.ok) throw new HttpError(401, "Anmeldung ungültig oder abgelaufen");
  const user = await userRes.json();
  const rolesRes = await fetch(`${MAIN_URL}/rest/v1/user_roles?select=role&user_id=eq.${user.id}`, {
    headers: { apikey: MAIN_ANON, Authorization: `Bearer ${token}` },
  });
  const roles: { role: string }[] = rolesRes.ok ? await rolesRes.json() : [];
  const admin = roles.some((r) => r.role === "admin" || r.role === "super_admin");
  return { email: user.email || user.id, admin };
}

function requireAdmin(who: { admin: boolean }) {
  if (!who.admin) throw new HttpError(403, "Nur Admins und Superadmins dürfen Versionen verwalten");
}

const SERVER_RE = /^[\w-]+$/;
const SERIAL_RE = /^[\w.:-]+$/;

// ── Aktionen ────────────────────────────────────────────────────────────

async function list(who: { email: string; admin: boolean }) {
  const [versions, phones, jobs, agents] = await Promise.all([
    db("GET", "ig_versions?select=*&order=created_at.desc"),
    db("GET", "ig_phones?select=*&order=server_name.asc,name.asc"),
    db("GET", "ig_update_jobs?select=*&order=created_at.desc&limit=300"),
    db("GET", "mac_agents?select=server_name,last_seen,version"),
  ]);
  // Die Uhr der Function geht richtig — anders als die der Macs
  return { me: who, versions, phones, jobs, agents, server_time: new Date().toISOString() };
}

async function requestUpdate(body: any, who: { email: string }) {
  const server = String(body.server_name || "");
  const serial = body.serial ? String(body.serial) : null;
  const pkg = body.package ? String(body.package) : null;
  if (!SERVER_RE.test(server)) throw new HttpError(400, "Mac fehlt");
  if (serial && !SERIAL_RE.test(serial)) throw new HttpError(400, "Ungültige Seriennummer");
  if (pkg && !(pkg === ORIGINAL || CLONE_RE.test(pkg))) throw new HttpError(400, "Nur Instagram-Original oder -Kopien");
  if (pkg && !serial) throw new HttpError(400, "Einzelne App nur zusammen mit einem Handy");
  const [standard] = await db("GET", "ig_versions?select=id&is_standard=is.true&status=eq.ready");
  if (!standard) throw new HttpError(409, "Es ist keine Standard-Version festgelegt");
  // Gleichen Auftrag nicht doppelt einreihen
  let dup = `ig_update_jobs?select=id&server_name=eq.${server}&status=in.(queued,running)`;
  dup += serial ? `&serial=eq.${serial}` : "&serial=is.null";
  dup += pkg ? `&package=eq.${pkg}` : "&package=is.null";
  const existing = await db("GET", dup);
  if (existing.length) return { job: existing[0], duplicate: true };
  const [job] = await db("POST", "ig_update_jobs", {
    server_name: server, serial, package: pkg, version_id: standard.id, requested_by: who.email,
  });
  return { job };
}

async function cancelJob(body: any) {
  const id = Number(body.id);
  const rows = await db("PATCH", `ig_update_jobs?id=eq.${id}&status=in.(queued,running)`, {
    status: "cancelled", progress: "abgebrochen (Dashboard)",
  });
  if (!rows.length) throw new HttpError(409, "Auftrag läuft nicht mehr");
  return { job: rows[0] };
}

async function uploadStart(body: any, who: { email: string }) {
  const name = String(body.version_name || "");
  const code = Number(body.version_code);
  if (!/^[\d.]+$/.test(name) || !Number.isFinite(code) || code <= 0) throw new HttpError(400, "Version ungültig");
  if (body.package !== ORIGINAL) throw new HttpError(400, "Die Datei ist kein Instagram (com.instagram.android)");
  const parts = Array.isArray(body.parts) ? body.parts : [];
  if (!parts.length || parts.some((p: any) => !/^[0-9a-f]{64}$/.test(p.sha256) || p.bytes > PART_BYTES)) {
    throw new HttpError(400, "Teile ungültig");
  }
  const abi = String(body.abi || "").replace(/[^\w-]/g, "") || "unbekannt";
  const release = `instagram-${name}-${abi}-${Date.now()}`;
  const [version] = await db("POST", "ig_versions", {
    release, version_name: name, version_code: code, abi, package: ORIGINAL,
    file_name: String(body.file_name || "instagram.apk").replace(/[^\w.()-]/g, "_"),
    bytes: Number(body.bytes), sha256: String(body.sha256), status: "uploading",
    uploaded_by: who.email, parts,
  });
  const uploads = [];
  for (let i = 0; i < parts.length; i++) {
    const path = `${release}/part${String(i).padStart(3, "0")}`;
    const signed = await storage("POST", `object/upload/sign/${BUCKET}/${path}`, "{}");
    uploads.push({ path, url: `${URL_SELF}/storage/v1${signed.url}` });
  }
  return { version, uploads };
}

async function uploadFinish(body: any) {
  const id = Number(body.id);
  const [version] = await db("GET", `ig_versions?select=*&id=eq.${id}`);
  if (!version) throw new HttpError(404, "Version nicht gefunden");
  if (version.status === "ready") return { version };
  const parts = version.parts || [];
  const listed = await storage("POST", `object/list/${BUCKET}`, JSON.stringify({ prefix: version.release, limit: 1000 }));
  const names = new Set((listed || []).map((o: any) => o.name));
  const missing = parts.map((_: any, i: number) => `part${String(i).padStart(3, "0")}`).filter((n: string) => !names.has(n));
  if (missing.length) throw new HttpError(409, `Teile fehlen noch: ${missing.join(", ")}`);
  // Format wie tools/apk_upload.py — die Macs prüfen jede Prüfsumme selbst
  const manifest = {
    file: version.file_name, bytes: version.bytes, sha256: version.sha256,
    parts: parts.map((p: any, i: number) => ({ name: `${version.release}/part${String(i).padStart(3, "0")}`, ...p })),
  };
  await storage("POST", `object/${BUCKET}/${version.release}/manifest.json`, JSON.stringify(manifest));
  const [ready] = await db("PATCH", `ig_versions?id=eq.${id}`, { status: "ready" });
  return { version: ready };
}

async function setStandard(body: any) {
  const id = Number(body.id);
  const [version] = await db("GET", `ig_versions?select=id,status&id=eq.${id}`);
  if (!version || version.status !== "ready") throw new HttpError(409, "Nur fertig hochgeladene Versionen");
  await db("PATCH", "ig_versions?is_standard=is.true", { is_standard: false });
  const [standard] = await db("PATCH", `ig_versions?id=eq.${id}`, { is_standard: true });
  return { version: standard };
}

async function deleteVersion(body: any) {
  const id = Number(body.id);
  const [version] = await db("GET", `ig_versions?select=*&id=eq.${id}`);
  if (!version) throw new HttpError(404, "Version nicht gefunden");
  if (version.is_standard) throw new HttpError(409, "Die Standard-Version kann nicht gelöscht werden");
  const used = await db("GET", `ig_update_jobs?select=id&version_id=eq.${id}&status=in.(queued,running)`);
  if (used.length) throw new HttpError(409, "Version wird gerade ausgerollt");
  const listed = await storage("POST", `object/list/${BUCKET}`, JSON.stringify({ prefix: version.release, limit: 1000 }));
  const prefixes = (listed || []).map((o: any) => `${version.release}/${o.name}`);
  if (prefixes.length) await storage("DELETE", `object/${BUCKET}`, JSON.stringify({ prefixes }));
  await db("PATCH", `ig_update_jobs?version_id=eq.${id}`, { version_id: null }, "return=minimal");
  await db("DELETE", `ig_versions?id=eq.${id}`, undefined, "return=minimal");
  return { deleted: id };
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  try {
    if (req.method !== "POST") throw new HttpError(405, "Nur POST");
    const body = await req.json().catch(() => ({}));
    const who = await caller(req);
    switch (body.action) {
      case "list": return json(await list(who));
      case "request_update": return json(await requestUpdate(body, who));
      case "cancel_job": return json(await cancelJob(body));
      case "upload_start": requireAdmin(who); return json(await uploadStart(body, who));
      case "upload_finish": requireAdmin(who); return json(await uploadFinish(body));
      case "set_standard": requireAdmin(who); return json(await setStandard(body));
      case "delete_version": requireAdmin(who); return json(await deleteVersion(body));
      default: throw new HttpError(400, `Unbekannte Aktion '${body.action}'`);
    }
  } catch (e) {
    const status = e instanceof HttpError ? e.status : 500;
    return json({ error: e instanceof Error ? e.message : String(e) }, status);
  }
});
