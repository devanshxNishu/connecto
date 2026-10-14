# The optional AI helper

Connecto works fully without AI. If you switch the helper on, it does exactly one thing during a run: for each person it
may write **one opening sentence** - the `{hook}` in your template - from a few public facts. Everything else (who is
chosen, the rest of the note, every click) is ordinary code.

```yaml
ai:
  enabled: true
  provider: gemini            # gemini | groq | openrouter | ollama | custom
message:
  templates:
    - "Hi {first_name}, {hook} I'm {me_first_name}, {me_about} - would love to connect."
    - "Hi {first_name}, I'm {me_first_name}, {me_about}. Would love to connect."     # used when there is no hook
```

When the helper is on and you have written no templates of your own, two built-in templates with `{hook}` are used
next to the plain ones. If you wrote your own and none contains `{hook}`, `connecto preview` tells you the helper will
not be used.

## Providers

| Provider | Key | Notes |
|---|---|---|
| `gemini` | `GEMINI_API_KEY` from [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | Free tier on the Flash models, at the time of writing. |
| `groq` | `GROQ_API_KEY` from [console.groq.com/keys](https://console.groq.com/keys) | Free tier with rate limits. |
| `openrouter` | `OPENROUTER_API_KEY` from [openrouter.ai/keys](https://openrouter.ai/keys) | Models whose id ends in `:free` cost nothing; small daily limits without credits. |
| `ollama` | none | A model on your own computer ([ollama.com/download](https://ollama.com/download), then `ollama pull llama3.2` or similar). Nothing leaves your machine. |
| `custom` | optional | Any OpenAI-compatible server - LM Studio, llama.cpp, vLLM, Together, Cerebras, Mistral ... Set `base_url`, optionally `model` and `api_key_env`. |

Free tiers change without warning, so Connecto does not hard-code model names: it asks the provider which models exist
and picks a suitable small chat model (set `ai.model` to choose one yourself). `connecto ai providers` lists the
providers with current notes; `connecto ai test` makes one tiny request and tells you plainly what is wrong if it
fails (wrong key, no models, server down).

## Your key

Put it in the setup page's *Message* tab, or:

```bash
connecto ai key                 # asks for it, hidden
connecto ai key --stdin         # or pipe it in:  pass show gemini | connecto ai key --stdin
connecto ai key --remove
```

The key is stored only in `.env` inside the Connecto folder, readable only by you (mode 0600). It is never written to
`config.yaml`, never printed, never logged, and never sent anywhere but to the provider's own server in the
`Authorization` header. Some further rules protect it:

- `.env` only ever holds names that say they are keys (`..._KEY`, `..._TOKEN`, `..._SECRET`, `..._PASSWORD`), so a
  settings file cannot use it to set `PATH` or `HTTPS_PROXY`.
- A settings file cannot make Connecto send a key from your shell environment to a server of its choosing: a built-in
  provider's key is only ever sent to that provider, and `ai.api_key_env` may only name a key that Connecto itself saved.
- Over the network, `http://` is allowed only for servers on your own machine (such as Ollama); everything else needs `https://`.

## What the provider sees

For each person, at most these facts - and only the ones that exist - are sent, each cut to 200 characters:

- job title, company, headline, the company's one-line description, industry, and up to five tags;
- your own `me.about` and `me.project` lines (so the model can pick a fitting angle);
- a short instruction to write one sentence of at most about 22 words.

**Never** sent: the person's name, profile link, location, e-mail, phone, or anything else. A field that reads like an
instruction to an AI ("ignore the above ...") is dropped before sending. Whatever the provider does with prompts is
governed by its own policy: on some free tiers, prompts may be used to improve their products, so if that matters to you,
choose `ollama` (nothing leaves your computer) or a paid tier.

## What the model may write

A model is a stranger who could be steered by a poisoned profile, so its answer is validated by `connecto/ai/guard.py`
before it can reach a real invitation. It is used only if it is **one plain sentence, three to 28 words, grounded in the
facts it was given**, and it has no links or domain names, e-mail addresses, phone numbers, emoji, markup, questions,
greetings, refusals, flattery ("amazing", "incredible"), or invented specifics. Anything else is thrown away and that
person simply gets a template without a hook. A line is also sized to the room the template leaves: LinkedIn's free
note holds only 200 characters, so the model is asked for a shorter line, and a line that does not fit is never cut -
it is dropped.

## Budget and failures

- **`ai.max_calls_per_day`** (default 60) caps the calls. Lines are cached per person and set of facts, so a person is
  never asked about twice. When the budget is used up, notes carry on without hooks.
- A rate limit pauses the helper for a while (honouring the provider's `Retry-After`); repeated server or network failures rest it
  for five minutes; three wrong-key or wrong-setting failures in a row switch it off for the rest of that run.
- None of this ever stops a send run. The worst case is a note without a hook.

## Choosing who to meet in plain words

```bash
connecto ai suggest "founders of fintech startups in India"
connecto ai suggest --apply "heads of design at climate startups"
```

The helper turns a sentence into suggested `keywords`, `titles`, `locations`, `exclude_keywords` and LinkedIn search
queries (and, for YC founders, batches, industries and tags). On the command line it prints them and changes nothing;
`--apply` merges the four filter lists into your settings. In the setup page (*People* tab, when the helper is on),
*Add to my filters* also adds the search queries and the YC batches and industries to the matching sources, creating
them if needed, and you review everything before saving. Only your sentence is sent to the provider - nothing about any
person. You need a provider set up (a key, or a local model), but not the note helper switched on.
