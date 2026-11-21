// MCP tools. Each one fetches only what the question needs and trims Garmin's
// verbose records to the fields that matter, with units in the field names.

import { McpServer } from "@modelcontextprotocol/server";
import { CfWorkerJsonSchemaValidator } from "@modelcontextprotocol/server/validators/cf-worker";
import { z } from "zod";
import {
  GarminEnv,
  GarminError,
  addDays,
  daysBetween,
  getActivities,
  getActivity,
  getDaily,
  getFitnessAge,
  getHrZones,
  getHrv,
  getSleep,
  getWeight,
  todayIn,
} from "./garmin";

export interface ToolEnv extends GarminEnv {
  TZ: string;
}

// Same thresholds and sport mapping as garmin/build.py, so answers match the dashboard.
const SLEEP_FLOOR_H = 7.0;
const SPORT: Record<string, string> = {
  running: "Run", treadmill_running: "Run", trail_running: "Run", track_running: "Run",
  cycling: "Bike", indoor_cycling: "Bike", virtual_ride: "Bike", road_biking: "Bike",
  mountain_biking: "Bike", gravel_cycling: "Bike",
  lap_swimming: "Swim", open_water_swimming: "Swim",
  strength_training: "Strength",
};
const NOT_TRAINING = new Set(["walking", "breathwork", "meditation", "incident_detected"]);
const MAX_DAYS = 731;
const PER_DAY_LIMIT = 120; // longer ranges come back grouped by week

// ---- small helpers -------------------------------------------------------------

const num = (x: unknown) => (typeof x === "number" && Number.isFinite(x) ? x : null);
const round = (x: unknown, dp = 0) => {
  const n = num(x);
  if (n === null) return null;
  const f = 10 ** dp;
  return Math.round(n * f) / f;
};
const hours = (s: unknown) => round((num(s) ?? NaN) / 3600, 2);
const mins = (s: unknown) => round((num(s) ?? NaN) / 60, 1);
const mean = (xs: (number | null | undefined)[]) => {
  const v = xs.filter((x): x is number => typeof x === "number" && Number.isFinite(x));
  return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null;
};
/** Garmin encodes local wall-clock time as an epoch, so read it as UTC. */
const clock = (ms: unknown) => (num(ms) ? new Date(ms as number).toISOString().slice(11, 16) : null);
/** Bedtime as hours relative to midnight: 23:30 -> -0.5, 00:45 -> 0.75. */
const bedHours = (ms: unknown) => {
  if (!num(ms)) return null;
  const d = new Date(ms as number);
  const h = d.getUTCHours() + d.getUTCMinutes() / 60;
  return h > 15 ? h - 24 : h;
};
const hm = (h: number | null) => {
  if (h === null) return null;
  const t = ((h % 24) + 24) % 24;
  const m = Math.round(t * 60) % 1440;
  return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
};
const pace = (secPerUnit: number | null) =>
  secPerUnit === null || !Number.isFinite(secPerUnit) || secPerUnit <= 0
    ? null
    : `${Math.floor(secPerUnit / 60)}:${String(Math.round(secPerUnit % 60)).padStart(2, "0")}`;

/** Drop nulls, undefined and empty arrays so responses stay small. */
const compact = (o: any): any => {
  if (Array.isArray(o)) return o.map(compact);
  if (o && typeof o === "object") {
    const out: any = {};
    for (const [k, v] of Object.entries(o)) {
      if (v === null || v === undefined || (Array.isArray(v) && v.length === 0)) continue;
      out[k] = compact(v);
    }
    return out;
  }
  return o;
};

const ok = (data: unknown) => ({ content: [{ type: "text" as const, text: JSON.stringify(compact(data)) }] });
const fail = (msg: string) => ({ content: [{ type: "text" as const, text: msg }], isError: true });

function mondayOf(iso: string) {
  const dow = (new Date(iso + "T00:00:00Z").getUTCDay() + 6) % 7;
  return addDays(iso, -dow);
}
const periodOf = (iso: string, by: "week" | "month") => (by === "week" ? mondayOf(iso) : iso.slice(0, 7));

const ISO = /^\d{4}-\d{2}-\d{2}$/;

