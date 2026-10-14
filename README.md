# Connecto

**A careful, local-first networking assistant for LinkedIn.**

Say who you would like to meet - founders from a certain batch, designers in your city, the speakers at a conference,
anyone you can describe or list - and Connecto sends a few personal connection requests a day from **your own browser,
on your own computer**. It works with no AI at all. If you want a more personal touch, a free API key (or a model
running on your laptop) can write one opening line per person.

**Website:** a one-page getting-started guide (install, sign in, first run, daily schedule) lives in
[`site/`](site/index.html). GitHub Pages publishes it at `https://devanshxnishu.github.io/connecto/` - see
[Development](#development) for the one-time setup.

> **Please read this first.** LinkedIn's User Agreement does not allow automated activity. Accounts that automate can be
> warned, limited or restricted, and no tool can promise otherwise. Connecto keeps the volume low (the ceilings are
> in the code, not just in settings), goes slowly, shows its browser window, and stops at the first sign of trouble -
> but it cannot make the risk zero. It is your account and your decision. Use it to reach people who might actually
> want to hear from you, with notes that are honest and true. More in [docs/safety.md](docs/safety.md).

## What it does

1. **You choose who.** Sources are pluggable: the public [Y Combinator](https://www.ycombinator.com/companies)
   directory (filter by batch, industry, region ...), a CSV file, any web page that links to LinkedIn profiles, a
   LinkedIn people search, or people you add by hand. Filters (keywords, titles, locations) apply to all of them.
2. **You write the note** - or use the friendly built-in ones. Templates fill in names and companies, and drop clauses
   that do not fit. Optionally an AI helper adds one grounded opening line per person.
3. **Connecto visits each profile slowly** in a Chrome window that you can watch, and clicks *Connect* the way you
   would - at most a handful per run, with random pauses, inside the hours you set.
4. **It runs by itself every day** if you want (macOS, Linux and Windows schedulers), and tells you when it needs you.

Everything - settings, people, history, your LinkedIn session - lives in one folder on your computer.

```
Hi Maya, I'm Asha, a product designer in Pune. Your work at Acme Pay caught my eye - would love to connect and
follow what you're building.
```

*(One of the built-in templates, as rendered for a sample profile.)*

## Quick start

You need **Python 3.10 or newer**, **Google Chrome** (or Microsoft Edge, or run `python -m playwright install chromium`
once), and a LinkedIn account. Connecto is developed and tested on Linux; the macOS and Windows schedulers are covered
by unit tests but have had less real-world use, so [please report](CONTRIBUTING.md) anything odd.

```bash
git clone https://github.com/devanshxNishu/connecto.git
cd connecto
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install .

connecto ui                          # a private setup page opens in your browser
```

The setup page walks you through it: read the risk note, tell Connecto your first name, pick who to meet, then press
*Sign in to LinkedIn* and sign in yourself in the window that opens (Connecto never asks for your password). Prefer the
terminal? `connecto init` asks the same questions.

Then, from the terminal:

```bash
connecto fetch                       # collect people from your sources
connecto preview                     # who is waiting, and what each note would say - sends nothing
connecto run --dry-run --limit 2     # watch it visit two profiles - sends nothing
connecto run --limit 3               # send your first three (it asks you to confirm)
connecto schedule install            # optional: do this every day by itself
```

`connecto doctor` checks that everything is in place and tells you what to fix if not.

## Choosing who to meet

Add one or more **sources** (in the setup page's *People* tab, or `sources:` in `config.yaml`). The full list of
options is in [docs/sources.md](docs/sources.md); recipes are in [`examples/`](examples).

| Source | What it reads | Example |
|---|---|---|
| `yc` | The public Y Combinator directory: companies from a public mirror, founders from each company's own page. Filters run *before* anything is fetched, so "fintech in India, last two batches" fetches a few hundred pages, not six thousand. | `batches: [S26, F26]`<br>`industries: [fintech]` |
| `csv` | Any spreadsheet with a LinkedIn profile column - LinkedIn's own *Connections* export, Apollo or Sales Navigator tool exports, your own list. Columns are recognised automatically. E-mail addresses and phone numbers are never stored. | `path: ~/leads.csv` |
| `webpage` | A team, speakers or alumni page that links to LinkedIn profiles. Careful downloader: http(s) only, no private addresses, size-capped. | `urls: ["https://example.com/speakers"]` |
| `linkedin_search` | A LinkedIn people search - keywords, or the address of a search you set up yourself. Read slowly through your signed-in browser, a few pages a run, with a daily page budget. | `queries: ["head of design fintech india"]` |
| by hand | `connecto add <profile link>` or paste links into the *People* tab. | `connecto add https://www.linkedin.com/in/jane/` |

On top of any source, **filters** narrow the list: `keywords`, `titles`, `locations`, and their `exclude_` twins.
People you add by hand are always wanted, whatever the filters say. To never contact somebody: `connecto skip <link>`.

Not sure how to put it into filters? `connecto ai suggest "founders of fintech startups in India"` turns a sentence into
suggested filters (needs an AI provider set up, see below; it only suggests - nothing changes unless you add `--apply`).

## The note

LinkedIn lets a connection request carry a short note: **200 characters on a free account, 300 on Premium / Sales
Navigator** (and free accounts can only add a few personal notes a month - at the time of writing). Set `message.plan` and Connecto keeps every
note inside the limit. If LinkedIn will not let your account add a note, Connecto sends a plain invitation (or pauses,
if you prefer: `message.if_note_unavailable: stop`).

Templates are plain text with placeholders:

```yaml
message:
  templates:
    - "Hi {first_name}, I'm {me_first_name}, {me_about}. Your work[[ at {company}]] caught my eye - would love to connect."
```

`{first_name}`, `{company}`, `{title}` come from the person; `{me_first_name}`, `{me_about}`, `{me_project}` from the
`me:` block of your settings; anything else your source provides (a CSV column, a YC `{batch}` ...) works too.
`{company|your company}` supplies a fallback, and `[[ text with {company} ]]` is dropped when the value is missing or
the note would be too long. A template that needs something a person does not have is simply skipped for them. Details
and more examples: [docs/notes.md](docs/notes.md).

## The optional AI helper

Off by default, and everything works without it. When on, it writes **one opening sentence per person** from a few
public facts - the `{hook}` in a template such as `Hi {first_name}, {hook} I'm {me_first_name}, {me_about}.`

- **Free options:** Google Gemini (AI Studio), Groq and OpenRouter's free models each give a free API key; Ollama (or LM
  Studio, llama.cpp ...) runs a model on your own computer with no key and nothing leaving it. Any
  OpenAI-compatible server works. Free-tier limits change often - check the provider's page.
- **What it sees:** the person's job title, company, headline and company description, industry and tags (whatever
  the source has), plus your own *about* and *project* lines. **Never** their name, profile link, location, e-mail or
  phone. On some providers' free tiers, prompts may be used to improve their products - pick Ollama if that matters.
- **What it may not do:** whatever a model writes is treated as untrusted. A line is used only if it is a single
  short sentence grounded in the facts it was given, with no links, e-mail addresses, emojis, questions or flattery.
  Otherwise that person simply gets a template without a hook.
- **Your key** is saved only in `.env` inside the Connecto folder (readable only by you) - never in `config.yaml`,
  never printed, never logged. A daily call limit (`ai.max_calls_per_day`, default 60) keeps free tiers safe.

Set it up in the *Message* tab, or `connecto ai providers`, `connecto ai key`, `connecto ai test`. See
[docs/ai.md](docs/ai.md).

## Safety

Connecto is built to be a cautious guest. The numbers below are **ceilings in the code**; settings and the setup
page can only go lower.

| | Default | Ceiling |
|---|---|---|
| Invitations per day | 8-12 (random each day) | 30 |
| Invitations per rolling week | 60 | 100 |
| Invitations per run | 4-7 | 20 |
| Pause between profiles | 1-3 minutes | never under 30 seconds |
| Active hours / days | 09:00-20:30, Monday-Saturday | - |
| Errors in a row before a run stops | 3 | 5 |

A run **stops at the first sign of trouble**: LinkedIn's invitation-limit dialog (and the next runs stay paused for two
days), a security check or restriction notice (paused for a day, and you get a desktop notification), being signed out,
or repeated unexplained errors. It never tries to get around a check.

