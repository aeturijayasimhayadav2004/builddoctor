import type { MyInstallation } from "./api.ts";

/**
 * What an installer sees while their installation is waiting for approval
 * (Phase 13).
 *
 * THE PROBLEM THIS EXISTS TO FIX
 *
 * Before Phase 13, an unapproved installation was skipped in silence. The
 * pipeline logged its reason to a console nobody outside this project can
 * read, GitHub reported the webhook as delivered successfully - because it
 * was - and the dashboard showed an empty table. Every visible signal said
 * "working, nothing has failed yet", and the installer's reasonable
 * conclusion was that BuildDoctor was broken or that they had done the
 * install wrong.
 *
 * An empty table and a blocked installation are different facts and now
 * they look different. That is the entire job of this component.
 *
 * WHY IT DOES NOT OFFER A "REQUEST APPROVAL" BUTTON
 *
 * There is nowhere for such a request to go. There is no notification
 * channel, no inbox, and no email configured, so the button would either
 * lie or write a row nobody ever reads. Installing the App is itself the
 * request - it puts a row in the installations table that the admin view
 * lists at the top - so a second act of asking would add a step without
 * adding a recipient. When there is somewhere for a request to land, that
 * is when the button becomes honest.
 */
export default function PendingApproval({
  installations,
}: {
  installations: MyInstallation[];
}) {
  return (
    <div className="alert" role="status">
      <div>
        <strong>Waiting for approval.</strong>
        <p>
          {installations.length === 1
            ? "Your installation is on record but has not been approved yet."
            : "Your installations are on record but none have been approved yet."}{" "}
          BuildDoctor is not diagnosing failures for{" "}
          {installations.length === 1 ? "it" : "them"} until that happens.
          Nothing is broken and nothing is being lost - builds simply run
          without it.
        </p>

        <ul className="facts">
          {installations.map((item) => (
            <li key={item.installation_id}>
              <span className="mono">{item.account_login}</span>
              <span className="muted">
                {" "}
                · {item.account_type ?? "unknown type"} · installation{" "}
                {item.installation_id}
              </span>
            </li>
          ))}
        </ul>

        <p className="note">
          Every new installation starts unapproved, including the owner's own.
          Approval is a person's decision rather than a setting, so there is
          no configuration on your side that would speed it up. Once approved,
          the very next failing workflow is diagnosed - nothing needs
          reinstalling and nothing needs restarting.
        </p>
      </div>
    </div>
  );
}
