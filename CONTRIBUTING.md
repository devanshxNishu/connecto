# Contributing

Thank you for helping. Connecto is a small, careful program, and most of what makes it good is what it *refuses* to do,
so please read the short list of promises below before you start.

## What this project will not do

These are not up for negotiation, and a pull request that breaks one will be closed however well it is written:

- **Raise or bypass the safety ceilings** in `connecto/limits.py`, or add a setting, flag or environment variable that does.
- **Hide that a program is driving the browser** - no fingerprint spoofing, no stealth plug-ins, no automation-flag
  tricks - or **solve or sidestep a CAPTCHA, verification or restriction notice.** It stops and tells the person instead.
- **Handle a LinkedIn password.** People sign in by hand in the browser window.
- **Send messages**, scrape profile details beyond what a connection note needs, or **store e-mail addresses or phone
  numbers.**
- **Phone home.** No telemetry, no accounts, no server of ours. Data goes to LinkedIn (through the person's own browser),
  to the places a person configures, and - only if they switch it on - to the AI provider they chose.
- **Send more than a few public facts to an AI provider**, or trust what a model writes without `ai/guard.py`.
- **Make real sending happen without a person.** A real send needs a confirmation, or a schedule that someone installed.

Everything else is open: new sources of people, better note templates, other languages, other AI providers, clearer
documentation, scheduler fixes for your operating system, and above all **fixes when LinkedIn changes its pages.**

## Good first contributions

- **LinkedIn changed something and sends fail.** The fix is nearly always in `connecto/sel.py`, `connecto/connect.py`,
  `connecto/browser.py` or `connecto/sources/linkedin_search.py`, together with the stand-in page in `tests/mock_linkedin.py`
  or `tests/mock_search.py`. A redacted `run.log` excerpt in the issue helps enormously.
- **macOS and Windows reports.** The schedulers there are covered by unit tests, but most real-world testing has been on
  Linux. "I installed the schedule on macOS 15 and it ran at 10:41" is a valuable contribution.
- **Documentation** that a newcomer could not follow. If you got stuck, the page is wrong, not you.
- **A source** (see "Adding things" in [docs/architecture.md](docs/architecture.md)), with a local stand-in test.

## Setting up

```bash
git clone https://github.com/devanshxNishu/connecto.git
cd connecto
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m pytest -m "not browser"                        # about 20 seconds, no browser needed
python -m playwright install chromium
python -m pytest -m browser                              # a few minutes: a real Chromium against a stand-in LinkedIn
```

Use a throw-away data folder while you experiment, so you never touch your real settings or LinkedIn session:

```bash
export CONNECTO_HOME=$(mktemp -d)        # or pass --home to any command
```

**Never run a real send (`connecto run` without `--dry-run`) against your own LinkedIn account while developing.** Everything
that needs LinkedIn is tested against stand-ins on your own machine, and the test suite fails loudly if a test tries to
reach the real internet.

## How a change should look

- **Small and boring.** Prefer the standard library; a new dependency needs a very good reason. Python 3.10 must keep working.
- **Comments say why**, not what. When you make a safety decision, write the reason next to it.
- **Every behaviour has a test, and every safety rule has a test that fails without it.** After you write the code, break it
  on purpose (delete the check, flip the comparison) and make sure the test goes red. If it stays green, the test is not
  testing the rule.
- **Tests only talk to this machine.** Use the stand-ins in `tests/` (`mock_linkedin.py`, `mock_search.py`,
  `fake_ai_server.py`) rather than the real internet; `tests/conftest.py` enforces it.
- **Nothing secret in logs, errors or test output.** Keys live in `.env` only and are redacted from logs. If you add a new
  place that prints configuration, check that a key cannot reach it.
- **People's names, titles and notes come from strangers' pages.** The setup page builds its DOM with `textContent`
  only; keep it that way, and treat anything from a web page or a model as hostile input.
- Keep the documentation true: if you change behaviour, change the page that describes it in the same pull request.

## Pull requests

1. Fork, branch, and make your change with tests.
2. `python -m pytest -m "not browser"` passes (and `-m browser` if you touched `connect`, `browser`, `sel`, the setup page
   or a source that uses a browser).
3. Fill in the checklist in the pull request template. Say what you tested **and what you could not test.**
4. Be patient and kind in review; reviewers are volunteers and so are you.

## Reporting bugs

Open an issue and include your operating system, Python version, the output of `connecto doctor`, and the relevant lines
of `logs/run.log` - **with names, profile links and anything that looks like a key removed.** Do not paste `config.yaml`
without reading it, and never `.env`.

If the problem is a safety problem - a limit that was exceeded, a run that did not stop, a key that was printed, a way
into the setup page from another website - **do not open a public issue.** Follow [SECURITY.md](SECURITY.md).

## Conduct

Be respectful and assume good faith. Harassment, personal attacks and discrimination are not welcome here, and
maintainers may remove comments, close issues and block people who engage in them. This project is about meeting
people honestly; please treat each other that way too.
