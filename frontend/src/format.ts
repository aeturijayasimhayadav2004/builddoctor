/**
 * Turning API values into something a table cell can show.
 *
 * The recurring job here is nulls. This database has several, for
 * unrelated reasons - a lane that predates lanes, a url lost to a bug, a
 * memory lookup that never ran - and every one of them must come out as a
 * dash rather than as the word "undefined".
 */

import type { Lane } from "./api.ts";

/** The character to show when there is genuinely nothing to show. */
export const EMPTY = "—";

/** Any missing value becomes the dash. Zero and false are kept. */
export function orDash(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return EMPTY;
  const text = String(value).trim();
  return text === "" ? EMPTY : text;
}

const LANE_LABELS: Record<Lane, string> = {
  informational: "informational",
  safe_auto_fix: "safe auto-fix",
  needs_review: "needs review",
};

/** Human-readable lane name. Null becomes "unclassified", not a dash: the
 *  row was diagnosed, it just happened before Phase 4 invented lanes. */
export function laneLabel(lane: Lane | null): string {
  return lane === null ? "unclassified" : LANE_LABELS[lane];
}

/** The css class that carries the lane's colour. */
export function laneClass(lane: Lane | null): string {
  return `badge badge-${lane ?? "none"}`;
}

/** An ISO timestamp as local date and time, or a dash. */
export function formatTime(iso: string | null): string {
  if (!iso) return EMPTY;
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return EMPTY;
  return when.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** "3 hours ago". Falls back to the dash rather than inventing a time. */
export function relativeTime(iso: string | null): string {
  if (!iso) return EMPTY;
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return EMPTY;

  const seconds = Math.round((then - Date.now()) / 1000);
  const steps: Array<[Intl.RelativeTimeFormatUnit, number]> = [
    ["second", 60],
    ["minute", 60],
    ["hour", 24],
    ["day", 7],
    ["week", 4.35],
    ["month", 12],
    ["year", Number.POSITIVE_INFINITY],
  ];

  const format = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  let value = seconds;
  for (const [unit, size] of steps) {
    if (Math.abs(value) < size) return format.format(Math.round(value), unit);
    value = value / size;
  }
  return format.format(Math.round(value), "year");
}

/** 0.9999 -> "100.0%". Similarity is stored 0..1. */
export function percent(fraction: number): string {
  return `${(fraction * 100).toFixed(1)}%`;
}
