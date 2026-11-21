// End-to-end check of the connector, the way claude.ai uses it:
// discovery -> client registration -> password sign-in -> token -> MCP tool calls.
//
//   npx wrangler dev            (in another terminal, with .dev.vars holding both secrets)
//   CONNECTOR_PASSWORD=... node test/e2e.mjs [base-url]
//
// Prints timings and response sizes, never the data itself (pass --show to print it).

import { createHash, randomBytes } from "node:crypto";

const BASE = (process.argv.find((a) => a.startsWith("http")) || "http://127.0.0.1:8787").replace(/\/$/, "");
const SHOW = process.argv.includes("--show");
const PASSWORD = process.env.CONNECTOR_PASSWORD;
if (!PASSWORD) throw new Error("Set CONNECTOR_PASSWORD");

const b64url = (b) => Buffer.from(b).toString("base64url");
let failures = 0;
const check = (cond, msg) => {
  console.log(`${cond ? "ok  " : "FAIL"} ${msg}`);
  if (!cond) failures++;
};

// Cookies for the consent step (a tiny jar: name=value only).
const jar = new Map();
const keep = (res) => {
  for (const c of res.headers.getSetCookie?.() ?? []) {
    const [pair] = c.split(";");
    const i = pair.indexOf("=");
    jar.set(pair.slice(0, i), pair.slice(i + 1));
  }
};
const cookie = () => [...jar].map(([k, v]) => `${k}=${v}`).join("; ");

// 1. Discovery
let r = await fetch(BASE + "/mcp", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
check(r.status === 401, `unauthenticated /mcp is refused (${r.status})`);
check(/resource_metadata=/.test(r.headers.get("www-authenticate") || ""), "401 points at protected resource metadata");
const prm = await (await fetch(BASE + "/.well-known/oauth-protected-resource/mcp")).json();
check(prm.resource === BASE + "/mcp", `resource metadata names ${prm.resource}`);
const asm = await (await fetch(BASE + "/.well-known/oauth-authorization-server")).json();
check(!!asm.registration_endpoint && !!asm.token_endpoint, "authorization server metadata has register and token endpoints");

// 2. Dynamic client registration (what claude.ai does)
async function register(redirect) {
  const res = await fetch(asm.registration_endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ client_name: "e2e test", redirect_uris: [redirect], token_endpoint_auth_method: "none", grant_types: ["authorization_code", "refresh_token"], response_types: ["code"] }),
  });
  return res.json();
}
const REDIRECT = "http://127.0.0.1:9/callback";
const client = await register(REDIRECT);
check(!!client.client_id, "client registered");

// 3. Authorization page
const verifier = b64url(randomBytes(32));
const challenge = b64url(createHash("sha256").update(verifier).digest());
const authUrl = (cid, redirect) =>
  `${asm.authorization_endpoint}?` +
  new URLSearchParams({ response_type: "code", client_id: cid, redirect_uri: redirect, code_challenge: challenge, code_challenge_method: "S256", state: "s123", scope: "garmin:read", resource: BASE + "/mcp" });

async function openConsent() {
  const res = await fetch(authUrl(client.client_id, REDIRECT), { redirect: "manual" });
  keep(res);
  const html = await res.text();
  return { res, html, handle: /name="handle" value="([^"]+)"/.exec(html)?.[1], retry: /name="retry" value="([^"]+)"/.exec(html)?.[1] };
}
let page = await openConsent();
check(page.res.status === 200 && !!page.handle, `consent page renders (${page.res.status})`);
check(/frame-ancestors 'none'/.test(page.res.headers.get("content-security-policy") || ""), "consent page can't be framed");

const submit = (handle, password, decision = "approve", retry = "") =>
  fetch(asm.authorization_endpoint, {
    method: "POST",
    redirect: "manual",
    headers: { "Content-Type": "application/x-www-form-urlencoded", Cookie: cookie(), Origin: BASE },
    body: new URLSearchParams({ handle, password, decision, retry: retry.replace(/&amp;/g, "&") }),
  });

r = await submit(page.handle, "wrong-password-xyz", "approve", page.retry);
check(r.status === 401, `wrong password is refused (${r.status})`);

// A redirect to an unknown site is refused even with the right password.
const evil = await register("https://evil.example/callback");
r = await fetch(authUrl(evil.client_id, "https://evil.example/callback"), { redirect: "manual" });
check(r.status === 403, `sign-in for a non-Claude redirect host is refused (${r.status})`);

