# Sources and filters

A **source** is where people come from. You can have several; they all feed one pool, and one set of **filters**
(`targets:`) narrows that pool. Add sources in the setup page (*People* tab) or in `config.yaml` under `sources:`.

```yaml
sources:
  - type: yc                      # an example of each; use only what you need
    batches: [S26, F26]
    industries: [fintech]
  - type: csv
    path: ~/leads.csv
  - type: webpage
    urls: ["https://example.com/speakers"]
  - type: linkedin_search
    queries: ["head of design fintech india"]
```

Every source also accepts `enabled: false`, which keeps it in the file but switches it off. `connecto fetch` collects
people from all enabled sources (`--only yc` for one kind, `--limit N` to stop early, `--refresh` to ignore what was
fetched before). A send run tops the pool up by itself when it is empty, and a scheduled run does so every time
(`schedule.fetch_before_send`, on by default).

People are stored once, whichever source found them (a person is identified by their LinkedIn profile address), and
nobody is ever invited twice.

## `yc` - the Y Combinator directory

Founders of YC companies. The company list comes from a public, daily-refreshed mirror
(`yc-oss.github.io/api/companies/all.json`, cached for 24 hours); each company's founders, with their LinkedIn links,
come from that company's public page on ycombinator.com. **Your company filters are applied to the list first**, so
"fintech in India, last two batches" fetches a few hundred pages instead of six thousand. Newest batches are fetched
first, the work is resumable, and it is polite: at most two requests at a time, a pause between them, back-off on
errors, and a full stop after repeated blocks. Please be considerate and keep to the site's terms.

| Option | Meaning |
|---|---|
| `batches` | `S26`, `W26`, `Summer 2026` ... Empty means every batch. |
| `industries`, `tags`, `regions` | Words YC uses for its companies (`fintech`, `B2B`, `India` ...). Any one of them matching is enough. |
| `keywords`, `exclude_keywords` | Words in the company's name, one-liner, description, tags or industry. |
| `company_status` | `Active`, `Acquired`, `Public`, `Inactive`. |
| `only_top_companies` | `true` keeps only the companies YC marks as top companies. |
| `max_team_size` | Skip companies with a larger team. |
| `include_former_founders` | Off by default: only people listed as active founders. |
| `engine` | `requests` (default) or `playwright` - load the pages in a real browser window instead, slower but it can get past a bot check. |
| `workers`, `delay_seconds`, `cache_hours` | Fine-tuning. Workers never exceed 2. |

