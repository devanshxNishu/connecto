# Troubleshooting

Start with:

```bash
connecto doctor            # everything that can be checked without touching LinkedIn
connecto doctor --online   # also makes one tiny request to your AI provider
connecto doctor --launch   # also starts a throw-away browser to prove it opens
connecto report            # what happened lately, and any pause
```

and look at the end of `logs/run.log` in the Connecto folder (`connecto config path` shows where it is). Logs never contain
your keys, but check them before pasting them anywhere.

## Setup

**"No settings found ... Run `connecto init` first"** - there is no `config.yaml` yet in the folder Connecto is looking at.
It uses `--home`, then `CONNECTO_HOME`, then `~/.connecto`. `connecto config path` shows which one it is using.

**"Your settings file could not be read"** - `config.yaml` is not valid YAML (an unclosed bracket, a tab where spaces
belong). The message names the file; your editor or any YAML validator will point at the line. Every save through
Connecto keeps the previous version as `config.yaml.bak`.

**"Not ready to send yet"** - the first name is missing, or the risk note has not been accepted. Fix it in the setup page
or with `connecto init`.

**Numbers in my settings changed by themselves** - the safety ceilings (see [safety.md](safety.md)) pull a number back
and `connecto config check` lists each one as a note.

## The browser

**"No browser could be started"** - install Google Chrome, or run `python -m playwright install chromium` once. To use
Edge, set `browser.channel: msedge`.

**"No desktop session found"** - Connecto opens a visible window, which needs a desktop you are logged in to. It cannot
run over SSH, in a container or on a bare server. Run it on your own computer.

**Google sign-in does not work in the window** - Google may refuse to sign in inside an automated browser. Sign in with
LinkedIn's e-mail and password (or its one-time code). If you only ever used "Continue with Google", use *Forgot
password* on LinkedIn once to create a password.

**"another Connecto run is in progress"** - a run (or the setup page's *Send* button) is already using the browser
profile; two at once would corrupt it. If a crash left the lock behind, it expires by itself after two minutes.

**The window flashes and closes** - run `connecto run --dry-run --limit 1` in a terminal and read the lines it prints.

## Sending

**"Nobody to invite"** - the queue is empty. `connecto preview` says how many people are stored, how many match your
filters and how many are waiting; loosen a filter, add a source, or `connecto fetch`.

**"Outside active time"** - it is outside the hours or days you chose. `connecto run --ignore-hours` overrides it for one run.

**"Paused until ..."** - you paused it, or LinkedIn showed a limit (two days) or a security check (one day). Look at your
LinkedIn account in your own browser first, then `connecto resume`.

**"LinkedIn is not letting this account add personal notes right now"** - free accounts can add only a few personal notes
a month. Connecto sends plain invitations for a week, then tries notes again; `connecto resume --notes` tries sooner. A Premium
account lifts the limit; or set `message.send_note: false` to stop expecting notes.

**Everyone ends up "no_connect_option" or "error"** - LinkedIn probably changed its page. Every selector lives in
`connecto/sel.py`; `connecto run --dry-run --limit 1` shows what it sees. Please open an issue with the (redacted) log lines.
Profiles that failed are not retried unless you pass `--retry-errors`.

**A person shows as `already_pending` or `already_connected`** - Connecto noticed an earlier request or an existing
connection and left them alone. Nothing to fix.

**"needs_email"** - LinkedIn wanted the person's e-mail address to connect. Connecto never supplies one, and moves on.

## People and sources

**The YC source finds nobody** - check `connecto fetch --only yc` for the message; a filter such as `industries: [fintech]`
needs the word YC actually uses. Set `engine: playwright` if the plain download is being blocked.

**A CSV gives "no LinkedIn profile URL"** - the file needs a column with personal profile addresses
(`https://www.linkedin.com/in/...`). Company and Sales Navigator links are not personal profiles. Name the column with
`columns: {linkedin_url: "Your Header"}` if it is not recognised.

**A web page gives nothing** - the names probably appear only after JavaScript runs; try `render: true`.

**A LinkedIn search stops early** - it stops the moment LinkedIn shows a verification, and has a daily page budget.

## The AI helper

`connecto ai test` tells you what is wrong. The usual causes: no key saved (`connecto ai key`), a key for a different
provider, a rate limit (wait, or lower `ai.max_calls_per_day`), a local model that is not running (`ollama serve`) or
not pulled (`ollama pull llama3.2`). A failing helper never stops a run: notes simply carry on without the extra line.

## Starting over

- Forget the saved LinkedIn session: `connecto logout`.
- Forget the people and the history but keep your settings, key and session: write the history down first
  (`connecto export --out history.csv`), then delete `connecto.sqlite` in the Connecto folder. Without that record,
  people you already invited could be invited again.
- Remove everything: delete the Connecto folder. Your LinkedIn account is not touched.

## Still stuck?

Open an issue (see [CONTRIBUTING.md](../CONTRIBUTING.md)) with your operating system, Python version, the output of
`connecto doctor`, and the relevant lines of `run.log` - **with names, links and anything that looks like a key removed**.
Never paste `config.yaml` without checking it, or `.env` at all.
