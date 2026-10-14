# Safety, limits and the honest risk

## The risk, plainly

LinkedIn's User Agreement does not allow software that automates activity on the site. Accounts that automate can be
warned, limited or restricted - sometimes without notice - and no tool can promise that yours will not be. Connecto is
designed to lower that risk, not to remove it:

- it keeps the volume low, with **ceilings in the code** that no setting can raise;
- it goes slowly, one profile at a time, with random pauses, inside hours you choose;
- it drives **your own browser, on your own computer**, with a visible window, so your account behaves as it normally
  does - same device, same place, same session;
- it stops at the first sign of trouble and never tries to get around a check.

It is your account and your decision. If your livelihood depends on your LinkedIn account, or you cannot afford a
restriction, do not automate it - add people by hand and use `connecto preview` to prepare, or send requests yourself.

Connecto does **not** try to look like a human-driven browser. There are no stealth plug-ins, no fingerprint changes and no
attempts to hide that a program is clicking: those arms races hurt the people who lose them. It simply goes slowly and
stops early.

## What a run does, step by step

1. Checks that settings are complete, the risk note has been accepted, no pause is active, and it is inside your active
   hours and days.
2. Works out the quota: today's target (a random number in your daily range, the same all day), minus what was already
   sent today and in the last seven days.
3. Opens Chrome with Connecto's own profile (your everyday Chrome is untouched) and checks you are still signed in.
4. For each person in the queue: opens the profile, scrolls like a person reading it, looks for *Connect* (directly or
   in the *More* menu), skips people who are already connected or pending, opens the invitation dialog, adds the note
   (if any), and presses *Send*. Then it waits a random time before the next profile.
5. Writes down exactly what happened to each person in its local database, so nobody is invited twice.

## Hard limits

These ceilings are in `connecto/limits.py`. Whatever `config.yaml` or the setup page asks for is clamped to them, and
`connecto config check` tells you when a number was pulled back.

| | Default | Ceiling |
|---|---|---|
| Invitations per day | 8-12 (random each day) | 30 |
| Invitations per rolling 7 days | 60 | 100 |
| Invitations per run | 4-7 | 20 |
| Pause between profiles | 60-180 seconds | never under 30 seconds |
| Errors in a row before a run stops | 3 | 5 |
| LinkedIn search pages per run / per day | 3 per query | 10 / 20 |
| Parallel requests to the YC directory | 2 | 2 |

LinkedIn's own limit is roughly 100 invitations a week; the ceilings sit below it on purpose, because accounts that
work at the limit - or that act in lock-step - are the ones that get noticed. **There is no switch to raise them**, and
none will be added.

Active hours default to 09:00-20:30, Monday to Saturday. Scheduled runs also start a random 0-30 minutes late, so they
do not land on the same minute every day.

## When it stops

A run ends at the first of:

- the run's quota is used, or the active-hours window closes;
- LinkedIn shows its **invitation-limit** dialog - Connecto pauses for **two days**;
- LinkedIn asks for a **security check**, a CAPTCHA or shows a restriction notice - it stops at once, pauses for a
  **day**, and shows a desktop notification; you deal with it yourself, in your own browser;
- you are **signed out** (it asks you to run `connecto login`);
- **three errors in a row** (a sign LinkedIn changed its page or something is wrong);
- you press *Stop* in the setup page or Ctrl+C.

`connecto resume` lifts a pause, but if LinkedIn showed you a check or a limit, give it time first.

## What it never does

- It never asks for, types or stores your LinkedIn password. You sign in by hand in its browser window.
- It never solves or bypasses a CAPTCHA or a verification step.
- It never sends messages - only connection requests with an optional note of up to 300 characters.
- It never collects or stores e-mail addresses or phone numbers (they are dropped when a CSV has them).
- It does not run hidden by default: it opens a visible window on a desktop you are logged in to, so you can watch and
  step in (and a server without a display cannot run it). `browser.headless: true` exists, but hidden browsers are easier
  for LinkedIn to flag, so it is discouraged and `connecto doctor` warns about it.
- It never sends anything on its own that you did not set up. Real sending needs a person: `connecto run` asks for
  confirmation (or `--yes`), the setup page asks in a dialog, and a scheduled run exists only if you installed the schedule.
  `--dry-run` visits profiles and sends nothing at all.

## Staying in control

| You want to | Do this |
|---|---|
| See who and what would happen | `connecto preview`, then `connecto run --dry-run --limit 2` |
| Start tiny | `connecto run --limit 3` for the first few days |
| Stop for a while | `connecto pause --days 7` (and `connecto resume`) |
| Never contact someone | `connecto skip <profile link>` |
| Stop the daily runs | `connecto schedule uninstall` |
| Remove the saved LinkedIn session | `connecto logout` |
| Remove everything | delete the Connecto folder (`~/.connecto` by default) |
| Look at what was sent | `connecto report`, or `connecto export` for a CSV |

## Good habits

- Write notes that are honest and specific. Say who you are and why you are reaching out. A short, sincere note is
  accepted far more often than a clever one - and a pile of ignored requests is itself a signal.
- Reach out to people who plausibly want to hear from you. Narrow filters and a small queue beat a big list.
- Keep to the defaults. If you see "invitation limit" dialogs, lower your daily range instead of waiting them out.
- Do not run several automation tools on the same account.
- If LinkedIn restricts your account, stop immediately (`connecto pause`), follow LinkedIn's own instructions, and be
  honest in any appeal. Do not try to evade a restriction.

## The people on your list

Names, titles, companies and profile links of people are personal data. Connecto keeps them only on your computer, in
`connecto.sqlite`, and uses them only to write and send your own connection requests. Contact people you have a genuine
reason to contact, do not share or sell the list, keep to the rules that apply where you and they live (for example the
GDPR in Europe), and delete the folder when you no longer need it.

## Reporting a problem

If something behaves in a way that could put an account at risk - a limit that was exceeded, a run that did not stop -
please follow [SECURITY.md](../SECURITY.md) rather than opening a public issue with details.