/** Resolve start/end/days into a validated [start, end] range. */
function resolveRange(env: ToolEnv, a: { start_date?: string; end_date?: string; days?: number }, defaultDays: number) {
  const today = todayIn(env.TZ);
  for (const v of [a.start_date, a.end_date]) {
    const t = v === undefined ? NaN : Date.parse(v + "T00:00:00Z");
    const valid = v !== undefined && ISO.test(v) && !Number.isNaN(t) && new Date(t).toISOString().slice(0, 10) === v;
    if (v !== undefined && !valid) throw new GarminError(`Dates must be real dates as YYYY-MM-DD, got "${v}".`);
  }
  const end = a.end_date && a.end_date < today ? a.end_date : today;
  const start = a.start_date ?? addDays(end, -((a.days ?? defaultDays) - 1));
  if (start > end) throw new GarminError("start_date is after end_date.");
  if (daysBetween(start, end) + 1 > MAX_DAYS) throw new GarminError(`Ask for at most ${MAX_DAYS} days at a time.`);
  return { start, end, today };
}

function pickGrouping(start: string, end: string, group_by?: "day" | "week" | "month") {
  const n = daysBetween(start, end) + 1;
  if (group_by && group_by !== "day") return { by: group_by, note: undefined };
  if (n > PER_DAY_LIMIT) {
    return { by: "week" as const, note: `Grouped by week because ${n} days is more than ${PER_DAY_LIMIT}. Ask for group_by "month" for less detail, or a shorter range for per-day values.` };
  }
  return { by: "day" as const, note: undefined };
}

function periodsBetween(start: string, end: string, by: "week" | "month"): string[] {
  const out: string[] = [];
  if (by === "week") {
    for (let d = mondayOf(start); d <= end; d = addDays(d, 7)) out.push(d);
  } else {
    for (let d = start.slice(0, 7) + "-01"; d <= end; d = addDays(d, 32).slice(0, 7) + "-01") out.push(d.slice(0, 7));
  }
  return out;
}

/** Aggregate rows per week or month. With `fill`, periods with no rows are included (as agg([])). */
function groupRows<T extends { date: string }>(rows: T[], by: "week" | "month", agg: (rows: T[]) => object, fill?: { start: string; end: string }) {
  const groups = new Map<string, T[]>();
  for (const k of fill ? periodsBetween(fill.start, fill.end, by) : []) groups.set(k, []);
  for (const r of rows) {
    const k = periodOf(r.date, by);
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k)!.push(r);
  }
  return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([k, rs]) => ({ [by]: k, ...agg(rs) }));
}

const avg = (rows: any[], key: string, dp = 1) => round(mean(rows.map((r) => r[key])), dp);

// ---- shaping -------------------------------------------------------------------

function shapeNight(s: any) {
  const sc = s.sleepScores || {};
  return {
    date: s.calendarDate as string,
    sleep_h: hours(s.sleepTimeSeconds),
    score: num(sc.overall?.value),
    quality: sc.overall?.qualifierKey ?? null,
    bedtime: clock(s.sleepStartTimestampLocal),
    wake: clock(s.sleepEndTimestampLocal),
    deep_h: hours(s.deepSleepSeconds),
    light_h: hours(s.lightSleepSeconds),
    rem_h: hours(s.remSleepSeconds),
    awake_h: hours(s.awakeSleepSeconds),
    awakenings: num(s.awakeCount),
    avg_hr_bpm: round(s.avgHeartRate),
    avg_stress: round(s.avgSleepStress),
    respiration_brpm: round(s.averageRespirationValue, 1),
    spo2_avg_pct: round(s.averageSpO2Value),
    nap_h: s.napTimeSeconds ? hours(s.napTimeSeconds) : null,
    sleep_need_h: s.sleepNeed?.actual ? round(s.sleepNeed.actual / 60, 2) : null,
    feedback: s.sleepScoreFeedback ?? null,
    finalised: s.sleepWindowConfirmed ?? null,
    _bed: bedHours(s.sleepStartTimestampLocal),
  };
}

function shapeHrv(h: any) {
  return {
    date: h.calendarDate as string,
    last_night_ms: num(h.lastNightAvg),
    high_5min_ms: num(h.lastNight5MinHigh),
    weekly_avg_ms: num(h.weeklyAvg),
    normal_low_ms: num(h.baseline?.balancedLow),
    normal_high_ms: num(h.baseline?.balancedUpper),
    status: h.status ?? null,
  };
}

