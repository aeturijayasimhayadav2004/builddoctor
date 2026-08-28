import { LOGIN_URL } from "./api.ts";
import { Refresh } from "./icons.tsx";

/**
 * "Refresh access" - the way out of a stale installation list (Phase 14).
 *
 * THE DEAD END THIS REPLACES
 *
 * The session cookie carries a SNAPSHOT of what GitHub said at sign-in: the
 * installations this person administers. Every request intersects that
 * snapshot against the installations table, and that intersection can only
 * ever shrink it - it removes access, it never grants it. So an installation
 * created AFTER signing in is invisible, and the dashboard truthfully reports
 * zero installations to somebody who just installed the App.
 *
 * The old fix was "sign out, then sign in again", which is a real fix and a
 * terrible instruction: it reads like a superstition, it appears at exactly
 * the moment somebody is already unsure whether they installed the App
 * correctly, and it asks them to do something that sounds destructive to
 * recover from something that sounds broken. It was the first thing every
 * new installer hit.
 *
 * WHY THIS DOES NOT MEAN STORING A TOKEN
 *
 * The obvious alternative is for the server to re-ask GitHub which
 * installations this person administers, on demand. It cannot: answering that
 * question needs the USER ACCESS TOKEN, and Phase 12 deliberately throws that
 * token away at the end of the OAuth callback and writes it nowhere. Keeping
 * it - in the cookie, which is signed but not encrypted, or in a table - is
 * the one thing that would turn a stale list into a stored credential.
 *
 * So instead of the server re-asking on the user's behalf, the user re-asks.
 * This is a plain navigation to /login, which runs the same OAuth round trip
 * that produced the session in the first place and writes a fresh snapshot.
 *
 * AND IT IS FAST, which is the part that makes it a button rather than an
 * apology. The browser still holds its own github.com session, and this
 * account has already authorized the App, so GitHub has no question left to
 * ask: it redirects straight back rather than rendering a consent screen.
 * What looks like a full sign-in is two redirects and no interaction.
 *
 * A LINK WOULD ALSO WORK, and a button is used anyway because this is not
 * navigation to a page - it is an action with an effect, and it should look
 * like the other actions in the masthead rather than like a footnote.
 */
export default function RefreshAccess({
  label = "Refresh access",
}: {
  label?: string;
}) {
  return (
    <button
      type="button"
      // A full page navigation, not fetch(). The OAuth flow leaves this
      // origin for github.com and comes back, which is something only the
      // top-level document can do - an XHR would be blocked by CORS at
      // github.com and could not follow the redirect chain anyway.
      onClick={() => {
        window.location.href = LOGIN_URL;
      }}
      title="Ask GitHub again which installations you administer, without signing out"
    >
      <Refresh size={15} />
      {label}
    </button>
  );
}
