// The sign-in and consent page at /authorize. One owner, one password (a Worker
// secret). claude.ai sends you here when you add the connector; you type the
// password, and the OAuth library issues claude.ai a token for /mcp.

import { AuthorizationError, CimdFetchError, type ConsentDescription, type OAuthHelpers } from "@cloudflare/workers-oauth-provider";

export interface AuthEnv {
  OAUTH_PROVIDER: OAuthHelpers;
  OAUTH_KV: KVNamespace;
  CONNECTOR_PASSWORD?: string;
  ALLOWED_REDIRECT_HOSTS?: string;
}

export const SCOPE = "garmin:read";
const OWNER = "owner";
const MAX_FAILS_PER_HOUR = 10;
const FAILS_KEY = "login:fails";

const escape = (v: string) => v.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

function page(title: string, body: string, status = 200, headers = new Headers()): Response {
  headers.set("Content-Type", "text/html; charset=utf-8");
  headers.set("Cache-Control", "no-store");
  headers.set("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'");
  headers.set("X-Frame-Options", "DENY");
  headers.set("Referrer-Policy", "no-referrer");
  const html = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>${escape(title)}</title>
<style>
  :root { color-scheme: light dark; --bg: #f6f5f2; --card: #fff; --ink: #1c1b19; --muted: #6b6862; --line: #e3e0da; --accent: #0f766e; --warn: #b45309; }
  @media (prefers-color-scheme: dark) { :root { --bg: #151514; --card: #1f1e1c; --ink: #eceae6; --muted: #a29e97; --line: #34322f; --accent: #2dd4bf; --warn: #f59e0b; } }
  * { box-sizing: border-box; }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: var(--bg); color: var(--ink);
         font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; padding: 16px; }
  main { width: 100%; max-width: 420px; background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 28px 24px; }
  h1 { font-size: 1.25rem; margin: 0 0 4px; }
  p { margin: 12px 0; color: var(--muted); }
  strong { color: var(--ink); }
  .warn { color: var(--warn); }
  label { display: block; font-weight: 600; margin: 20px 0 6px; }
  input[type=password] { width: 100%; font: inherit; padding: 10px 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--bg); color: var(--ink); }
  input[type=password]:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
  .row { display: flex; gap: 10px; margin-top: 20px; }
  button, a.btn { flex: 1; font: inherit; font-weight: 600; padding: 10px; border-radius: 8px; border: 1px solid var(--line); cursor: pointer;
                  background: transparent; color: var(--ink); text-align: center; text-decoration: none; }
  button.primary, a.btn { background: var(--accent); border-color: var(--accent); color: #fff; }
  @media (prefers-color-scheme: dark) { button.primary, a.btn { color: #062925; } }
</style></head>
<body><main>${body}</main></body></html>`;
  return new Response(html, { status, headers });
}

const errorPage = (msg: string, status = 400, retry?: string) =>
  page(
    "Garmin connector",
    `<h1>Garmin connector</h1><p>${escape(msg)}</p>${retry ? `<div class="row"><a class="btn" href="${escape(retry)}">Try again</a></div>` : ""}`,
    status,
  );

function allowedRedirect(env: AuthEnv, d: ConsentDescription): boolean {
  if (d.redirectIsLoopback) return true; // Claude Code and other local clients
  const hosts = (env.ALLOWED_REDIRECT_HOSTS || "claude.ai,claude.com").split(",").map((h) => h.trim().toLowerCase()).filter(Boolean);
  const host = d.redirectHost.toLowerCase();
  return hosts.some((h) => host === h || host.endsWith("." + h));
}

async function digest(s: string): Promise<ArrayBuffer> {
  return crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
}

/** A short fingerprint of the password, stored in each grant. Changing the password signs every client out. */
export async function passwordVersion(password: string): Promise<string> {
  const d = new Uint8Array(await digest("garmin-connector-grant:" + password));
  return [...d.slice(0, 8)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function passwordMatches(given: string, expected: string): Promise<boolean> {
  const [a, b] = await Promise.all([digest(given), digest(expected)]);
  return crypto.subtle.timingSafeEqual(a, b);
}

function consentForm(d: ConsentDescription, handle: string, retryPath: string): string {
  const who = d.clientDomain
    ? `<strong>${escape(d.clientName)}</strong> (published by <strong>${escape(d.clientDomain)}</strong>)`
    : `<strong>${escape(d.clientName)}</strong>`;
  return `<h1>Connect your Garmin data</h1>
<p>${who} wants read-only access to your Garmin sleep, HRV, activities and daily stats.</p>
<p>Access will be sent to <strong>${escape(d.redirectHost)}</strong>.</p>
${d.redirectIsLoopback ? `<p class="warn">This sends access to an app on your own computer. Continue only if you just started connecting from it.</p>` : ""}
<form method="post" action="/authorize">
  <input type="hidden" name="handle" value="${escape(handle)}">
  <input type="hidden" name="retry" value="${escape(retryPath)}">
  <label for="pw">Connector password</label>
  <input id="pw" type="password" name="password" autocomplete="current-password" required autofocus>
  <div class="row">
    <button type="submit" name="decision" value="deny" formnovalidate>Deny</button>
    <button type="submit" name="decision" value="approve" class="primary">Allow</button>
  </div>
</form>`;
}

export async function handleAuthorize(request: Request, env: AuthEnv): Promise<Response> {
  const oauth = env.OAUTH_PROVIDER;
  try {
    if (request.method === "GET") {
      if (!env.CONNECTOR_PASSWORD || env.CONNECTOR_PASSWORD.length < 12) {
        return errorPage("This connector has no password yet. Set the CONNECTOR_PASSWORD secret (12+ characters) on the Worker, then try again.", 503);
      }
      const authRequest = await oauth.parseAuthRequest(request);
      const details = await oauth.describeConsent(authRequest);
      if (!allowedRedirect(env, details)) {
        return errorPage(`Sign-in from ${details.redirectHost} isn't allowed. Only claude.ai and apps on this computer can connect.`, 403);
      }
      const consent = await oauth.beginConsent(authRequest);
      const url = new URL(request.url);
      return page("Connect Garmin", consentForm(details, consent.handle, url.pathname + url.search), 200, consent.headers);
    }

    if (request.method !== "POST") return new Response("Method not allowed", { status: 405 });

    const form = await request.formData();
    const handle = String(form.get("handle") || "");
    const retryRaw = String(form.get("retry") || "");
    const retry = retryRaw.startsWith("/authorize?") ? retryRaw : undefined;

    if (form.get("decision") !== "approve") {
      const denied = await oauth.denyConsent(request, handle);
      return new Response(null, { status: 302, headers: denied.headers });
    }

    const fails = Number((await env.OAUTH_KV.get(FAILS_KEY)) || 0);
    if (fails >= MAX_FAILS_PER_HOUR) {
      return errorPage("Too many wrong passwords. Sign-in is locked for up to an hour.", 429);
    }
    const expected = env.CONNECTOR_PASSWORD || "";
    if (expected.length < 12 || !(await passwordMatches(String(form.get("password") || ""), expected))) {
      await env.OAUTH_KV.put(FAILS_KEY, String(fails + 1), { expirationTtl: 3600 });
      return errorPage("Wrong password.", 401, retry);
    }

    const approved = await oauth.approveConsent(request, handle, { scope: [SCOPE] });
    const { redirectTo } = await oauth.completeAuthorization({
      request: approved.request,
      userId: OWNER,
      metadata: {},
      scope: [SCOPE],
      props: { user: OWNER, pwv: await passwordVersion(expected) },
    });
    approved.headers.set("Location", redirectTo);
    return new Response(null, { status: 302, headers: approved.headers });
  } catch (error) {
    if (error instanceof AuthorizationError && error.redirectTo) {
      return Response.redirect(error.redirectTo, 302);
    }
    if (error instanceof AuthorizationError) {
      return errorPage(error.description || "This sign-in link has expired or was already used. Start connecting again from Claude.");
    }
    if (error instanceof CimdFetchError) {
      return errorPage("This app could not be verified.");
    }
    throw error;
  }
}