function shapeDay(d: any) {
  const bb = d.bodyBattery || {};
  const st = d.stress || {};
  return {
    date: d.calendarDate as string,
    steps: num(d.movement?.steps?.value),
    resting_hr_bpm: num(d.heartRate?.restingValue),
    min_hr_bpm: num(d.heartRate?.minValue),
    max_hr_bpm: num(d.heartRate?.maxValue),
    stress_avg: num(st.avgLevel) !== null && st.avgLevel >= 0 ? st.avgLevel : null,
    stress_max: num(st.maxLevel),
    body_battery_high: num(bb.maxValue),
    body_battery_low: num(bb.minValue),
    body_battery_charged: num(bb.chargedValue),
    body_battery_drained: num(bb.drainedValue),
    body_battery_latest: num(bb.latestValue),
    intensity_moderate_min: num(d.intensityMinutes?.moderate),
    intensity_vigorous_min: num(d.intensityMinutes?.vigorous),
    active_kcal: num(d.calories?.burnedActive),
    total_kcal: num(d.calories?.burnedTotal),
    floors: num(d.movement?.floorsAscended?.value),
    respiration_avg_brpm: num(d.respiration?.avgValue),
    spo2_avg_pct: num(d.pulseOx?.avgValue),
  };
}

function shapeActivity(a: any) {
  const t = a.activityType?.typeKey ?? a.activityTypeDTO?.typeKey ?? "unknown";
  const s = a.summaryDTO ?? a; // detail endpoint nests the numbers under summaryDTO
  const sport = SPORT[t] ?? "Other";
  const km = num(s.distance) ? round(s.distance / 1000, 2) : null;
  const dur = num(s.duration);
  const moving = num(s.movingDuration);
  const speed = num(s.averageMovingSpeed) ?? num(s.averageSpeed); // m/s
  const isFoot = sport === "Run" || ["walking", "hiking"].includes(t);
  const zones = [1, 2, 3, 4, 5].map((z) => mins(a[`hrTimeInZone_${z}`]) ?? 0);
  const sets = (a.summarizedExerciseSets || [])
    .filter((x: any) => x.sets || x.reps)
    .map((x: any) => ({
      exercise: String(x.category || "").toLowerCase(),
      sets: num(x.sets),
      reps: num(x.reps),
      max_kg: x.maxWeight ? round(x.maxWeight / 1000, 1) : null,
    }));
  return {
    id: a.activityId,
    start: String(s.startTimeLocal || a.startTimeLocal || "").slice(0, 16).replace("T", " "),
    name: a.activityName ?? null,
    type: t,
    sport,
    training: !NOT_TRAINING.has(t),
    km,
    duration_min: mins(dur),
    moving_min: moving && dur && Math.abs(moving - dur) > 30 ? mins(moving) : null,
    pace_per_km: isFoot && speed ? pace(1000 / speed) : null,
    pace_per_100m: sport === "Swim" && speed ? pace(100 / speed) : null,
    speed_kmh: sport === "Bike" && speed ? round(speed * 3.6, 1) : null,
    avg_hr_bpm: round(s.averageHR),
    max_hr_bpm: round(s.maxHR),
    hr_zone_min: zones.some((z) => z > 0) ? zones : null,
    aerobic_te: round(s.aerobicTrainingEffect ?? s.trainingEffect, 1),
    anaerobic_te: round(s.anaerobicTrainingEffect, 1),
    te_label: s.trainingEffectLabel ?? null,
    kcal: round(s.calories),
    elevation_gain_m: round(s.elevationGain),
    cadence_spm: round(a.averageRunningCadenceInStepsPerMinute),
    avg_power_w: round(s.avgPower),
    norm_power_w: round(s.normPower),
    swolf: round(a.averageSwolf),
    pool_m: num(a.poolLength) ? round(a.poolLength / 100, 0) : null, // Garmin stores pool length in cm
    sets: sets.length ? sets : null,
    body_battery_change: num(s.differenceBodyBattery),
    recovery_hr_bpm: num(s.recoveryHeartRate),
    pr: a.pr || a.isPR || null,
    location: a.locationName ?? null,
  };
}

// ---- readiness (same rules as the dashboard) ----------------------------------

type Level = "good" | "warning" | "critical";

