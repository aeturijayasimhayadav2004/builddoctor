import { useCallback, useEffect, useMemo, useState } from "react";

import {
  API_BASE,
  NotSignedInError,
  fetchDiagnoses,
  fetchMe,
  fetchStats,
  logout,
} from "./api.ts";
import type { Diagnosis, Me, Stats } from "./api.ts";
import DiagnosisTable from "./DiagnosisTable.tsx";
import SignIn from "./SignIn.tsx";
import StatCards from "./StatCards.tsx";
import type { LaneFilter } from "./StatCards.tsx";
import { CardsSkeleton, LoadingAnnouncement, TableSkeleton } from "./Skeleton.tsx";
import { laneLabel, relativeTime } from "./format.ts";
import { AlertTriangle, Pulse, Refresh, X } from "./icons.tsx";

/**
 * The whole dashboard.
 *
 * It fetches once on mount and again when Refresh is pressed. There is no
 * automatic polling on purpose: this page exists to show what BuildDoctor
 * has already done, and a build failing is a thing that happens every few
 * hours at most, so a timer would spend all day asking a question whose
 * answer has not changed.
 *
 * Both requests are fired together rather than one after the other. They
 * do not depend on each other, and waiting for the first before starting
 * the second would double the time the page spends empty.
 *
 * SINCE PHASE 12 there is a question that has to be answered before either
 * of them: who is looking. /api/me is the only route that answers for
 * everybody, so it goes first and alone, and the two data calls happen only
 * if it says somebody is signed in. A logged-out page that still fired them
 * would collect two 401s and render an error, which would be a lie - nothing
 * is broken, the visitor simply has not logged in yet.
 */
