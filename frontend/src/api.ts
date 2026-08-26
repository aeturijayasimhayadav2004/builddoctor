/**
 * The exact shape of what the BuildDoctor API returns, and the two calls
 * that fetch it.
 *
 * These types are written by hand from the real responses, so they are a
 * claim, not a guarantee - nothing checks at runtime that the server still
 * agrees. What they DO give is a compiler that refuses to build the moment
 * the UI reads a field that is not declared here, or forgets that a field
 * can be null. Half the rows in this database have a null somewhere, so
 * that second part is the one earning its keep.
 */

/** The three lanes from Phase 4. Null on rows 1-4, which predate lanes. */
export type Lane = "informational" | "safe_auto_fix" | "needs_review";

export interface LaneCount {
  lane: Lane | null;
  count: number;
  percent: number;
}

export interface MemoryStats {
  /** Rows where memory actually ran. Phase 6 onwards only. */
  asked: number;
  /** Of those, how many found a past failure above the threshold. */
  hits: number;
  /** hits / asked, as a percentage. Not hits / total - see db.py. */
  rate: number;
  /** Rows that have an embedding, so memory can find them in future. */
  searchable: number;
}

export interface Stats {
  total: number;
  repos: number;
  /** ISO 8601, or null on an empty database. */
  latest_at: string | null;
  lanes: LaneCount[];
  memory: MemoryStats;
}

/** Present only when memory found a past failure above the threshold. */
export interface MemoryMatch {
  /** The diagnoses.id that was matched. */
  row_id: number;
  run_id: number;
  /** Cosine similarity, 0..1. Only ever >= the 0.90 threshold. */
  similarity: number;
  lane: Lane | null;
}

export interface Diagnosis {
  id: number;
  run_id: number;
  repo: string;
  created_at: string | null;
  lane: Lane | null;
  /** First line of diagnosis_text, already shortened by the server. */
  summary: string;
  diagnosis_text: string;
  log_excerpt: string;
  /** "pr_comment" | "commit_comment" | null. */
  posted_to: string | null;
  /**
   * Null in two unrelated situations: row 10, where a Phase 5 bug lost the
   * url, and every amber run, which re-runs a job and posts nothing at
   * all. The UI must not assume this is a string.
   */
  posted_url: string | null;
  run_url: string | null;
  workflow: string | null;
  failed_step: string | null;
  run_attempt: number | null;
  files_changed: string[];
  /**
   * Whether memory RAN, which is a different question from whether it
   * matched. False for every row written before Phase 6.
   */
  memory_checked: boolean;
  memory_match: MemoryMatch | null;
  embedded: boolean;
}

export interface DiagnosesResponse {
  count: number;
  limit: number;
  diagnoses: Diagnosis[];
}

/**
 * Where the API lives, from the point of view of the BROWSER.
 *
 * This is the one thing that is easy to get wrong once the dashboard runs
 * in a container. Inside compose, the app is reachable as "app" - but this
 * code does not run inside compose, it runs in a browser on the host, and
 * a browser has never heard of a compose service name. So it must be a URL
 * the host can open: http://localhost:8000.
 *
 * Vite exposes variables that start with VITE_ and substitutes them into
 * the bundle when the dev server starts. Changing it means restarting the
 * container, not just reloading the page.
 */
export const API_BASE: string =
  import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`);
  if (!response.ok) {
    throw new Error(`${path} returned ${response.status} ${response.statusText}`);
  }
  return (await response.json()) as T;
}

export function fetchStats(): Promise<Stats> {
  return getJson<Stats>("/api/stats");
}

export function fetchDiagnoses(limit = 100): Promise<DiagnosesResponse> {
  return getJson<DiagnosesResponse>(`/api/diagnoses?limit=${limit}`);
}