function readiness(night: any, hrv: any, day: any, rhr30: number | null) {
  const checks: { check: string; value: string; level: Level }[] = [];
  if (night?.sleep_h != null) {
    const h = night.sleep_h;
    checks.push({ check: "Sleep", value: `${h.toFixed(1)} h`, level: h >= SLEEP_FLOOR_H ? "good" : h >= 6 ? "warning" : "critical" });
  }
  if (hrv?.last_night_ms != null && hrv.normal_low_ms) {
    const v = hrv.last_night_ms;
    const lo = hrv.normal_low_ms;
    checks.push({
      check: "HRV",
      value: `${v} ms (normal ${lo}–${hrv.normal_high_ms})`,
      level: v >= lo ? "good" : v >= lo - 6 ? "warning" : "critical",
    });
  }
  if (day?.resting_hr_bpm != null && rhr30) {
    const diff = day.resting_hr_bpm - rhr30;
    checks.push({
      check: "Resting HR",
      value: `${day.resting_hr_bpm} bpm (${diff >= 0 ? "+" : ""}${diff.toFixed(0)} vs 30-day avg)`,
      level: diff <= 2 ? "good" : diff <= 5 ? "warning" : "critical",
    });
  }
  if (day?.body_battery_high != null) {
    const b = day.body_battery_high;
    checks.push({ check: "Body Battery", value: `${b} peak`, level: b >= 75 ? "good" : b >= 50 ? "warning" : "critical" });
  }
  const levels = checks.map((c) => c.level);
  let verdict: string, advice: string;
  if (levels.includes("critical") || levels.filter((l) => l === "warning").length >= 3) {
    verdict = "Recover";
    advice = "Rest or an easy 20–30 min. Prioritise an early night.";
  } else if (levels.includes("warning")) {
    verdict = "Keep it easy";
    advice = "Zone 2 or technique work. Save the hard session.";
  } else {
    verdict = "Go hard";
    advice = "Recovered. Good day for intervals or a heavy lift.";
  }
  return { verdict: checks.length ? verdict : "Unknown", advice: checks.length ? advice : "No sleep, HRV or daily data yet today.", checks };
}

// ---- server --------------------------------------------------------------------

const RANGE = {
  start_date: z.string().optional().describe("First day, YYYY-MM-DD (user's local date)."),
  end_date: z.string().optional().describe("Last day, YYYY-MM-DD. Defaults to today."),
  days: z.number().int().min(1).max(MAX_DAYS).optional().describe("Instead of start_date: this many days ending on end_date."),
};
const GROUP = z
  .enum(["day", "week", "month"])
  .optional()
  .describe(`Per-day rows (default, up to ${PER_DAY_LIMIT} days) or averages per week (Monday start) or month.`);

const READ_ONLY = { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: true };

function guarded<A>(fn: (a: A) => Promise<unknown>) {
  return async (a: A) => {
    try {
      return ok(await fn(a));
    } catch (e) {
      if (e instanceof GarminError) return fail(e.message);
      console.error(e);
      return fail("Could not reach Garmin right now: " + (e instanceof Error ? e.message : String(e)));
    }
  };
}

