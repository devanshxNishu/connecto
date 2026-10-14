# Security policy

Connecto runs on a person's own computer, drives their own LinkedIn session and can keep an API key, so security reports
are taken seriously. Thank you for taking the time to send one.

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

- Use GitHub's private reporting: on the repository, open the **Security** tab and choose **Report a vulnerability**.
- If that button is not there, open a public issue that says only "I have a security report" - **no details** - and a
  maintainer will arrange a private channel.

Please include what you found, how to reproduce it (a minimal configuration, the commands you ran, your operating system
and Python version) and what you think the impact is. The maintainers will acknowledge a report as soon as they can, work
out a fix with you, and credit you in the release notes unless you prefer to stay anonymous. Please give them a reasonable
time to release a fix before you publish details.

*(Maintainers: turn on "Private vulnerability reporting" under Settings -> Code security so the button exists.)*

## What counts

Reports about any of these are very welcome:

- **A safety limit that can be exceeded**, or a run that fails to stop when it should (the ceilings in `connecto/limits.py`,
  the stop conditions in `connecto/runner.py`, the single-run lock, the pause logic).
- **Real sending without a person's confirmation** - anything that sends invitations from `--dry-run`, from the setup
  page without its confirmation, or from a code path nobody asked for.
- **A secret that escapes**: an API key printed, logged, written to `config.yaml`, shown by the setup page, put in an
  exported file, or sent to any server other than the provider it belongs to.
- **The local setup page** (`connecto ui`): reaching it from another computer or another website (CSRF, DNS rebinding,
  cross-origin reads), bypassing its token, script injection through a person's name, title or note, or anything that lets
  a web page run code on the computer.
- **Input handling**: path traversal or request forgery through the `webpage` or `csv` sources, a crafted CSV or web page
  that crashes a run or injects spreadsheet formulas into an export, or a crafted AI reply that gets past `ai/guard.py`
  into a note.
- **The schedulers**: a way to make the installed launchd, cron or Task Scheduler entry run something other than Connecto.
- **Privacy**: data reaching an AI provider beyond the few public facts documented in [docs/ai.md](docs/ai.md) (never a
  name, profile link, location, e-mail address or phone number).

## What does not count

- **"LinkedIn does not allow automation."** That is the documented risk of the whole tool (see
  [docs/safety.md](docs/safety.md)), not a vulnerability. Account restrictions are not security reports.
- **Things that need the attacker to already control the computer or the person's `config.yaml`.** `config.yaml` is trusted
  the way a script you run on your own computer is trusted - copy a stranger's config only after reading it. (A way to
  *make a person run someone else's config without knowing* would count.)
- **Weaknesses in a dependency itself** (Playwright, requests, PyYAML ...): please report those upstream. Connecto *using*
  one unsafely does count.
- **Denial of service by the person against themselves**, such as a huge CSV they chose to import - the documented size caps
  already apply.

## The security model in short

- Everything stays on the person's computer: one folder holding settings, people, history, the browser profile and logs.
  There is no server of ours and no telemetry.
- The browser profile in that folder **is a logged-in LinkedIn session**: it should be treated like a password and never be
  shared, committed or uploaded. `.gitignore` keeps it out of the repository.
- API keys live only in `.env` in that folder (mode 0600), under names that say they are keys, and are masked in logs.
- The setup page listens on `127.0.0.1` only and needs a random per-launch token (as a cookie and as a header), checks
  `Host`, `Origin` and `Sec-Fetch-Site`, accepts JSON only, caps request size, sends a nonce-based Content-Security-Policy
  and never sends a saved secret back to the browser.
- Anything that comes from a stranger's web page or from a language model is treated as hostile input.

## Testing

Please test only against your own installation, with your own throw-away data folder (`--home` or `CONNECTO_HOME`), and
never against LinkedIn accounts or services you do not own. Do not try to defeat LinkedIn's own protections; that is not
what this policy is about and nobody here can authorise it.
