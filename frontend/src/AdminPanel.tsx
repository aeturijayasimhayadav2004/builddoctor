import { useCallback, useEffect, useState } from "react";

import {
  ForbiddenError,
  NotSignedInError,
  fetchAdminInstallations,
  setInstallationAllowed,
} from "./api.ts";
import type { AdminInstallation } from "./api.ts";
import { relativeTime } from "./format.ts";

/**
 * The approval view (Phase 13). Rendered only for the App owner.
 *
 * WHY IT FETCHES ITS OWN DATA INSTEAD OF BEING HANDED IT
 *
 * The rest of the page loads in App.tsx, but this list is not part of that
 * load: it is a different question, asked of a different route, that only
 * one account in the world can ask. Folding it into the main load would mean
 * every ordinary installer's page collecting a 403 on every refresh, and the
 * error handling to swallow it - so a panel that only the owner sees also
 * only the owner requests.
 *
 * WHY THE ROW UPDATES FROM THE SERVER RATHER THAN OPTIMISTICALLY
 *
 * Approving is the entire point of this screen, and it is the one action
 * here whose result must not be guessed at. An optimistic flip shows an
 * approved row whether or not the write landed, and the failure it would
 * hide is exactly the one that matters: the installation was uninstalled
 * while this page was open, so the row is gone and the approval went
 * nowhere. Re-reading the list is one request against an action a human
 * takes a handful of times.
 */
export default function AdminPanel() {
  const [rows, setRows] = useState<AdminInstallation[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const [open, setOpen] = useState(false);

  const load = useCallback(async () => {
    try {
      const data = await fetchAdminInstallations();
      setRows(data.installations);
      setError(null);
    } catch (cause) {
      // 401 and 403 both mean "this panel is not for you". Neither is worth
      // an alarming message on a page that is otherwise working: the owner
      // check already decided this, and App.tsx will handle a real sign-out.
      if (cause instanceof NotSignedInError || cause instanceof ForbiddenError) {
        setRows([]);
        return;
      }
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const flip = useCallback(
    async (installationId: number, allowed: boolean) => {
      setBusy(installationId);
      setError(null);
      try {
        await setInstallationAllowed(installationId, allowed);
        await load();
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : String(cause));
      } finally {
        setBusy(null);
      }
    },
    [load],
  );

  if (rows === null || rows.length === 0) return null;

  const pending = rows.filter((row) => !row.is_allowed);

  return (
    <section className="panel">
      <div className="section-head">
        <h2 className="section-title">
          Installations{" "}
          <span className="muted">
            {pending.length > 0
              ? `${pending.length} waiting for approval`
              : "all approved"}
          </span>
        </h2>
        <button type="button" onClick={() => setOpen((value) => !value)}>
          {open ? "Hide" : "Show"}
        </button>
      </div>

      {open && (
        <>
          {error && (
            <p className="note" role="alert">
              {error}
            </p>
          )}

          <div className="rows">
            {rows.map((row) => (
              <div className="row-group" key={row.installation_id}>
                <div className="summary-row">
                  <span className="mono">{row.account_login}</span>
                  <span className="muted">
                    {row.account_type ?? "unknown"} · {row.installation_id}
                    {row.created_at ? ` · ${relativeTime(row.created_at)}` : ""}
                    {" · "}
                    {row.diagnoses} diagnos{row.diagnoses === 1 ? "is" : "es"}
                  </span>
                  <span
                    className={
                      row.is_allowed ? "badge badge-safe" : "badge badge-needs"
                    }
                  >
                    {row.is_allowed ? "approved" : "pending"}
                  </span>
                  <button
                    type="button"
                    disabled={busy === row.installation_id}
                    onClick={() =>
                      void flip(row.installation_id, !row.is_allowed)
                    }
                  >
                    {busy === row.installation_id
                      ? "Saving…"
                      : row.is_allowed
                        ? "Revoke"
                        : "Approve"}
                  </button>
                </div>
              </div>
            ))}
          </div>

          <p className="foot">
            Approving takes effect on the next webhook - the gate re-reads this
            table on every delivery and caches nothing, so there is no deploy
            and no restart. Revoking stops future diagnoses; it does not hide
            diagnoses already written, which stay visible to the people who
            could always see them.
          </p>
        </>
      )}
    </section>
  );
}