page = await openConsent();
r = await submit(page.handle, PASSWORD);
const loc = r.headers.get("location") || "";
check(r.status === 302 && loc.startsWith(REDIRECT), `right password redirects back to the client (${r.status})`);
const code = new URL(loc).searchParams.get("code");
check(new URL(loc).searchParams.get("state") === "s123", "state is passed back");

// 4. Token
r = await fetch(asm.token_endpoint, {
  method: "POST",
  headers: { "Content-Type": "application/x-www-form-urlencoded" },
  body: new URLSearchParams({ grant_type: "authorization_code", code, redirect_uri: REDIRECT, client_id: client.client_id, code_verifier: verifier, resource: BASE + "/mcp" }),
});
const tok = await r.json();
check(!!tok.access_token && !!tok.refresh_token, `token issued (${r.status}), expires in ${tok.expires_in}s`);
if (process.env.E2E_SAVE_TOKEN) (await import("node:fs")).writeFileSync(process.env.E2E_SAVE_TOKEN, tok.access_token, { mode: 0o600 });

// 5. MCP
let id = 0;
async function rpc(method, params, timeIt) {
  const t = performance.now();
  const res = await fetch(BASE + "/mcp", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream", Authorization: "Bearer " + tok.access_token, "MCP-Protocol-Version": "2025-11-25" },
    body: JSON.stringify({ jsonrpc: "2.0", id: ++id, method, params }),
  });
  const text = await res.text();
  const ms = performance.now() - t;
  let body;
  try {
    body = JSON.parse(text);
  } catch {
    body = { raw: text.slice(0, 300) };
  }
  return { status: res.status, body, ms, bytes: text.length };
}

let x = await rpc("initialize", { protocolVersion: "2025-11-25", capabilities: {}, clientInfo: { name: "e2e", version: "1" } });
check(x.status === 200 && x.body.result?.serverInfo?.name === "garmin", `initialize (${x.status}, protocol ${x.body.result?.protocolVersion})`);
x = await rpc("tools/list", {});
const tools = (x.body.result?.tools || []).map((t) => t.name);
check(tools.length === 8, `tools/list: ${tools.join(", ")}`);

const calls = [
  ["garmin_today", {}],
  ["garmin_sleep", {}],
  ["garmin_sleep", { days: 365, group_by: "month" }],
  ["garmin_hrv", { days: 90 }],
  ["garmin_daily", { days: 30 }],
  ["garmin_activities", {}],
  ["garmin_activities", { days: 182, group_by: "week" }],
  ["garmin_activities", { days: 90, sport: "Run" }],
  ["garmin_weight", {}],
  ["garmin_profile", {}],
  ["garmin_sleep", { start_date: "2026-13-01" }],
];
let firstActivity;
console.log("\n  tool call                                         time    size");
for (const [name, args] of calls) {
  x = await rpc("tools/call", { name, arguments: args });
  const res = x.body.result;
  const text = res?.content?.[0]?.text || JSON.stringify(x.body.error || x.body);
  const expectError = name === "garmin_sleep" && args.start_date === "2026-13-01";
  check(x.status === 200 && (expectError ? res?.isError : !res?.isError), `${(name + " " + JSON.stringify(args)).padEnd(46)} ${(x.ms / 1000).toFixed(2)}s ${(text.length / 1024).toFixed(1).padStart(5)} KB`);
  if (!expectError && res?.isError) console.log("     ", text);
  if (name === "garmin_activities" && !firstActivity) firstActivity = JSON.parse(text).activities?.[0]?.id;
  if (SHOW) console.log(text, "\n");
}
if (firstActivity) {
  x = await rpc("tools/call", { name: "garmin_activity", arguments: { activity_id: firstActivity } });
  check(x.status === 200 && !x.body.result?.isError, `garmin_activity ${(x.ms / 1000).toFixed(2)}s`);
  if (SHOW) console.log(x.body.result?.content?.[0]?.text);
}

// 6. Refresh
r = await fetch(asm.token_endpoint, {
  method: "POST",
  headers: { "Content-Type": "application/x-www-form-urlencoded" },
  body: new URLSearchParams({ grant_type: "refresh_token", refresh_token: tok.refresh_token, client_id: client.client_id, resource: BASE + "/mcp" }),
});
check(r.status === 200 && !!(await r.json()).access_token, "refresh token works");

console.log(failures ? `\n${failures} check(s) failed` : "\nall checks passed");
process.exit(failures ? 1 : 0);