What it **never** does: type or store your LinkedIn password; solve CAPTCHAs; send messages (only connection
requests); collect e-mail addresses or phone numbers; or pretend not to be automation (no stealth plug-ins or
fingerprint tricks - it simply goes slowly and stops early). Its browser window is visible by default so you can watch
and step in, which also means a server without a display cannot run it (`browser.headless: true` exists, but hidden
browsers are easier for LinkedIn to flag, so it is discouraged and `connecto doctor` warns about it).

Real sends need a person's confirmation: `connecto run` asks before sending, a scheduled run only exists if you
installed the schedule, and `--dry-run` never sends. Details and the whole risk note: [docs/safety.md](docs/safety.md).

## Running it every day

```bash
connecto schedule install            # uses the times in your settings (default 10:40 and 18:20)
connecto schedule status
connecto schedule uninstall
```

This creates a launchd agent (macOS), a cron entry (Linux) or an "interactive only" scheduled task (Windows) that runs
`connecto run --scheduled`. Each run waits a random few minutes, sends a handful of invitations inside your active
hours, and shows a desktop notification if it needs you (for example, LinkedIn asked you to sign in again). Because
Connecto needs a *visible* browser, **your computer must be on, awake and logged in** at those times; a Chrome window
opens for a few minutes. If the computer is off or asleep at a scheduled time, that run is skipped (macOS runs it when the
Mac wakes up, as long as it is still inside your active hours). See [docs/automation.md](docs/automation.md).

## Commands

