import { useCallback, useEffect, useState } from "react";

import { API_BASE, fetchDiagnoses, fetchStats } from "./api.ts";
import type { Diagnosis, Stats } from "./api.ts";
import DiagnosisTable from "./DiagnosisTable.tsx";
import StatCards from "./StatCards.tsx";

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

  return (
    <div className="page">
      <header className="masthead">
        <div>
          <h1>BuildDoctor</h1>
          <p className="tagline">
            Every CI failure it has diagnosed, and what it decided to do about
            each one.
          </p>
        </div>
        <button type="button" onClick={() => void load()} disabled={loading}>
          {loading ? "Loading…" : "Refresh"}
        </button>
      </header>

      {error && (
        <div className="alert">
          <strong>Could not reach the API.</strong>
          <p>{error}</p>
          <p className="note">
            Calling <code>{API_BASE}</code>. Check that the app container is up
            (<code>docker compose ps</code>) and that{" "}
            <code>{API_BASE}/health</code> answers in a browser.
          </p>
        </div>
      )}

      {stats && <StatCards stats={stats} />}

      {/* Only shown on the very first load. A refresh keeps the old rows on
          screen rather than blanking the page for a second. */}
      {loading && rows.length === 0 && !error && <p className="empty">Loading…</p>}

      {rows.length > 0 && (
        <>
          <h2 className="section-title">
            Diagnoses <span className="muted">newest first</span>
          </h2>
          <DiagnosisTable rows={rows} />
          <p className="foot">
            Click any row to see the full diagnosis and the log excerpt it was
            written from.
          </p>
        </>
      )}
    </div>
  );
}
