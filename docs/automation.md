# Running every day by itself

```bash
connecto schedule install                       # the times from your settings (default 10:40 and 18:20)
connecto schedule install --times "09:15, 17:30"   # other times (remembered in config.yaml)
connecto schedule status
connecto schedule uninstall
connecto schedule install --dry-run             # show what would be written, change nothing
```

The setup page's *Limits & schedule* tab does the same with buttons. Before it installs anything, Connecto checks that
a send run could actually start (your first name is set, the risk note is accepted).

## What gets installed

One schedule per user account, always running the same command:

```
<python> -m connecto --home <your connecto folder> run --scheduled
```

| System | Mechanism | Where to look |
|---|---|---|
| macOS | A launchd *LaunchAgent*, `~/Library/LaunchAgents/com.connecto.daily.plist`, with one `StartCalendarInterval` per time. | `logs/launchd.out.log`, `logs/launchd.err.log` in the Connecto folder |
| Linux | A marked block in your user crontab (`# >>> connecto >>>` ... `# <<< connecto <<<`), one line per time; the rest of your crontab is never touched. | `logs/cron.log` |
| Windows | One Task Scheduler task per time, named `Connecto-HHMM`, set to run only while you are signed in. | Task Scheduler, and `logs\run.log` |

Installing again replaces the previous schedule, whatever its times were; `uninstall` removes it and succeeds even if
there was nothing to remove. All of the actual work is in `logs/run.log`.

## What a scheduled run does

1. Waits a **random 0-30 minutes** (`schedule.start_jitter_minutes`), so it does not start on the same minute every day.
2. Checks it is still inside your active hours and days (default 09:00-20:30, Monday-Saturday) and that no pause is on.
3. Tops up the pool of people from your sources (`schedule.fetch_before_send`, on by default - a few dozen companies or pages
   at most per run).
4. Sends up to this run's quota (4-7 by default, never more than what is left of today's and this week's allowance), with
   random pauses between profiles.
5. Shows a desktop notification if it needs you - you were signed out, LinkedIn showed a check or a limit, or several
   profiles in a row failed - or a short summary when it sent something.

With two times a day and 4-7 per run you land at roughly 8-14 a day, and the daily and weekly limits cap the total
whatever the schedule says.

## It needs a desktop

By default Connecto opens a **visible** browser window, so every scheduler above starts it inside *your* logged-in desktop session. That means:

- the computer must be **on and logged in** at those times - it does not run while you are logged out, and a laptop on
  battery or asleep may skip a run (macOS runs it when the Mac wakes up, as long as it is still inside your active hours;
  on Linux and Windows a missed run is simply skipped, and the next one carries on);
- a Chrome window opens for a few minutes. Leave it alone, or let it run in the background;
- it cannot run on a server, in a container or over plain SSH (there is no display). If you try, it says
  "No desktop session found" and does nothing.

On Linux the cron line sets `DISPLAY=:0` unless it is already set. On some desktops (Wayland, with a different
`XAUTHORITY`) cron may still not be able to open a window; if the log says it cannot connect to the display, run
`connecto run --scheduled --limit 1` once from a terminal inside your desktop to see the real error, and
open an issue with the details.

## Signing in

The scheduled run reuses the LinkedIn session you created with `connecto login`. Sessions do expire from time to time;
when that happens the run stops, shows "Connecto: sign in needed", and tries again at the next scheduled time. Run
`connecto login` once and carry on.

## Pausing and holidays

```bash
connecto pause --days 10 --reason "holiday"
connecto resume
```

A pause stops every run, scheduled or not, until it ends (`connecto run --ignore-pause` overrides it for one manual run). Weekends (and any days you remove from `schedule.active_days`)
are skipped automatically. Connecto also pauses itself after LinkedIn shows a limit (two days) or a security check (one day).

## Changing the times

Edit `schedule.times` in `config.yaml` (or the setup page) and run `connecto schedule install` again. Times are 24-hour
`HH:MM` in your computer's local time zone.
