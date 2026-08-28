import { LOGIN_URL } from "./api.ts";
import { Pulse } from "./icons.tsx";

/**
 * What the dashboard shows to somebody who is not signed in (Phase 12).
 *
 * The important part is not the button, it is what this component does NOT
 * do: it fires no requests. Before Phase 12 the page loaded and immediately
 * asked for every diagnosis in the database, which worked, because the API
 * gave them to anyone who asked. Now those routes answer 401, and a
 * logged-out page that still called them would spend its first second
 * collecting two failures and then rendering an error - telling the user
 * something is broken when nothing is.
 *
 * So the signed-out state is a dead end on purpose. The only way out of it
 * is a full page navigation to /login, which has to be a navigation rather
 * than a fetch: the flow leaves this origin for github.com and comes back.
 */
export default function SignIn({ reason }: { reason?: string }) {
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
              Every CI failure it has diagnosed, and what it decided to do
              about each one.
            </p>
          </div>
        </div>
      </header>

      <div className="alert" role="status">
        <div>
          <strong>Sign in to see your diagnoses.</strong>
          <p>
            {reason ??
              "BuildDoctor shows you the failures from repositories where you " +
                "have installed the app, and nothing else."}
          </p>
          <p style={{ marginTop: "1rem" }}>
            {/* A plain link, not a button with an onClick. This has to be a
                real navigation - a fetch to /login would follow the redirect
                to github.com in the background and hand back HTML nobody can
                do anything with. */}
            <a href={LOGIN_URL}>
              <button type="button">Sign in with GitHub</button>
            </a>
          </p>
          <p className="note">
            You will be asked to authorize BuildDoctor CI. It reads which
            installations you administer, and nothing about your account is
            stored beyond your GitHub id and login.
          </p>
        </div>
      </div>
    </div>
  );
}
