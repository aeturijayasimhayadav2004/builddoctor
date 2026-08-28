# BuildDoctor — installation guide

Share this with anyone who wants to use BuildDoctor on their own
repositories. It is written for them, not for you; the parts you have to do
are at the end, under [For the owner](#for-the-owner).

---

## What it is

BuildDoctor watches a repository for failing GitHub Actions runs. When one
fails, it reads the log, works out what went wrong, and posts a comment on
the commit that caused it. That is the whole product.

It runs in three lanes, decided per failure:

| Lane | What it does |
| ---- | ------------ |
| `informational` | posts an explanation and stops |
| `safe_auto_fix` | re-runs the failed job (for a flake or a transient network error) |
| `needs_review` | flags it as something a person should look at |

It never pushes a commit, never opens a pull request, and never edits a file.

---

## The short version

**If every repository you select is public**, installing is the whole
process. BuildDoctor approves the installation itself, and the next failing
build gets a comment. Nothing to ask anybody for.

**If any of them is private**, the installation waits for the owner to
approve it by hand. They are notified automatically that you are waiting.

The reason for the split is in the next section, and it is worth thirty
seconds of your time before you install anything.

---

## Read this before you install

None of it is a catch. It is the stuff you would want to know afterwards.

**Your build logs leave your repository.** To work out what failed,
BuildDoctor downloads the failing job's log, trims it to the part around the
error, and sends that excerpt to a language model — `openai/gpt-oss-20b`,
running on Groq. If your build logs contain secrets, internal hostnames, or
anything you would not paste into a third-party service, do not install this
on that repository.

**The excerpt is stored.** Each diagnosis is kept in a Postgres database with
the repository name, the workflow name, the files the commit touched, and the
log excerpt itself. That is what the dashboard reads. It is kept
indefinitely — there is no automatic deletion.

**Its comments are as public as your repository.** BuildDoctor comments on
commits. On a public repository, so is the comment.

**This is exactly why public and private installs are treated differently.**
A public repository's build logs are already readable by anyone who wants
them, so BuildDoctor reading one discloses nothing that was not already open,
and no approval adds anything. A private repository's logs are a different
matter entirely — sending those to a model and storing them is a real
decision, and it stays a decision a human makes.

**What it asks permission for.** At install you will be asked for:

| Permission | Level | What BuildDoctor does with it |
| ---------- | ----- | ----------------------------- |
| Actions | read | download the failing run's logs, re-run a job |
| Contents | read | read the diff that triggered the run |
| Issues | write | post a comment |
| Pull requests | write | post a comment on a PR |
| Metadata | read | mandatory for every GitHub App |

Nothing here can write to your code. Earlier versions asked for
`contents: write`, which was wider than the code ever used; it was narrowed
to read in Phase 14.

**It is a hobby project on a free plan.** One person operates it. There is no
uptime guarantee, no support, and no notification if it stops working. The
server sleeps when idle, so the first request after a quiet spell can take up
to two minutes.

---

## Installing it

### 1. Install the App

Go to **<https://github.com/apps/builddoctor-ci>** and click **Install**.

- Pick the account or organisation you want it on. An organisation is a
  separate choice from your personal account — make sure you pick the right
  one.
- Choose **Only select repositories** and pick the ones you want watched.

  **Do not choose "All repositories" if you want the automatic approval.**
  An "all repositories" install covers every repository the account creates
  from then on, including private ones nobody has looked at, so BuildDoctor
  refuses to approve it by itself and sends it for manual approval instead.
  This is true even if every repository you own today is public.
- Click **Install**.

### 2. Sign in to the dashboard

Go to **<https://builddoctor.onrender.com/dashboard/>** and click **Sign in
with GitHub**.

You will be asked to authorize BuildDoctor CI. This is separate from
installing it: installing lets the bot see your repository, signing in lets
*you* see the dashboard. Authorizing reads which installations you
administer, and nothing else — your GitHub id and login are all that get
stored about you.

If your repositories are all public, you are done. If one is private, you
will see **Waiting for approval**, which is the correct state and not an
error — see [If you are waiting for approval](#if-you-are-waiting-for-approval).

### 3. Try it, if you want to see it work

Make a repository fail on purpose. Add `.github/workflows/ci.yml`:

```yaml
name: CI
on: [push]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: exit 1
```

Commit it. The run fails, and a comment should follow within about half a
minute — longer if the server was asleep.

---

## If you are waiting for approval

You will see this when the installation includes at least one private
repository, or when you installed on **All repositories**.

**The owner is notified automatically.** You do not have to chase them, and
there is no "request approval" button because installing already *is* the
request. If it has been a long time, message them — this is a hobby project,
not a rota.

Once approved, the next failing workflow is diagnosed. Nothing needs
reinstalling and nothing needs restarting; reload the dashboard to see it.

**A shortcut, if you can use it.** Selecting only your public repositories at
install time is approved instantly with nobody in the loop. If the private
one was not important to you, that is the faster path.

---

## When the dashboard looks wrong

### "I installed it, but the dashboard says I have no installations"

**Press "Just installed it? Refresh access"** on that message.

When you sign in, the dashboard asks GitHub once which installations you
administer and remembers the answer for that session. If you installed the
App *after* signing in, that answer is out of date and does not refresh on
its own. The button re-asks GitHub.

It is quick — your browser is still signed into github.com and you have
already authorized the App, so GitHub redirects straight back without asking
you anything. It looks like a page flicker rather than a sign-in.

(The reason it is not automatic: re-asking on your behalf would mean the
server holding onto your GitHub access token, and it deliberately throws that
away the moment you have signed in. A button you press is a better trade than
a stored credential.)

### "It says Waiting for approval"

See [above](#if-you-are-waiting-for-approval). The owner has been told.

### "It was working, and now it says waiting for approval"

Something changed the repositories your installation covers. BuildDoctor
approves an installation automatically only while **every** repository it
covers is public, and it withdraws that approval by itself if:

- a private repository is added to the installation, or
- one of the repositories is switched from public to private, or
- the installation is widened to "All repositories".

None of this loses anything. The owner is notified and can approve it by
hand, and an approval made by a person is never withdrawn automatically.

### "The dashboard is empty, but it does not say waiting"

You are approved and nothing has failed yet. Break a build (above) to check.

### "The page took forever to load"

The server sleeps when idle and takes about two minutes to wake. Subsequent
requests are fast.

### "A build failed but nothing appeared"

- Is the repository one you selected during install? Check at
  <https://github.com/settings/installations>.
- BuildDoctor only reacts to **GitHub Actions** workflow failures. Other CI
  systems send nothing.
- Give it a minute — a cold server is slow on the first delivery.

---

## Stopping it

**Uninstall:** <https://github.com/settings/installations> → **Configure**
next to BuildDoctor CI → **Uninstall**. For an organisation, the same page
under the org's settings.

Uninstalling stops everything immediately and deletes the installation
record. Diagnoses already written stay in the database — they are the
history of what happened, and removing them would silently change past
totals. Ask the owner if you want them deleted.

**Revoke your sign-in** (without uninstalling):
<https://github.com/settings/apps/authorizations>.

---

## For the owner

The half of the flow that is yours.

### What you no longer have to do

Public-only installations approve themselves. You are not asked, and you do
not need to watch for them. The dashboard shows them marked
**`auto · public`** so you can tell them apart from the ones you approved.

### What still reaches you

An installation that includes a private repository, or one made on "All
repositories", still waits for you — and now says so out loud. If
`OWNER_NOTIFY_WEBHOOK` is configured, a line arrives in your Discord or Slack
channel the moment it happens. If it is not configured, the same line goes to
the server log and the admin panel is still the authoritative list.

### Approving somebody

1. Sign in at <https://builddoctor.onrender.com/dashboard/>
2. The **Installations** panel appears under the stat cards — visible only to
   you, because it is gated on being the account that registered the App,
   read from GitHub's `GET /app` rather than from any config.
3. Pending installations sort to the top. Press **Approve**.

It takes effect on the **next webhook** — no deploy, no restart. The gate
re-reads the table on every delivery and caches nothing.

**Your approval outranks the automation.** A row you approve is marked as
yours and is never withdrawn automatically, even if a private repository is
added to it later. That is deliberate: withdrawing it would be the App
overruling a decision you made on purpose. Only the App's *own* automatic
approvals are ever withdrawn automatically.

**Revoke** is the same button in the other direction. It stops future
diagnoses; it does not hide diagnoses already written, which stay visible to
whoever could already see them.

### What you are agreeing to when you approve

Approving means their failing build logs get downloaded, excerpted, sent to
Groq, and stored in your database. You are the one paying for the model calls
and holding the data. Approve people whose logs you are willing to be
responsible for.

This is why the automatic path is limited to public repositories: those logs
were already public, so approving them commits you to nothing you had not
already accepted by running a public service.

### Things that do not exist yet

- **No notification to the installer when you approve.** They find out by
  reloading the dashboard.
- **No second approver.** Only the account that registered the App can
  approve, with no delegation.
- **No sweep of existing installations.** Withdrawal happens when a webhook
  arrives. Nothing periodically re-checks what every repository looks like
  now, so a delivery lost while the server was asleep would leave an
  approval standing that should have been withdrawn.

### Making the App private again

Currently public, so anyone can install it. Per GitHub's docs a public App
**cannot be made private while it is installed on other accounts** — you
would have to uninstall it everywhere else first. The control is at
<https://github.com/settings/apps/builddoctor-ci/advanced> → **Danger zone**
→ **Make private**.

Being public is safe because the automatic approval is narrow: it only ever
covers repositories that are already public, and everything else waits for
you.
