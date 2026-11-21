# Garmin connector for Claude

A remote MCP server on Cloudflare Workers that lets Claude (chat, mobile, Cowork, Claude Code)
read your Garmin data on demand. It calls the same official, read-only Garmin Personal API as
`../pull.py`, fetches only what a question needs, and trims Garmin's verbose records to the
fields that matter, so most answers need one or two calls of about 0.2 s each.

It speaks the current MCP protocol (2026-07-28, which Claude tries first) and the 2025 versions,
through the official v2 SDK (`@modelcontextprotocol/server`), statelessly: every request gets a
fresh server instance, so nothing is kept between calls.

## Tools

| Tool | What it returns | Default range |
| --- | --- | --- |
| `garmin_today` | Go hard / keep it easy / recover, with the same rules as the dashboard: last night's sleep, HRV vs your normal range, resting HR vs 30-day average, Body Battery, last 3 days of training | today |
| `garmin_sleep` | Per night: duration, score, bedtime, wake, stages, overnight HR, stress, respiration, SpO2, sleep need | 7 nights |
| `garmin_hrv` | Per night: last-night average, 5-min high, weekly average, normal range, status | 14 nights |
| `garmin_daily` | Per day: steps, resting HR, stress, Body Battery, intensity minutes, calories | 7 days |
| `garmin_activities` | Sessions with pace or speed, HR, zones, training effect, power, strength sets; or totals per week or month | 14 days |
| `garmin_activity` | One activity in full (min and recovery HR, temperature) | |
| `garmin_weight` | Weigh-ins in kg with body composition if your scale records it | 365 days |
| `garmin_profile` | Fitness age and its levers, HR zone floors | |

Ranges take `start_date`, `end_date` or `days` (up to 2 years). Sleep, HRV and daily take
`group_by: "week" | "month"`, and ranges over 120 days are grouped by week automatically.
Dates are India time (`TZ` in `wrangler.jsonc`).

## How it is secured

- Your Garmin key is a Worker secret. It is never in this repo, and the Worker only uses it to
  get a 24 h Garmin token, cached in KV for 20 h.
- `/mcp` requires an OAuth token (OAuth 2.1 with PKCE, via `@cloudflare/workers-oauth-provider`).
  Claude registers itself automatically, then sends you to `/authorize`. That page asks for
  **your connector password** (the `CONNECTOR_PASSWORD` secret) before it issues a token.
- Only redirects back to `claude.ai`, `claude.com` or an app on your own computer are accepted,
  so the page can't be used to send a token anywhere else (`ALLOWED_REDIRECT_HOSTS`).
- 10 wrong passwords lock sign-in for an hour.
- Tokens, codes and client secrets are stored in KV only as hashes. Access tokens last 1 hour.
  Claude refreshes them, and a grant stays valid while it is used at least once every 30 days.
- **To sign every client out, change `CONNECTOR_PASSWORD`.** Each grant records a fingerprint
  of the password it was approved with, and `/mcp` rejects grants from an older password.

## Deploy

You need a free Cloudflare account and Node 20 or newer.

```sh
cd garmin/connector
npm ci
npx wrangler login                 # opens a browser; or set CLOUDFLARE_API_TOKEN
npx wrangler deploy                # creates the KV namespace and prints the URL
npx wrangler secret put GARMIN_PERSONAL_API_KEY
npx wrangler secret put CONNECTOR_PASSWORD     # 12+ characters; you type it when Claude connects
```

The URL is `https://garmin-mcp.<your-subdomain>.workers.dev`. On a new Cloudflare account,
open **Workers & Pages** in the dashboard once first, so it creates your `workers.dev` subdomain.

Secrets can also be set in the dashboard: **Workers & Pages → garmin-mcp → Settings →
Variables and Secrets → Add → Secret**. Until `CONNECTOR_PASSWORD` is set, sign-in is refused.

## Add it to Claude

1. In Claude, open **Settings → Connectors**. If an older "Garmin" custom connector is there,
   remove it (or edit its URL).
2. **Add custom connector**, name it Garmin, URL `https://garmin-mcp.<your-subdomain>.workers.dev/mcp`.
3. **Connect**. A page from your Worker opens: enter your connector password and press **Allow**.

The connector then works in every Claude app on your account.

## Upkeep

- **The Garmin key expires after a year.** Tool calls then report that Garmin refused the key.
  Create a new one at <https://connect.garmin.com/app/settings/personalApiToken> and run
  `npx wrangler secret put GARMIN_PERSONAL_API_KEY` again (or set it in the dashboard).
- Logs: `npx wrangler tail`, or **Observability** in the dashboard. They contain request
  paths and errors, not your data.
- Redeploy after changing code: `npx wrangler deploy`.

## Limits

- Cloudflare's free plan allows 100,000 requests a day and 10 ms of CPU per request. Measured
  with real data, typical calls take 1–3 ms and a full year of daily summaries about 4–7 ms.
  If you ever see "exceeded CPU" errors on very long ranges, ask for `group_by: "month"` over a
  shorter span, or move to Workers Paid ($5 a month).
- Garmin rejects responses over about 256 KB. The Worker splits ranges and halves them on that
  error, as `pull.py` does.
- The data is whatever has synced to Garmin Connect, not what is on the watch.
- The Personal API doesn't offer training readiness, training status, race predictions or
  per-lap splits, so neither does this connector.

## Local test

```sh
# .dev.vars (gitignored) holds GARMIN_PERSONAL_API_KEY=... and CONNECTOR_PASSWORD=...
npx wrangler dev
CONNECTOR_PASSWORD=... npm run e2e         # in another terminal
```

`test/e2e.mjs` runs the same sequence as claude.ai: discovery, client registration, the
password page (wrong password, a non-Claude redirect, then the right password), token
exchange, every tool, the 2026-07-28 protocol through the official v2 client, and token refresh.
It prints timings and sizes, not your data (`--show` prints the data). Point it at a deployed Worker with `node test/e2e.mjs https://...workers.dev`.

## Files

| Path | |
| --- | --- |
| `src/index.ts` | Entry point: OAuth provider and the stateless MCP endpoint |
| `src/auth.ts` | `/authorize` password and consent page |
| `src/garmin.ts` | Garmin client: token cache, retries, range splitting |
| `src/tools.ts` | MCP tools and the readiness rules |
| `wrangler.jsonc` | Worker config (no secrets) |
