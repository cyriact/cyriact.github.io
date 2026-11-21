// Garmin Personal API client: the same calls as garmin/pull.py, for a Worker.
// Read-only. The key is exchanged for a bearer token that is cached for 20 hours.

const BASE = "https://personal.connectapi.garmin.com";
const TOKEN_URL = BASE + "/personalapitoken-service/personal-api/personal-api-key/token";
const TOKEN_KV_KEY = "garmin:bearer";
const TOKEN_TTL_S = 20 * 3600;
const CONCURRENCY = 4;

export interface GarminEnv {
  GARMIN_PERSONAL_API_KEY: string;
  OAUTH_KV: KVNamespace;
}

export class GarminError extends Error {}

/** HTTP 500 with an empty body: the response was over ~256 KB. */
class TooBig extends Error {}

let memo: { token: string; until: number } | null = null;

async function getToken(env: GarminEnv, fresh = false): Promise<string> {
  const now = Date.now();
  if (!fresh && memo && memo.until > now) return memo.token;
  if (!fresh) {
    const cached = await env.OAUTH_KV.get(TOKEN_KV_KEY);
    if (cached) {
      memo = { token: cached, until: now + 3600_000 };
      return cached;
    }
  }
  if (!env.GARMIN_PERSONAL_API_KEY) throw new GarminError("GARMIN_PERSONAL_API_KEY is not set on the Worker.");
  const r = await fetch(TOKEN_URL, { method: "POST", headers: { Authorization: "ApiKey " + env.GARMIN_PERSONAL_API_KEY } });
  if (!r.ok) {
    throw new GarminError(
      `Garmin refused the API key (HTTP ${r.status}). It is missing, mistyped or expired: ` +
        "create a new one in Garmin Connect and update the Worker secret.",
    );
  }
  const token = (await r.text()).trim();
  await env.OAUTH_KV.put(TOKEN_KV_KEY, token, { expirationTtl: TOKEN_TTL_S });
  memo = { token, until: now + TOKEN_TTL_S * 1000 };
  return token;
}

const sleep = (ms: number) => new Promise((res) => setTimeout(res, ms));

/** GET one path. Returns null on 404 or an empty body. */
export async function apiGet(env: GarminEnv, path: string): Promise<any> {
  let token = await getToken(env);
  let refreshed = false;
  for (let attempt = 0; ; attempt++) {
    const r = await fetch(BASE + path, { headers: { Authorization: "Bearer " + token, Accept: "application/json" } });
    if (r.ok) {
      const body = await r.text();
      return body ? JSON.parse(body) : null;
    }
    if (r.status === 401 && !refreshed) {
      token = await getToken(env, true);
      refreshed = true;
      continue;
    }
    if (r.status === 500) throw new TooBig(path);
    if (r.status === 404) return null;
    if ([429, 502, 503].includes(r.status) && attempt < 3) {
      await sleep(800 * (attempt + 1));
      continue;
    }
    throw new GarminError(`Garmin API error: HTTP ${r.status} on ${path.split("?")[0]}`);
  }
}

// ---- dates ---------------------------------------------------------------------

export function todayIn(tz: string): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
}

