## What this changes and why

<!-- One or two sentences. Link the issue if there is one. -->

## How I tested it

<!-- Commands you ran, and what you could NOT test (a real LinkedIn page, macOS, Windows ...). -->

## Checklist

- [ ] `python -m pytest -m "not browser"` passes (and `-m browser` if I touched the browser, selectors, the setup page or a browser-based source)
- [ ] New behaviour has a test, and a new safety rule has a test that fails without it (I broke the code on purpose to check)
- [ ] The documentation describes what the code now does
- [ ] It does **not** raise a safety ceiling, hide that a program is driving the browser, handle a LinkedIn password, store e-mail addresses or phone numbers, send more than a few public facts to an AI provider, or phone home
- [ ] Nothing secret (keys, `config.yaml`, `.env`, a browser profile, real names or profile links) is in the diff, the tests or the description