export function buildServer(env: ToolEnv): McpServer {
  const server = new McpServer(
    { name: "garmin", version: "1.0.0" },
    {
      jsonSchemaValidator: new CfWorkerJsonSchemaValidator(),
      instructions:
        `Read-only access to the user's Garmin Connect data (official Garmin Personal API). ` +
        `Dates are the user's local dates in ${env.TZ}. Units are in the field names (_h hours, _min minutes, _ms milliseconds, _bpm beats per minute, km). ` +
        `Sleep is keyed by the morning the user woke up. Data appears once the watch has synced to Garmin Connect. ` +
        `For "how am I doing today / should I train hard" use garmin_today first. For trends over months use group_by "week" or "month".`,
    },
  );

  server.registerTool(
    "garmin_today",
    {
      title: "Today's readiness",
      description:
        "Readiness for today: last night's sleep, HRV against the user's normal range, resting HR against the 30-day average, " +
        "Body Battery, a go-hard / keep-it-easy / recover call (same rules as the user's dashboard), and the last 3 days of training.",
      inputSchema: z.object({}),
      annotations: READ_ONLY,
    },
    guarded(async () => {
      const today = todayIn(env.TZ);
      const [sleep, hrv, daily, acts] = await Promise.all([
        getSleep(env, addDays(today, -1), today),
        getHrv(env, addDays(today, -6), today),
        getDaily(env, addDays(today, -31), today),
        getActivities(env, addDays(today, -2), today, 20),
      ]);
      // The latest day with sleep or HRV, as the dashboard does (today, or yesterday if today hasn't synced).
      const day = [today, addDays(today, -1)].find((d) => sleep.has(d) || hrv.has(d)) ?? today;
      const night = sleep.has(day) ? shapeNight(sleep.get(day)) : null;
      const h = hrv.has(day) ? shapeHrv(hrv.get(day)) : null;
      const d = daily.has(day) ? shapeDay(daily.get(day)) : null;
      const prior = [...daily.entries()].filter(([k]) => k < day && k >= addDays(day, -30)).map(([, v]) => v.heartRate?.restingValue);
      const rhr30 = mean(prior);
      const { _bed, ...nightOut } = night ?? ({} as any);
      return {
        today,
        data_for: day,
        synced_today: sleep.has(today),
        ...readiness(night, h, d, rhr30),
        sleep: night ? nightOut : null,
        hrv: h,
        resting_hr_30d_avg_bpm: round(rhr30, 1),
        day: d,
        recent_training: acts.map(shapeActivity).filter((a) => a.training),
      };
    }),
  );

  server.registerTool(
    "garmin_sleep",
    {
      title: "Sleep",
      description:
        "Sleep per night (date = the morning woken up): duration, sleep score, bedtime and wake time, stages, overnight HR, stress, " +
        "respiration, SpO2, sleep need. Defaults to the last 7 nights. Long ranges can be grouped by week or month.",
      inputSchema: z.object({ ...RANGE, group_by: GROUP }),
      annotations: READ_ONLY,
    },
    guarded(async (a: any) => {
      const { start, end } = resolveRange(env, a, 7);
      const nights = [...(await getSleep(env, start, end)).values()].map(shapeNight).sort((x, y) => x.date.localeCompare(y.date));
      const { by, note } = pickGrouping(start, end, a.group_by);
      const summary = {
        nights: nights.length,
        avg_sleep_h: avg(nights, "sleep_h", 2),
        avg_score: avg(nights, "score", 0),
        avg_bedtime: hm(mean(nights.map((n) => n._bed))),
        nights_7h_plus: nights.filter((n) => (n.sleep_h ?? 0) >= SLEEP_FLOOR_H).length,
        avg_deep_h: avg(nights, "deep_h", 2),
        avg_rem_h: avg(nights, "rem_h", 2),
      };
      const rows =
        by === "day"
          ? nights.map(({ _bed, ...n }) => n)
          : groupRows(nights, by, (rs) => ({
              nights: rs.length,
              avg_sleep_h: avg(rs, "sleep_h", 2),
              avg_score: avg(rs, "score", 0),
              avg_bedtime: hm(mean(rs.map((n) => n._bed))),
              nights_7h_plus: rs.filter((n) => (n.sleep_h ?? 0) >= SLEEP_FLOOR_H).length,
              avg_deep_h: avg(rs, "deep_h", 2),
              avg_rem_h: avg(rs, "rem_h", 2),
              avg_overnight_hr_bpm: avg(rs, "avg_hr_bpm", 0),
            }));
      return { start, end, note, summary, [by === "day" ? "nights" : by + "s"]: rows };
    }),
  );

  server.registerTool(
    "garmin_hrv",
    {
      title: "HRV",
      description:
        "Overnight heart-rate variability per night: last-night average, 5-minute high, 7-day average, the user's normal (balanced) range, and status. " +
        "Defaults to the last 14 nights.",
      inputSchema: z.object({ ...RANGE, group_by: GROUP }),
      annotations: READ_ONLY,
    },
    guarded(async (a: any) => {
      const { start, end } = resolveRange(env, a, 14);
      const rows = [...(await getHrv(env, start, end)).values()].map(shapeHrv).sort((x, y) => x.date.localeCompare(y.date));
      const { by, note } = pickGrouping(start, end, a.group_by);
      const latest = rows[rows.length - 1];
      const summary = {
        nights: rows.length,
        avg_ms: avg(rows, "last_night_ms", 0),
        latest_normal_range_ms: latest ? `${latest.normal_low_ms}–${latest.normal_high_ms}` : null,
        nights_below_normal: rows.filter((r) => r.last_night_ms != null && r.normal_low_ms != null && r.last_night_ms < r.normal_low_ms).length,
      };
      const out =
        by === "day"
          ? rows
          : groupRows(rows, by, (rs) => ({
              nights: rs.length,
              avg_ms: avg(rs, "last_night_ms", 0),
              normal_low_ms: avg(rs, "normal_low_ms", 0),
              normal_high_ms: avg(rs, "normal_high_ms", 0),
              nights_below_normal: rs.filter((r) => r.last_night_ms != null && r.normal_low_ms != null && r.last_night_ms < r.normal_low_ms).length,
            }));
      return { start, end, note, summary, [by === "day" ? "nights" : by + "s"]: out };
    }),
  );

  server.registerTool(
    "garmin_daily",
    {
      title: "Daily summaries",
      description:
        "Per day: steps, resting HR, min/max HR, stress, Body Battery (high, low, charged, drained, latest), intensity minutes, calories, floors, " +
        "respiration, SpO2. Defaults to the last 7 days.",
      inputSchema: z.object({ ...RANGE, group_by: GROUP }),
      annotations: READ_ONLY,
    },
    guarded(async (a: any) => {
      const { start, end } = resolveRange(env, a, 7);
      const rows = [...(await getDaily(env, start, end)).values()]
        .filter((d) => d.includesWellnessData !== false)
        .map(shapeDay)
        .sort((x, y) => x.date.localeCompare(y.date));
      const { by, note } = pickGrouping(start, end, a.group_by);
      const agg = (rs: any[]) => ({
        days: rs.length,
        avg_steps: avg(rs, "steps", 0),
        avg_resting_hr_bpm: avg(rs, "resting_hr_bpm", 1),
        avg_stress: avg(rs, "stress_avg", 0),
        avg_body_battery_high: avg(rs, "body_battery_high", 0),
        intensity_min: rs.reduce((s, r) => s + (r.intensity_moderate_min ?? 0) + 2 * (r.intensity_vigorous_min ?? 0), 0),
      });
      return {
        start,
        end,
        note,
        summary: agg(rows),
        [by === "day" ? "days" : by + "s"]: by === "day" ? rows : groupRows(rows, by, agg),
      };
    }),
  );

  server.registerTool(
    "garmin_activities",
    {
      title: "Activities",
      description:
        "Workouts newest first: type, distance, duration, pace or speed, HR, time in HR zones, training effect, elevation, power, strength sets. " +
        "Defaults to the last 14 days. Filter by sport (Run, Bike, Swim, Strength, Other) or Garmin type key (e.g. trail_running). " +
        "group_by week or month returns training volume per sport instead of individual sessions.",
      inputSchema: z.object({
        ...RANGE,
        sport: z.string().optional().describe("Run, Bike, Swim, Strength, Other, or a Garmin type key such as lap_swimming."),
        limit: z.number().int().min(1).max(200).optional().describe("Most sessions to list (default 30). Ignored when grouping."),
        group_by: z.enum(["session", "week", "month"]).optional().describe("Individual sessions (default) or totals per week/month."),
      }),
      annotations: READ_ONLY,
    },
    guarded(async (a: any) => {
      const { start, end } = resolveRange(env, a, 14);
      const grouped = a.group_by === "week" || a.group_by === "month";
      const raw = await getActivities(env, start, end, grouped ? 2000 : a.sport ? 1000 : (a.limit ?? 30));
      let acts = raw.map(shapeActivity);
      if (a.sport) {
        const f = String(a.sport).toLowerCase();
        acts = acts.filter((x) => x.sport.toLowerCase() === f || x.type === f);
      }
      const totals = (rs: ReturnType<typeof shapeActivity>[]) => {
        const by: Record<string, { sessions: number; hours: number; km: number }> = {};
        for (const r of rs) {
          const k = r.training ? r.sport : "Non-training";
          by[k] ??= { sessions: 0, hours: 0, km: 0 };
          by[k].sessions += 1;
          by[k].hours += (r.duration_min ?? 0) / 60;
          by[k].km += r.km ?? 0;
        }
        for (const v of Object.values(by)) {
          v.hours = round(v.hours, 2)!;
          v.km = round(v.km, 1)!;
        }
        const trainingDays = new Set(rs.filter((r) => r.training).map((r) => r.start.slice(0, 10))).size;
        return { training_days: trainingDays, by_sport: by };
      };
      if (grouped) {
        const rows = acts.map((x) => ({ ...x, date: x.start.slice(0, 10) }));
        // Weeks or months with no sessions are listed too, so gaps in training show up.
        return { start, end, totals: totals(acts), [a.group_by + "s"]: groupRows(rows, a.group_by, totals, { start, end }) };
      }
      const limit = a.limit ?? 30;
      return {
        start,
        end,
        totals: totals(acts),
        shown: Math.min(limit, acts.length),
        activities: acts.slice(0, limit),
      };
    }),
  );

  server.registerTool(
    "garmin_activity",
    {
      title: "One activity",
      description: "Full summary of one activity by its id (from garmin_activities), including min HR, recovery HR and temperature.",
      inputSchema: z.object({ activity_id: z.number().int().describe("activity id") }),
      annotations: READ_ONLY,
    },
    guarded(async (a: any) => {
      const d = await getActivity(env, a.activity_id);
      if (!d) throw new GarminError(`No activity with id ${a.activity_id}.`);
      const s = d.summaryDTO || {};
      return {
        ...shapeActivity(d),
        min_hr_bpm: round(s.minHR),
        avg_temp_c: round(s.averageTemperature, 1),
        elapsed_min: mins(s.elapsedDuration),
        aerobic_te_message: s.aerobicTrainingEffectMessage ?? null,
        anaerobic_te_message: s.anaerobicTrainingEffectMessage ?? null,
        moderate_intensity_min: num(s.moderateIntensityMinutes),
        vigorous_intensity_min: num(s.vigorousIntensityMinutes),
        steps: num(s.steps),
        device: d.metadataDTO?.manufacturer ?? null,
      };
    }),
  );

  server.registerTool(
    "garmin_weight",
    {
      title: "Weight",
      description: "Weigh-ins in kg (with BMI and body composition when the scale records them). Defaults to the last 365 days.",
      inputSchema: z.object(RANGE),
      annotations: READ_ONLY,
    },
    guarded(async (a: any) => {
      const { start, end } = resolveRange(env, a, 365);
      const rows = [...(await getWeight(env, start, end)).values()]
        .map((w) => {
          const l = w.latestWeight || {};
          const g = (x: unknown) => (num(x) ? round((x as number) / 1000, 1) : null);
          return {
            date: w.summaryDate,
            kg: g(l.weight),
            bmi: round(l.bmi, 1),
            body_fat_pct: round(l.bodyFat, 1),
            muscle_kg: g(l.muscleMass),
            bone_kg: g(l.boneMass),
            body_water_pct: round(l.bodyWater, 1),
          };
        })
        .sort((x, y) => x.date.localeCompare(y.date));
      return { start, end, weigh_ins: rows.length, weights: rows };
    }),
  );

  server.registerTool(
    "garmin_profile",
    {
      title: "Fitness age and HR zones",
      description: "Garmin fitness age with the levers that would lower it, and heart-rate zone floors per sport.",
      inputSchema: z.object({}),
      annotations: READ_ONLY,
    },
    guarded(async () => {
      const today = todayIn(env.TZ);
      const [fa, zones] = await Promise.all([getFitnessAge(env, today), getHrZones(env)]);
      const c = fa?.components || {};
      const lever = (x: any) => (x ? { value: round(x.value, 1), target: num(x.targetValue), potential_age: round(x.potentialAge, 1) } : null);
      return {
        fitness_age: fa
          ? {
              chronological_age: num(fa.chronologicalAge),
              fitness_age: round(fa.fitnessAge, 1),
              achievable: round(fa.achievableFitnessAge, 1),
              previous: round(fa.previousFitnessAge, 1),
              vigorous_days_per_week: lever(c.vigorousDaysAvg),
              vigorous_minutes_per_week: lever(c.vigorousMinutesAvg),
              resting_hr_bpm: num(c.rhr?.value),
              bmi: lever(c.bmi),
              updated: fa.lastUpdated ?? null,
            }
          : null,
        hr_zones: (zones || []).map((z: any) => ({
          sport: z.sport,
          method: z.trainingMethod,
          max_hr_bpm: num(z.maxHeartRateUsed),
          resting_hr_bpm: num(z.restingHeartRateUsed),
          lactate_threshold_bpm: num(z.lactateThresholdHeartRateUsed),
          zone_floors_bpm: [z.zone1Floor, z.zone2Floor, z.zone3Floor, z.zone4Floor, z.zone5Floor],
        })),
      };
    }),
  );

  return server;
}