export function addDays(iso: string, n: number): string {
  const d = new Date(iso + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

export function daysBetween(a: string, b: string): number {
  return Math.round((Date.parse(b + "T00:00:00Z") - Date.parse(a + "T00:00:00Z")) / 86400_000);
}

function* ranges(start: string, end: string, days: number): Generator<[string, string]> {
  let cur = start;
  while (cur <= end) {
    let stop = addDays(cur, days - 1);
    if (stop > end) stop = end;
    yield [cur, stop];
    cur = addDays(stop, 1);
  }
}

async function pool<T, R>(items: T[], n: number, fn: (x: T) => Promise<R>): Promise<R[]> {
  const out: R[] = new Array(items.length);
  let next = 0;
  await Promise.all(
    Array.from({ length: Math.min(n, items.length) }, async () => {
      while (next < items.length) {
        const i = next++;
        out[i] = await fn(items[i]);
      }
    }),
  );
  return out;
}

type PathFn = (a: string, b: string) => string;

/** GET one date range, halving it when Garmin says the response is too big. */
async function fetchRange(env: GarminEnv, make: PathFn, a: string, b: string): Promise<any[]> {
  try {
    return [await apiGet(env, make(a, b))];
  } catch (e) {
    if (!(e instanceof TooBig)) throw e;
    if (a === b) return [];
    const mid = addDays(a, Math.floor(daysBetween(a, b) / 2));
    const [x, y] = await Promise.all([fetchRange(env, make, a, mid), fetchRange(env, make, addDays(mid, 1), b)]);
    return [...x, ...y];
  }
}

/** Fetch [start, end] in chunks of `chunk` days, a few at a time, and return items keyed by date. */
export async function fetchDated(
  env: GarminEnv,
  make: PathFn,
  extract: (resp: any) => any[] | undefined,
  dateKey: string,
  start: string,
  end: string,
  chunk: number,
): Promise<Map<string, any>> {
  const parts = await pool([...ranges(start, end, chunk)], CONCURRENCY, ([a, b]) => fetchRange(env, make, a, b));
  const out = new Map<string, any>();
  for (const resps of parts) {
    for (const resp of resps) {
      for (const item of extract(resp) || []) {
        const d = item?.[dateKey];
        if (d) out.set(String(d).slice(0, 10), item);
      }
    }
  }
  return out;
}

// ---- endpoints -----------------------------------------------------------------

export const getSleep = (env: GarminEnv, start: string, end: string) =>
  fetchDated(
    env,
    (a, b) => `/sleep-service/sleep/dailySleepSummariesByDate?startDate=${a}&endDate=${b}`,
    (r) => (Array.isArray(r) ? r.filter((s) => s?.sleepTimeSeconds) : []),
    "calendarDate",
    start,
    end,
    31,
  );

export const getDaily = (env: GarminEnv, start: string, end: string) =>
  fetchDated(
    env,
    (a, b) => `/usersummary-service/v2/dailysummaries?startDate=${a}&endDate=${b}`,
    (r) => r?.data,
    "calendarDate",
    start,
    end,
    45,
  );

export const getHrv = (env: GarminEnv, start: string, end: string) =>
  fetchDated(env, (a, b) => `/hrv-service/hrv/daily/${a}/${b}`, (r) => r?.hrvSummaries, "calendarDate", start, end, 365);

export const getWeight = (env: GarminEnv, start: string, end: string) =>
  fetchDated(
    env,
    (a, b) => `/weight-service/weight/range/${a}/${b}`,
    (r) => r?.dailyWeightSummaries,
    "summaryDate",
    start,
    end,
    365,
  );

export const getActivity = (env: GarminEnv, id: number) => apiGet(env, `/activity-service/activity/${id}`);
export const getFitnessAge = (env: GarminEnv, day: string) => apiGet(env, `/fitnessage-service/fitnessage/v2/${day}`);
export const getHrZones = (env: GarminEnv) => apiGet(env, "/biometric-service/heartRateZones/v2");

const PAGE = 40;

/** Activities newest first, from the newest back to `since` (inclusive), at most `max`. */
export async function getActivities(env: GarminEnv, since: string, until: string, max: number): Promise<any[]> {
  const out: any[] = [];
  for (let start = 0; start < 2000; start += PAGE) {
    let page: any[];
    try {
      page = (await apiGet(env, `/activitylist-service/activities/search/activities?start=${start}&limit=${PAGE}`)) || [];
    } catch (e) {
      if (!(e instanceof TooBig)) throw e;
      // A page of unusually large activities: fetch it in two halves.
      const half = PAGE / 2;
      const a = (await apiGet(env, `/activitylist-service/activities/search/activities?start=${start}&limit=${half}`)) || [];
      const b = (await apiGet(env, `/activitylist-service/activities/search/activities?start=${start + half}&limit=${half}`)) || [];
      page = [...a, ...b];
    }
    for (const a of page) {
      const d = String(a.startTimeLocal || "").slice(0, 10);
      if (d > until) continue;
      if (d < since) return out;
      out.push(a);
      if (out.length >= max) return out;
    }
    if (page.length < PAGE) return out;
  }
  return out;
}
