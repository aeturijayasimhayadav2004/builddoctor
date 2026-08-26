import { useCallback, useEffect, useMemo, useState } from "react";

import { API_BASE, fetchDiagnoses, fetchStats } from "./api.ts";
import type { Diagnosis, Stats } from "./api.ts";
import DiagnosisTable from "./DiagnosisTable.tsx";
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
 */
export default function App() {
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
      const [nextStats, nextRows] = await Promise.all([
        fetchStats(),
        fetchDiagnoses(),
      ]);
      setStats(nextStats);
      setRows(nextRows.diagnoses);
      setLoadedAt(new Date().toISOString());
    } catch (cause) {
      // The usual cause is the API not running, and fetch reports that as
      // a bare "Failed to fetch" with no url in it. Saying which url was
      // being called turns a mystery into an obvious fix.
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setLoading(false);
    }
  }, []);

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

      {firstLoad && <TableSkeleton />}

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