export default function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [stats, setStats] = useState<Stats | null>(null);
  const [rows, setRows] = useState<Diagnosis[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [loadedAt, setLoadedAt] = useState<string | null>(null);

  /* undefined = show every lane. null is NOT the same thing: it is the
     "unclassified" bucket, rows 1-4, which predate lanes. Collapsing the
     two would make it impossible to filter to exactly those rows. */
  const [filter, setFilter] = useState<LaneFilter>(undefined);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const who = await fetchMe();
      setMe(who);

      if (!who.signed_in) {
        // Not an error state. Clear anything a previous session left in
        // memory, so signing out cannot leave rows on screen.
        setStats(null);
        setRows([]);
        return;
      }

      const [nextStats, nextRows] = await Promise.all([
        fetchStats(),
        fetchDiagnoses(),
      ]);
      setStats(nextStats);
      setRows(nextRows.diagnoses);
      setLoadedAt(new Date().toISOString());
    } catch (cause) {
      // A 401 mid-flight means the eight-hour session expired between the
      // /api/me call and these two. Treated as "logged out", not as a
      // failure, because that is what it is.
      if (cause instanceof NotSignedInError) {
        setMe({ signed_in: false });
        setStats(null);
        setRows([]);
        return;
      }
      // Otherwise the usual cause is the API not running, and fetch reports
      // that as a bare "Failed to fetch" with no url in it. Saying which url
      // was being called turns a mystery into an obvious fix.
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setLoading(false);
    }
  }, []);

  const signOut = useCallback(async () => {
    await logout();
    // Re-running load() rather than setting state by hand: the server is the
    // authority on whether the cookie is really gone, and asking it means the
    // UI cannot end up claiming a sign-out that did not happen.
    void load();
  }, [load]);

  useEffect(() => {
    void load();
  }, [load]);

  /* Filtering happens here rather than on the server. Thirteen rows are
     already in memory; a round trip to re-ask for a subset of what the
     browser is holding would be slower and would make the API responsible
     for a purely visual choice. */
  const visible = useMemo(
    () => (filter === undefined ? rows : rows.filter((row) => row.lane === filter)),
    [rows, filter],
  );

  const firstLoad = loading && rows.length === 0 && !error;

  // Nothing at all until /api/me has answered. Rendering the signed-out page
  // during that gap would flash a "Sign in" screen at somebody who already is.
  if (me === null && loading) {
    return (
      <div className="page">
        <LoadingAnnouncement />
        <CardsSkeleton />
      </div>
    );
  }

  if (me !== null && !me.signed_in) {
    return <SignIn />;
  }

  return (
    <div className="page">
      <header className="masthead">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            <Pulse size={21} />
          </span>
          <div>
            <h1>BuildDoctor</h1>
            <p className="tagline">
              Every CI failure it has diagnosed, and what it decided to do about
              each one.
            </p>
          </div>
        </div>

        <div className="masthead-actions">
          {me?.login && (
            <span className="pulse-dot" title={
              me.accounts?.length
                ? `Installations: ${me.accounts.join(", ")}`
                : "No installations of BuildDoctor on this account"
            }>
              {me.login}
              {me.is_app_owner ? " · owner" : ""}
            </span>
          )}
          {/* Answers "is this number current, or a ghost from ten minutes
              ago?" without anyone having to ask. Turns coral the moment a
              request fails, so a stale page never looks healthy. */}
          {loadedAt && (
            <span className={error ? "pulse-dot is-stale" : "pulse-dot"}>
              {error ? "stale" : `updated ${relativeTime(loadedAt)}`}
            </span>
          )}
          <button type="button" onClick={() => void load()} disabled={loading}>
            <Refresh size={15} className={loading ? "spin" : undefined} />
            {loading ? "Loading…" : "Refresh"}
          </button>
          <button type="button" onClick={() => void signOut()}>
            Sign out
          </button>
        </div>
      </header>

      {error && (
        <div className="alert" role="alert">
          <span className="alert-icon">
            <AlertTriangle size={19} />
          </span>
          <div>
            <strong>Could not reach the API.</strong>
            <p>{error}</p>
            <p className="note">
              Calling <code>{API_BASE}</code>. Check that the app container is up
              (<code>docker compose ps</code>) and that{" "}
              <code>{API_BASE}/health</code> answers in a browser.
            </p>
          </div>
        </div>
      )}

      {firstLoad && <LoadingAnnouncement />}
      {firstLoad && <CardsSkeleton />}

      {stats && <StatCards stats={stats} filter={filter} onFilter={setFilter} />}

      {/* Says out loud what the numbers above are scoped to. Without it, a
          total of 2 on an account with one installation looks like data
          loss rather than like the filter working. */}
      {stats && me?.signed_in && (
        <p className="note">
          Showing {me.installations_visible ?? 0} installation
          {(me.installations_visible ?? 0) === 1 ? "" : "s"}
          {me.accounts?.length ? ` (${me.accounts.join(", ")})` : ""}
          {me.includes_legacy_rows
            ? ", plus the pre-installation rows this account owns"
            : ""}
          .
        </p>
      )}

      {firstLoad && <TableSkeleton />}

      {!loading && !error && rows.length === 0 && (
        <p className="note">
          No diagnoses yet for your installations. BuildDoctor writes a row
          the first time a workflow fails on a repository it is installed on.
        </p>
      )}

      {rows.length > 0 && (
        <>
          <div className="section-head">
            <h2 className="section-title">
              Diagnoses <span className="muted">newest first</span>
            </h2>

            {filter !== undefined && (
              <span className="filter-note">
                showing {visible.length} of {rows.length} · {laneLabel(filter)}
                <button
                  type="button"
                  onClick={() => setFilter(undefined)}
                  title="Show every lane again"
                >
                  <X size={13} />
                  clear
                </button>
              </span>
            )}
          </div>

          <DiagnosisTable rows={visible} filtered={filter !== undefined} />

          <p className="foot">
            Click any row to see the full diagnosis and the log excerpt it was
            written from. Click a lane in the card above to filter.
          </p>
        </>
      )}
    </div>
  );
}
