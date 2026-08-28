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

/** One installation of the App, as the viewer's own /api/me reports it. */
export interface MyInstallation {
  installation_id: number;
  account_login: string;
  /** "User" or "Organization". Null on rows recorded before Phase 11. */
  account_type: string | null;
  /**
   * Whether BuildDoctor will act on this installation's builds. Note what
   * this does NOT control: diagnoses already written stay visible to the
   * people who could always see them. Revoking stops future work; it does
   * not confiscate past work.
   */
  is_allowed: boolean;
}

/** Who the browser is signed in as. `GET /api/me` allows everybody. */
export interface Me {
  signed_in: boolean;
  login?: string;
  avatar_url?: string | null;
  /**
   * The installations GitHub says this user administers, intersected with
   * the ones this database still knows about - so an entry here is both
   * theirs and real. Empty means they have not installed the App.
   */
  installations?: MyInstallation[];
  /**
   * How many the session cookie claimed at sign-in. Larger than
   * installations.length means one went away mid-session, or its `created`
   * webhook never landed.
   */
  installations_at_login?: number;
  /** True only for the account that registered the App itself. */
  is_app_owner?: boolean;
  /** Whether the pre-Phase-11 rows with a null installation_id are included. */
  includes_legacy_rows?: boolean;
  /**
   * They have installed the App and none of their installations have been
   * approved yet. A presentation fact, not a permission: it decides whether
   * to say "waiting for approval" instead of showing an empty table.
   */
  pending_approval?: boolean;
}

/** One installation as the admin view sees it: everybody's, not just yours. */
export interface AdminInstallation extends MyInstallation {
  created_at: string | null;
  /** Diagnoses written for this installation so far. */
  diagnoses: number;
}

export interface AdminInstallations {
  count: number;
  pending: number;
  installations: AdminInstallation[];
}

/** Thrown when the API says 403: signed in, but not the App owner. */
export class ForbiddenError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ForbiddenError";
  }
}

/**
 * Where the API lives, from the point of view of the BROWSER.
 *
 * EMPTY, meaning "the origin this page came from" - which is the whole point
 * since Phase 12. The session is a cookie, and a cookie is scoped to a site;
 * calling the API on a different host would mean the browser refusing to
 * attach it. In production FastAPI serves this bundle itself under
 * /dashboard, and in development Vite proxies /api through to it, so the
 * answer is the same in both places: same origin, no host to configure.
 *
 * VITE_API_BASE is still honoured for the one case it is actually good for -
 * pointing a local dev build at some other deployment - and anyone who sets
 * it takes on the cross-site cookie problem knowingly.
 */
export const API_BASE: string = import.meta.env.VITE_API_BASE ?? "";

/** Thrown when the API says 401. Distinct so the UI can react to it. */
export class NotSignedInError extends Error {
  constructor() {
    super("Not signed in");
    this.name = "NotSignedInError";
  }
}

async function getJson<T>(path: string): Promise<T> {
  // credentials: "include" is the line that makes any of this work. fetch
  // does NOT send cookies by default on a cross-origin request, and omitting
  // this produces the most confusing possible symptom: a 401 from an API you
  // are definitely logged into, because the cookie was never sent.
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: "include",
  });

  if (response.status === 401) {
    throw new NotSignedInError();
  }
  if (response.status === 403) {
    throw new ForbiddenError("Only the App owner can do that.");
  }
  if (!response.ok) {
    throw new Error(`${path} returned ${response.status} ${response.statusText}`);
  }
  return (await response.json()) as T;
}

export function fetchMe(): Promise<Me> {
  return getJson<Me>("/api/me");
}

export function fetchStats(): Promise<Stats> {
  return getJson<Stats>("/api/stats");
}

export function fetchDiagnoses(limit = 100): Promise<DiagnosesResponse> {
  return getJson<DiagnosesResponse>(`/api/diagnoses?limit=${limit}`);
}

/** Where to send the browser to start GitHub sign-in. A full page navigation,
 *  not a fetch: the flow leaves this origin for github.com and comes back. */
export const LOGIN_URL = `${API_BASE}/login`;

export async function logout(): Promise<void> {
  await fetch(`${API_BASE}/logout`, {
    method: "POST",
    credentials: "include",
  });
}

/* -------------------------------------------------------------------------
 * The admin half. Only the App owner gets anything but a 403 from these.
 * ---------------------------------------------------------------------- */

export function fetchAdminInstallations(): Promise<AdminInstallations> {
  return getJson<AdminInstallations>("/api/admin/installations");
}

/**
 * Approve or revoke one installation.
 *
 * There is no CSRF token, and that is deliberate rather than forgotten: the
 * session cookie is SameSite=Lax, so a browser will not attach it to a POST
 * started by another site. Such a request arrives with no session and is
 * rejected as unauthenticated before it reaches the handler.
 */
export async function setInstallationAllowed(
  installationId: number,
  allowed: boolean,
): Promise<void> {
  const response = await fetch(
    `${API_BASE}/api/admin/installations/${installationId}/allowed`,
    {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ allowed }),
    },
  );

  if (response.status === 401) throw new NotSignedInError();
  if (response.status === 403) {
    throw new ForbiddenError("Only the App owner can approve installations.");
  }
  if (!response.ok) {
    throw new Error(
      `Approving ${installationId} returned ${response.status} ${response.statusText}`,
    );
  }
}