Each founder is stored with the company's fields, so notes can use `{batch}`, `{industry}`, `{one_liner}`, `{tags}`,
`{company}`, and the filters keep working later without fetching again. The exact words YC uses for industries and tags
are the ones in the filters of [ycombinator.com/companies](https://www.ycombinator.com/companies).

## `csv` - a spreadsheet

Any CSV (or `;`, tab or `|` separated) file with a column holding LinkedIn profile addresses: LinkedIn's own
*Connections* export, a Sales Navigator or Apollo export, your CRM, a list you made. Columns are recognised by their
header (`Name`, `First Name`, `Company`, `Job Title`, `Person Linkedin Url`, `Position` ...), and the header row may
come after a few lines of notes, as LinkedIn's export has. Everything else in the file rides along as extra fields
you can use in templates and keyword filters - **except e-mail addresses and phone numbers, which are never stored.**

| Option | Meaning |
|---|---|
| `path` | The file. `~` is expanded; a relative path is looked for in `<home>/data/imports` and then the current folder. |
| `columns` | Say which column is which when guessing fails: `columns: {full_name: "Contact", linkedin_url: "Profile"}`. |
| `delimiter` | `,` `;` `tab` or `|` when it cannot be detected. |

Limits: 60 MB and 100,000 rows per file. Rows without a personal profile link (company pages, empty cells) are skipped,
and the fetch report says how many.

## `webpage` - a page that links to profiles

A team, speakers, alumni, committee or "about us" page where people's names link to their LinkedIn profiles.
Connecto reads the links and works out each person's name (and title or company, when the page shows one nearby).

| Option | Meaning |
|---|---|
| `urls` | One or more page addresses. One request each, never a burst. |
| `render` | `true` loads the page in a browser first - for pages that build their list with JavaScript. |
| `allow_private` | Off. Pages on private or internal addresses are refused unless you set this on purpose (for a page on your own network). |

The downloader is careful: http(s) only, no redirects to private addresses, at most 3 redirects, 5 MB, HTML only. If a page
yields nothing, the report tells you whether `render: true` might help.

## `linkedin_search` - a LinkedIn people search

Reads LinkedIn's own people-search results through the same signed-in browser that sends the invitations. Connecto only
*reads* the result pages: slowly, a few per run, with a hard daily page budget, and it stops the moment LinkedIn asks for
a verification. Give plain keyword `queries`, or - the most flexible way to use LinkedIn's location, industry and
company filters - paste the address (`urls`) of a people search you set up yourself.

| Option | Meaning |
|---|---|
| `queries` | e.g. `["head of design fintech india"]`. |
| `urls` | Addresses of searches you built on linkedin.com (only linkedin.com people-search addresses are accepted). |
| `max_pages` | Result pages per query per fetch, default 3, at most 10 (and 20 a day in total). |
| `skip_first_degree` | On by default: people you are already connected with are skipped. |
| `delay_seconds` | Pause between result pages, default 6-15 seconds. |

LinkedIn changes its pages without notice. If searches stop finding anyone, the reader in
`connecto/sources/linkedin_search.py` (`parse_result_cards`) and the stand-in in `tests/mock_search.py` are what to fix.

## By hand

```bash
connecto add https://www.linkedin.com/in/jane-doe/
connecto add --name "Jane Doe" --company "Acme" --title "CEO" https://www.linkedin.com/in/jane-doe/
connecto add --file people.txt        # one per line: "Jane Doe, Acme, CEO, https://www.linkedin.com/in/jane-doe/"
connecto add --csv leads.csv
```

A line may hold just a link, or a name, company and title around it in any order, separated by commas, tabs, `|` or `;`.
Hand-added people are **always wanted**: filters never drop them and they go to the front of the queue. If a source
had already found the same person, you now own that row (it is not dropped when the source's filters change). A link
without a name gets a plain invitation - Connecto never guesses a name - so add the name if you want a personal note.

## Never contact

```bash
connecto skip https://www.linkedin.com/in/someone/
connecto skip --undo https://www.linkedin.com/in/someone/
```

A skipped profile is never invited, even if a source finds them again. (People you have already invited or are connected
with are left alone by `skip`.)

## Filters (`targets:`)

Applied to everyone from a source (not to people you added by hand). Every list is optional; empty means no restriction.

| Filter | Keeps a person when ... |
|---|---|
| `keywords` | any of the words appears in their headline, title, company, description or tags |
| `exclude_keywords` | none of the words appears there |
| `titles` / `exclude_titles` | their title (or headline, when there is no title) contains / does not contain any of the words |
| `locations` | any of the words appears in their location |
| `titles_regex` / `exclude_titles_regex` | their title matches / does not match a regular expression (for experts) |

Matching ignores case. Short words (three letters or fewer, like `ai` or `ml`) match whole words only, so `ai` finds
"AI-powered" but not "retail". Common shorthands are expanded: `ai` also looks for "artificial intelligence", `fintech`
for "financial", `ml` for "machine learning", `devtools` for "developer tools", and so on.

`connecto preview` shows who is selected and why, before anything is sent.

### Who comes first

`order: priority` (default) sends to hand-added people first, then newest YC batches first, shuffling people of equal
priority. The alternatives are `random`, `newest_added` and `oldest_added`.

### Describing the audience in words

With the optional AI helper on, `connecto ai suggest "founders of fintech startups in India"` proposes `keywords`,
`titles`, `locations`, `exclude_keywords` and search queries. It prints them and changes nothing; add `--apply` to put
them into your filters (the setup page can also add search queries and YC batches). Only your sentence is sent to the
provider. It needs an AI provider set up - see [ai.md](ai.md) - but not the note helper switched on.
