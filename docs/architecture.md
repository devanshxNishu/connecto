# Architecture (for contributors)

Connecto is a small, boring program on purpose: a person drives their own browser slowly, under limits that live in
code, and everything stays on their computer. Read this page, then [CONTRIBUTING.md](../CONTRIBUTING.md).

## The shape of it

```
 sources ──► store (people) ──► planner ──► runner ──► connect ──► LinkedIn (your browser)
 yc csv      sqlite              candidates  limits      one profile
 webpage     + invites           + note      + stops     at a time
 linkedin_search + by hand       composer    + pauses
                                    ▲
                    optional AI hook (validated)
```

1. A **source** finds people and writes them to the **store** (SQLite). A person is a flat dict of strings, identified
   by their canonical LinkedIn profile address (`models.person_key`).
2. The **planner** answers "who is next and what would we say": it applies the sources' own selection and the `targets:`
   filters, drops everyone already handled, orders the rest, and composes a note per person (`notes`, optionally with an
   AI `{hook}`). It never opens a browser, which is why `connecto preview` and the setup page are instant and safe.
3. The **runner** is the only thing that sends. It applies the limits, holds a lock so two runs cannot overlap, opens the
   browser, walks the queue through **connect** (one profile: find *Connect*, add the note, send, report exactly what
   happened), records every outcome, and ends at the first stop condition.
4. The **command line** and the **setup page** are two thin faces over the same functions.

## Module map

| Module | Responsibility |
|---|---|
| `limits.py` | `HARD` ceilings and the quota arithmetic. Everything is clamped to them. |
| `config.py` | Defaults, load/save, `normalize()` (coerces types, clamps limits, never raises), `problems()` (what blocks a send). |
| `paths.py`, `util.py` | The home folder; atomic writes, the `.env` file and the one key-name rule, log redaction, text clean-up. |
| `models.py`, `store.py` | The person shape and URL canonicalisation; SQLite: people, invites, runs, kv, AI cache. |
| `sources/` | `base.Source` contract; `yc`, `csv_file`, `webpage`, `linkedin_search`; `REGISTRY` in `__init__.py`. |
| `targets.py`, `manual.py` | Filters over people; people added by hand and the never-contact list. |
| `notes.py`, `planner.py` | The template engine (pure functions); who is next, `NoteComposer`, `build_preview`, and searchable `review_queue` pages for CLI/UI. |
| `ai/` | `client.py` (OpenAI-compatible HTTP), `presets.py`, `prompts.py`, `guard.py` (validation of untrusted output), `assist.py`. |
| `browser.py`, `sel.py`, `connect.py` | Launching the browser, login checks and human-paced helpers; how things are recognised on LinkedIn pages; the per-profile flow. |
| `runner.py`, `lock.py`, `notify.py` | The send run and every stop condition; single-instance lock; desktop notifications. |
| `scheduler.py` | launchd / cron / Task Scheduler: pure builders plus a thin installer with an injectable runner. |
| `doctor.py`, `wizard.py`, `migrate.py` | `connecto doctor`; `connecto init`; bringing over the v1 `yc-founder-connect` settings and history. |
| `cli.py`, `ui/` | The command line; the local setup page (`server.py` + one static `index.html`). |

## Rules the code keeps (and tests enforce)

These are not style preferences; a change that breaks one is a bug.

- **Limits are code.** Nothing in a config file, the setup page or an environment variable raises `limits.HARD`.
- **A real send needs a person.** `connecto run` asks (or `--yes`), the setup page's *Send* needs a confirmation and the
  accepted risk note, a dry run never sends, and a scheduled run exists only because someone installed it.
- **No credentials.** Connecto never types, reads or stores a LinkedIn password. Login is done by hand in its window.
- **Keys live in `.env` only** (mode 0600), under names that say they are keys; they are never printed, logged, put in
  `config.yaml`, or sent anywhere but the provider they belong to; a settings file cannot point a key at another server.
- **Model output is hostile input.** It passes `ai/guard.py` or it is thrown away; a failing helper never stops a run.
- **Nothing but a few public facts reaches an AI provider** - never a name, link, location, e-mail or phone.
- **Stop at the first sign of trouble.** Limit dialogs, security checks, being signed out and repeated errors end the run
  and set a pause; no code path retries around them.
- **No stealth.** No fingerprint spoofing, no automation-flag hiding, no CAPTCHA handling.
- **Hand-added people are always wanted**, and nobody is invited twice.
- **The setup page is local-only**: `127.0.0.1`, a random per-launch token (cookie *and* header), Host/Origin checks,
  JSON-only writes, a body cap, a nonce-based CSP, write-only secrets, and a page that builds its DOM with
  `textContent` only, because names, titles and notes come from strangers' pages.
- **Tests never use the real internet.** `tests/conftest.py` makes any attempt fail loudly - including from the browser.

## Where LinkedIn lives

LinkedIn randomises its class names and changes layout without notice, so nothing is matched by CSS class:

- `sel.py` holds the accessible role and visible-name patterns (*Connect*, *Pending*, *Send without a note* ...);
- `connect.py` is the per-profile flow and maps everything that can happen to one of the `OUTCOMES`;
- `browser.py` has the login/checkpoint detection and the human-paced helpers;
- `sources/linkedin_search.py` reads result pages as text (`parse_result_cards` is a pure function).

When LinkedIn changes something and sends start failing, the fix is nearly always in one of those four places, together
with the stand-in page in `tests/mock_linkedin.py` / `tests/mock_search.py`.

## Testing

```bash
python -m pytest -m "not browser"      # offline suite, no browser
python -m playwright install chromium
python -m pytest -m browser            # Chromium against local stand-ins for LinkedIn and the setup page
```

- Everything talks to **stand-ins on this machine**: `tests/mock_linkedin.py` (profile pages and dialogs),
  `tests/mock_search.py`, `tests/fake_ai_server.py` (an OpenAI-compatible server with scripted replies), a stand-in YC.
- The command line is tested through `cli.main(argv)`; the wizard through an injected `Console`; schedulers through a fake
  command runner; the setup page through a real server and a real Chromium (light and dark, 360 px wide, keyboard only).
- Time and sleeping are injectable (`RunOptions.sleep`, a fake clock in the AI tests), so nothing waits.
- When you add a safety rule, add a test that **fails without it**: break the code on purpose and watch the test fail.

## Adding things

**A source of people.** Subclass `sources.base.Source` (cheap constructor, `fetch(sink)`, `accepts(row)`, `describe()`), add it
to `REGISTRY` and `config.SOURCE_TYPES`, give the setup page a card (`SOURCE_LABEL`, `defaultSource`), document it in
[sources.md](sources.md), and test it against a local stand-in. Never store e-mail addresses or phone numbers.

**An AI provider.** Add a `Preset` in `ai/presets.py` (any OpenAI-compatible endpoint) with hedged free-tier notes and a
privacy note, and add it to `config.AI_PROVIDERS`. Do not hard-code model names; rank the provider's own list.

**A note variable.** Person fields are available as `{field}` automatically. A new `me:` key is `{me_<key>}`.

## Packaging and the product name

`pyproject.toml` builds the `connecto` package and console script. If you want a different product name, run
`python scripts/rename.py NEWNAME` (add `--dry-run` first): it renames the package folder and every `connecto` /
`Connecto` / `CONNECTO` in code, tests, docs and metadata in one step, then run the tests.
