# Examples

Recipes for the settings file. Every `.yaml` here is a complete, valid starting point (the tests load each one), with
sample values you should replace - above all the `me:` block, which says who you are.

| File | What it shows |
|---|---|
| [`yc-fintech-india.yaml`](yc-fintech-india.yaml) | Founders from the Y Combinator directory, filtered by batch, industry and region |
| [`csv-list.yaml`](csv-list.yaml) | A spreadsheet you already have, narrowed by title and location |
| [`conference-speakers.yaml`](conference-speakers.yaml) | A web page of speakers, a team or alumni, with your own note templates |
| [`people-search.yaml`](people-search.yaml) | A LinkedIn people search, read slowly through your own browser |
| [`free-ai-gemini.yaml`](free-ai-gemini.yaml) | An opening line per person from a free AI key (key saved with `connecto ai key`, never in the file) |
| [`local-ai-ollama.yaml`](local-ai-ollama.yaml) | The same with a model on your own computer: no key, nothing leaves it |
| [`leads.example.csv`](leads.example.csv) | The shape of a spreadsheet Connecto can read (all made-up people) |
| [`people.example.txt`](people.example.txt) | The format for `connecto add --file` |

## Using one

**With the setup page (easiest):** `connecto ui`, and fill in the *People* and *Message* tabs. The examples show what to type.

**With files:** run `connecto init` once (it creates your Connecto folder and asks you to read and accept the risk note),
then either

- copy the `sources:` and `targets:` (and `message:` / `ai:`) parts of an example into the `config.yaml` that
  `connecto config path` points to, keeping your own `me:` block; or
- on a fresh folder, copy the whole example over `config.yaml` and run `connecto init` again - it offers what is in the
  file as the answer to each question, so you can mostly press Enter (a backup, `config.yaml.bak`, is kept).

Then:

```bash
connecto config check      # is the file understood?
connecto fetch             # collect the people
connecto preview           # who is waiting, and what each note would say - sends nothing
connecto run --dry-run --limit 2
```

An example never switches sending on by itself: the risk note is accepted by you, in `connecto init` or the setup page,
and a real `connecto run` asks you to confirm before it sends anything.

The people in `leads.example.csv` and `people.example.txt` are made up and their links lead nowhere. Please do not send
requests to them.