| Command | What it does |
|---|---|
| `connecto ui` | The private setup page (local only). Everything below, with buttons. |
| `connecto init` | Guided setup in the terminal. |
| `connecto login` / `logout` | Sign in to LinkedIn once in Connecto's own browser window / delete the saved session. |
| `connecto fetch` | Collect people from your sources. |
| `connecto add`, `skip` | Add people by hand / never contact somebody. |
| `connecto preview` | Who is waiting and what each note would say. Sends nothing. |
| `connecto run` | Send invitations within your limits. `--dry-run` sends nothing; `--limit N`; `--yes` skips the question. |
| `connecto report` (`status`) | What was sent, what is coming, any pause. `export` writes the history as CSV. |
| `connecto pause` / `resume` | Stop sending for a while / lift the pause. |
| `connecto schedule install\|uninstall\|status` | Run by itself every day. |
| `connecto ai providers\|key\|test\|suggest` | The optional AI helper. |
| `connecto config path\|show\|check` | Where the settings are; print or check them. |
| `connecto doctor` | Check that everything is in place (`--online` also tests the AI helper). |
| `connecto migrate-v1 <folder>` | Bring over settings and history from the older `yc-founder-connect` scripts. |

Every command takes `--home PATH` (or set `CONNECTO_HOME`) to use a different folder. Exit codes: `0` fine, `1` needs
you (signed out, a security check or limit notice from LinkedIn, no browser, the browser window was closed, several
profiles in a row failed, or an invitation that could not be confirmed as sent), `2` settings problem, `3` another run is
in progress.

## Where your data lives

Everything is in one folder - `~/.connecto` by default, or wherever `--home` / `CONNECTO_HOME` points:

| File | What it is |
|---|---|
| `config.yaml` | Your settings. Safe to edit by hand; contains no secrets. |
| `.env` | API keys for the AI helper (mode 0600). |
| `connecto.sqlite` | The people, every invitation and its outcome, run history. |
| `browser/` | The automation browser's profile - **your LinkedIn session lives here; treat it like a password.** |
| `logs/run.log` | What each run did. |

Nothing is sent anywhere except to LinkedIn (through your browser), to the places you point sources at, and - only if
you turn it on - to the AI provider you chose. There is no account, no telemetry and no server of ours.
`config.yaml` is trusted like any file you run on your own computer: copy a stranger's config only after reading it.
The setup page binds to `127.0.0.1` only, behind a random per-launch token. See [SECURITY.md](SECURITY.md).

## Troubleshooting

Start with `connecto doctor`. The usual suspects:

- **"No desktop session found"** - Connecto needs a visible browser window, so it cannot run over SSH or on a bare
  server. Run it on your own computer.
- **Google sign-in does not work in the window** - Google may refuse to sign in inside an automated browser. Use
  LinkedIn's e-mail + password (or its one-time code) instead; if you never set a LinkedIn password, use *Forgot
  password* once.
- **"Personal notes unavailable"** - LinkedIn limits personal notes on free accounts. Connecto sends plain invitations
  meanwhile and tries notes again in a week (`connecto resume --notes` to try sooner).
- **It stopped and says it is paused** - LinkedIn showed a limit or a check. Deal with it in your own browser, wait,
  then `connecto resume`.
- **LinkedIn changed its page and invitations fail** - every selector is in `connecto/sel.py`; please open an issue
  with the (redacted) lines from `logs/run.log`.

More in [docs/troubleshooting.md](docs/troubleshooting.md).

## Using it with an AI assistant

[`skills/connecto/SKILL.md`](skills/connecto/SKILL.md) is a ready-made skill for Claude (or any assistant that reads skill
files). It helps you choose sources and filters, write honest notes and diagnose problems, and it is written never to send
anything for you, touch your LinkedIn credentials or take an API key in a chat. Coding agents working *on* this repository
should read [CLAUDE.md](CLAUDE.md).

## Development

```bash
pip install -e ".[dev]"
python -m pytest -m "not browser"    # fast, no browser
python -m playwright install chromium
python -m pytest -m browser          # the real browser against a mock LinkedIn
```

The test suite never touches the real internet or a real LinkedIn: a guard in `tests/conftest.py` makes any test that
tries fail loudly. Contributions are welcome - see [CONTRIBUTING.md](CONTRIBUTING.md) and
[docs/architecture.md](docs/architecture.md). Security issues: [SECURITY.md](SECURITY.md).

**The website.** `site/index.html` is the getting-started page. To publish it, open the repository on GitHub, go to
*Settings -> Pages -> Build and deployment* and choose **GitHub Actions** as the source (once). Every push to `main`
that changes `site/` then publishes it through `.github/workflows/pages.yml`. The page finds its own repository
address, so its GitHub links and the `git clone` command need no editing.

## FAQ

**Will my account be restricted?** Nobody can promise it will not. LinkedIn does not allow automation; Connecto reduces
the exposure (low volume, slow pace, early stops, your own browser) but does not remove it.

**Does it message people?** No - only connection requests, with an optional short note.

**Does it work with Sales Navigator?** It sends ordinary connection requests from linkedin.com profile pages, and the
CSV source reads Sales Navigator tool exports. Premium and Sales Navigator accounts get the longer 300-character note.

**Why not run it in the cloud?** On purpose: your session, your IP address and your browser stay on your computer.

**Can I raise the limits?** No. There is deliberately no switch for it.

## License

[MIT](LICENSE). Not affiliated with or endorsed by LinkedIn or Y Combinator.
