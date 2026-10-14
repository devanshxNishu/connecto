# Writing the note

A LinkedIn connection request can carry a short note. Connecto builds one per person from your **templates**: plain
text with `{placeholders}`. If you do not write any, a few friendly built-in templates are used, so it works out of the
box. The setup page's *Message* tab shows live examples as you type, and `connecto preview` does the same in the terminal.

## How long can the note be?

| Account | Note limit |
|---|---|
| Free | 200 characters |
| Premium / Sales Navigator | 300 characters |

Set `message.plan: free` or `premium`. Connecto shortens every note to fit - it drops optional parts first, then whole
sentences, and only as a last resort cuts at a word with "...". (`message.max_note_chars`, 40 to 300, overrides the
plan's number - for example if LinkedIn changes its limits.) LinkedIn also limits how many personal notes a free account
can add each month - a handful, at the time of writing. When LinkedIn will not let the account add a note,
`message.if_note_unavailable` decides:

- `plain` (default): send a plain invitation without a note, and try notes again in a week;
- `stop`: pause the run until notes are available again.

`message.send_note: false` sends plain invitations only.

## Placeholders

```yaml
message:
  templates:
    - "Hi {first_name}, I'm {me_first_name}, {me_about}. Your work[[ at {company}]] caught my eye - would love to connect."
    - "Hi {first_name}! {me_first_name} here, {me_about}. Love what you're building at {company|your company} - let's connect."
```

| Syntax | Meaning |
|---|---|
| `{first_name}` | A value from the person; a first name written all in capitals or all in lower case is shown as a name (VISHNU -> Vishnu). Also `{full_name}`, `{last_name}`, `{company}`, `{title}`, `{headline}`, `{location}`, and anything else the source stored (a CSV column, a YC `{batch}`, `{industry}`, `{one_liner}` ...). |
| `{me_first_name}`, `{me_about}`, `{me_project}`, `{me_project_blurb}` | Values from the `me:` block of your settings. Any extra key you add under `me:` is available as `{me_<key>}`. |
| `{company\|your company}` | A fallback, used when the value is missing. |
| `[[ at {company}]]` | An optional part. It is dropped when a value inside it is missing, and dropped first when the note is too long. |
| `{hook}` | One opening line written by the optional AI helper - see [ai.md](ai.md). |

A template that needs a value the person does not have (outside `[[ ]]` and without a fallback) is simply **skipped for
that person**, so one list can mix templates for different kinds of people. Several templates are rotated at random, so
your notes do not all read the same. If no template fits a person, they get a plain invitation.

Values are inserted in one pass, so text copied from someone's profile can never inject template syntax or commands.
Mistakes in a template (an unbalanced `[[`, a stray `{`) are listed under "Worth a look" by `connecto preview` and in the
*Message* tab.

## Examples

For somebody you know why you want to meet:

```yaml
me:
  first_name: Asha
  about: a product designer in Pune
  project: NINA
  project_blurb: an AI personal stylist
message:
  templates:
    - "Hi {first_name}, I'm {me_first_name}, {me_about}, building {me_project}[[ - {me_project_blurb}]]. Your work[[ at {company}]] is close to what I'm exploring - would love to connect."
```

For people at a conference:

```yaml
message:
  templates:
    - "Hi {first_name}, I'm {me_first_name}, {me_about}. I'm attending the same conference as you[[ - looking forward to {company}'s talk]]. Would be great to connect."
```

Notes should be honest and true: say who you are and why you are reaching out. A note that is specific, short and
sincere is accepted far more often than a clever one - and it keeps your account healthy.

## What a note will look like

`connecto preview` writes a few example notes (`--notes 5` for more) without sending or visiting anything, for example:

```
Hi Maya, I'm Asha, a product designer in Pune. Your work at Acme Pay caught my eye - would love to connect and follow what you're building.
```
