"""A tiny stand-in for LinkedIn so the connect flow can be exercised end-to-end without touching LinkedIn.

It models the accessibility structure of a profile page as understood when this was written (it has never been
compared with the live site): randomised class names (so nothing can be matched by class), the person's name in a
heading of a <section> inside <main>, Follow / More buttons, a role=menu with role=menuitem entries, and a role=dialog
invite flow. Scenarios are keyed by profile slug; the families ``dir-N`` (direct Connect), ``pend-N``, ``conn-N``,
``fol-N`` (Follow only) and ``inst-N`` (invitation sent at once, no dialog) give as many look-alike people as a test needs.
"""
from __future__ import annotations

import html
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

SCENARIOS = {
    "paul-witten-39579715a": {"name": "Paul Witten", "mode": "more_menu"},
    "direct-connect": {"name": "Dana Direct", "mode": "direct"},
    "pending-person": {"name": "Pat Pending", "mode": "pending"},
    "connected-person": {"name": "Connie Connected", "mode": "connected"},
    "limit-person": {"name": "Lim It", "mode": "limit"},
    "email-person": {"name": "Em Ail", "mode": "email"},
    "follow-only": {"name": "Fol Low", "mode": "follow_only"},
    "how-know-person": {"name": "Howie Know", "mode": "how_know"},
    "note-blocked": {"name": "Nora Blocked", "mode": "note_blocked"},
    "checkpoint-person": {"name": "Chek Point", "mode": "checkpoint"},
    "direct-two": {"name": "Dee Two", "mode": "direct"},
    "direct-three": {"name": "Tri Direct", "mode": "direct"},
    # --- pages that are laid out differently, or that go wrong in a particular way
    "h1-person": {"name": "Hana Heading", "mode": "direct", "heading": "h1"},          # the name is in an <h1>
    "bio-trap": {"name": "Bea Biotrap", "mode": "direct",
                 "about": "Ask me about too many invitations, the weekly invitation limit and why something went wrong."},
    "ghost-send": {"name": "Gus Ghost", "mode": "direct", "variant": "ghost"},          # Send closes the dialog, nothing happens
    "slow-close": {"name": "Sol Slow", "mode": "direct", "variant": "slow"},            # Send works, the dialog lingers
    "toast-error": {"name": "Tia Toast", "mode": "direct", "variant": "toasterror"},    # an error message, nothing sent
    "check-after-send": {"name": "Cal Check", "mode": "direct", "variant": "checkafter"},   # a security check after Send
    "no-add-note": {"name": "Nia Nonote", "mode": "direct", "variant": "noaddnote"},    # the button is called "Add note"
    "mangle-note": {"name": "Max Mangle", "mode": "direct", "variant": "mangle"},       # the note box rewrites what is typed
    "foreign-dialog": {"name": "Fay Foreign", "mode": "direct", "variant": "foreign"},  # Connect opens some other dialog
    "silent-send": {"name": "Sam Silent", "mode": "direct", "variant": "silent"},      # sent, but LinkedIn shows no sign of it
    "toast-only": {"name": "Tess Toast", "mode": "direct", "variant": "toastonly"},    # sent; only a message says so (no Pending)
    "limit-after": {"name": "Lola After", "mode": "direct", "variant": "limitafter"},  # Send answers with the limit dialog
    "limit-toast": {"name": "Lana Toast", "mode": "direct", "variant": "limittoast"},  # Send answers with a limit message
    "logout-after": {"name": "Lou Gout", "mode": "direct", "variant": "logoutafter"},  # signed out right after Send
    "other-label": {"name": "Ola Label", "mode": "direct", "label_name": "Somebody Else"},   # Connect is labelled for someone else
    "silent-widget": {"name": "Wil Widget", "mode": "direct", "variant": "silent",         # sent, no sign of it - and an unrelated
                      "widget": "Couldn't load the latest posts. Please try again later."},   # widget inside the page complains
    "decoy-first": {"name": "Dora Decoy", "mode": "direct", "decoy_first": True},        # a block with its own Connect comes first
    "howknow-foreign": {"name": "Hal Know", "mode": "how_know", "variant": "foreign2"},  # 'How do you know' leads to another dialog
    "no-section": {"name": "Nils Nosection", "mode": "direct", "container": "div"},      # the header is not wrapped in a <section>
    "no-title": {"name": "Tom Untitled", "mode": "direct", "title": ""},                # the page title says nothing
    "wrong-title": {"name": "Wes Wrong", "mode": "direct", "title": "Somebody Else"},  # the title names another person
}

FAMILIES = {"dir": ("Dana Direct", "direct"), "pend": ("Pat Pending", "pending"), "conn": ("Connie Connected", "connected"),
            "fol": ("Fol Low", "follow_only"), "inst": ("Ina Instant", "instant")}


def scenario_for(slug: str) -> dict | None:
    """The scenario for a profile slug: an explicit one, or a member of a family (``dir-7``); None = no such profile."""
    if slug in SCENARIOS:
        return SCENARIOS[slug]
    m = re.fullmatch(r"(dir|pend|conn|fol|inst)-(\d+)", slug)
    if m:
        name, mode = FAMILIES[m.group(1)]
        return {"name": f"{name} {m.group(2)}", "mode": mode}
    return None

# Addresses that LinkedIn answers with a redirect: slug -> where to. (A person who changed their address; a link that
# leads somewhere that is not a profile.) The mock records every visit in ``MockLinkedIn.visits``.
REDIRECTS = {
    "moved-person": "/in/direct-two/",
    "moved-fresh": "/in/direct-three/",
    "leads-to-feed": "/feed/",
}

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>{title_prefix}{title}{title_suffix}</title>
<style>[hidden]{{display:none}} .ckyr4{{padding:6px 12px}} main{{max-width:900px;margin:auto}}</style></head>
<body>
<nav class="ckya1 ckyb2" aria-label="Primary">Home · My Network · Jobs</nav>
<main id="workspace" class="ckymh2 ckymft">
  <!-- decoys like the real page: dialogs that exist in the DOM/a11y tree but are not on screen -->
  <div role="dialog" aria-label="Modal Window" style="width:0;height:0;overflow:hidden">This is a modal window.</div>
  <div role="dialog" aria-label="Caption Settings Dialog" hidden>Beginning of dialog window.</div>
  <section class="ckyum1"><div class="ckyum2">cover photo</div></section>
  {decoy}
  <{container} class="ckyum3">
    <div class="ckya1p"><{heading} class="ckyn1">{name}</{heading}></div>
    <div class="ckya1x">Founder at Mock Co · <span>3rd</span></div>
    <div class="ckya49" id="actions">
      <a href="/messaging/" class="ckymnz">Message</a>
      {primary}
      <button type="button" class="ckymnz ckyr4" id="more" aria-expanded="false">More</button>
    </div>
    <div role="menu" id="menu" hidden class="ckymz3">
      <a role="menuitem" href="#" class="ckym1">Send profile in a message</a>
      <div role="menuitem" class="ckym1">Save to PDF</div>
      {menu_connect}
      <a role="menuitem" href="#" class="ckym1">Report {first}</a>
      <a role="menuitem" href="#" class="ckym1">About this member</a>
    </div>
  </{container}>
  <section class="ckyum4"><h2>Activity</h2><p>posts…</p><p>{about}</p>{widget}</section>
  <section class="ckyum5"><h3>More profiles for you</h3>
    <div><h2>Other Person</h2><button type="button" aria-label="Invite Other Person to connect">Connect</button></div>
  </section>
</main>
<div id="toast" role="status" aria-live="polite" hidden></div>
<script>
const MODE = {mode_json};
const VARIANT = {variant_json};
const NAME = {name_json};
const more = document.getElementById('more');
const menu = document.getElementById('menu');
more.addEventListener('click', () => {{ const open = menu.hidden; menu.hidden = !open; more.setAttribute('aria-expanded', String(open)); }});
document.addEventListener('keydown', e => {{ if (e.key === 'Escape') {{ menu.hidden = true; more.setAttribute('aria-expanded','false'); const d = document.getElementById('dlg'); if (d) d.remove(); }} }});

function dialog(inner) {{
  const d = document.createElement('div'); d.id = 'dlg'; d.setAttribute('role','dialog'); d.setAttribute('aria-labelledby','dlg-h');
  d.innerHTML = '<div class="ckyd1"><h2 id="dlg-h">' + inner.title + '</h2>' + inner.body + '<button type="button" aria-label="Dismiss">×</button></div>';
  document.body.appendChild(d);
  d.querySelector('[aria-label="Dismiss"]').addEventListener('click', () => d.remove());
  return d;
}}
function markPending() {{
  const act = document.getElementById('actions');
  act.querySelectorAll('[data-connect]').forEach(b => b.remove());
  const p = document.createElement('button'); p.type='button'; p.textContent = 'Pending'; p.className='ckymnz ckyr4'; act.insertBefore(p, more);
  const mc = menu.querySelector('[data-connect]'); if (mc) mc.remove();
}}
function report(note) {{
  return fetch('/__sent', {{method:'POST', headers:{{'content-type':'application/json'}}, body: JSON.stringify({{name: NAME, note: note}})}});
}}
function inviteDialog() {{
  const d = dialog({{title: 'Add a note to your invitation?', body:
    '<p>LinkedIn members are more likely to accept invitations that include a personal note.</p>' +
    '<div id="notebox" hidden><label for="custom-message">Add a note</label><textarea id="custom-message" name="message" maxlength="300" placeholder="Ex: We know each other from…"></textarea><div><span id="count">0</span>/300</div></div>' +
    '<button type="button" id="addnote">' + (VARIANT === 'noaddnote' ? 'Add note' : 'Add a note') + '</button>' +
    '<button type="button" id="sendplain">Send without a note</button>' +
    '<button type="button" id="send" hidden disabled>Send</button>'}});
  const box = d.querySelector('#custom-message');
  d.querySelector('#addnote').addEventListener('click', () => {{
    if (MODE === 'note_blocked') {{
      // free-plan quota: no text box, an upsell instead, plain send still possible
      d.querySelector('#addnote').hidden = true;
      const p = document.createElement('p');
      p.textContent = "You've reached the monthly limit for personalized invitations. Upgrade to Premium to add a note.";
      d.querySelector('.ckyd1').insertBefore(p, d.querySelector('#sendplain'));
      return;
    }}
    d.querySelector('#notebox').hidden = false; d.querySelector('#addnote').hidden = true; d.querySelector('#sendplain').hidden = true; d.querySelector('#send').hidden = false;
  }});
  box.addEventListener('input', () => {{
    if (VARIANT === 'mangle') box.value = box.value.toUpperCase();
    d.querySelector('#count').textContent = box.value.length; d.querySelector('#send').disabled = box.value.trim().length === 0;
  }});
  async function finish(note) {{
    if (VARIANT === 'ghost') {{ d.remove(); return; }}                       // closes, but nothing was sent
    if (VARIANT === 'toasterror') {{ d.remove(); toast('Something went wrong. Please try again later.'); return; }}
    if (VARIANT === 'limitafter') {{ d.remove(); dialog({{title: "You've reached the weekly invitation limit", body: '<p>We limit the number of invitations you can send each week.</p><button type="button">Got it</button>'}}); return; }}
    if (VARIANT === 'limittoast') {{ d.remove(); toast("You've reached the weekly invitation limit"); return; }}
    await report(note);
    if (VARIANT === 'checkafter') {{ location.href = '/checkpoint/challenge/zzz'; return; }}
    if (VARIANT === 'logoutafter') {{ location.href = '/login'; return; }}
    if (VARIANT === 'silent') {{ d.remove(); return; }}                           // sent, but nothing on the page says so
    if (VARIANT === 'toastonly') {{ d.remove(); toast('Your invitation to ' + NAME + ' was sent'); return; }}
    markPending(); toast('Invitation sent');
    if (VARIANT === 'slow') setTimeout(() => d.remove(), 1500); else d.remove();
  }}
  d.querySelector('#sendplain').addEventListener('click', () => finish(''));
  d.querySelector('#send').addEventListener('click', () => finish(box.value));
}}
function toast(msg) {{ const t = document.getElementById('toast'); t.textContent = msg; t.hidden = false; setTimeout(() => t.hidden = true, 3000); }}
function shareDialog() {{
  const d = dialog({{title: 'Share this profile', body: '<p>Share this profile with a colleague by message.</p><button type="button" id="share">Send</button>'}});
  d.querySelector('#share').addEventListener('click', async () => {{ await report('SHARED'); d.remove(); }});
}}
function onConnect(e) {{
  e.preventDefault(); menu.hidden = true; more.setAttribute('aria-expanded','false');
  if (MODE === 'instant') {{ report('').then(() => {{ markPending(); toast('Invitation sent to ' + NAME); }}); return; }}
  if (VARIANT === 'foreign') {{ shareDialog(); return; }}
  if (MODE === 'limit') return dialog({{title: "You've reached the weekly invitation limit", body: '<p>To keep LinkedIn a trusted place, we limit the number of invitations you can send each week.</p><button type="button">Got it</button>'}});
  if (MODE === 'email') return dialog({{title: 'Connect with ' + NAME, body: "<p>To verify this member knows you, please enter their email to connect.</p><input type='email' name='email'><button type='button'>Connect</button>"}});
  if (MODE === 'how_know') {{
    const d = dialog({{title: 'How do you know ' + NAME + '?', body: '<label><input type="radio" name="k">Colleague</label><label><input type="radio" name="k">We\\'ve done business together</label><label><input type="radio" name="k" id="other">Other</label><button type="button" id="hk-connect" disabled>Connect</button>'}});
    d.querySelectorAll('input[type=radio]').forEach(r => r.addEventListener('change', () => d.querySelector('#hk-connect').disabled = false));
    d.querySelector('#other').parentElement.addEventListener('click', () => {{ d.querySelector('#other').checked = true; d.querySelector('#hk-connect').disabled = false; }});
    d.querySelector('#hk-connect').addEventListener('click', () => {{ d.remove(); if (VARIANT === 'foreign2') shareDialog(); else inviteDialog(); }});
    return;
  }}
  inviteDialog();
}}
document.querySelectorAll('[data-connect]').forEach(el => el.addEventListener('click', onConnect));
document.querySelectorAll('[data-decoy]').forEach(el => el.addEventListener('click', () => report('DECOY')));
</script></body></html>"""

FEED = """<!doctype html><html><head><title>(5) Feed | LinkedIn</title></head><body>
<nav aria-label="Primary">Home</nav><main><h1>Feed</h1><p>post</p><p>post</p></main></body></html>"""

CHECKPOINT = """<!doctype html><html><head><title>Security Verification | LinkedIn</title></head><body><main>
<h1>Let's do a quick security check</h1><p>Please verify you're not a robot.</p></main></body></html>"""

LOGIN = """<!doctype html><html><head><title>LinkedIn Login</title></head><body><main><h1>Sign in</h1></main></body></html>"""

NOT_FOUND = """<!doctype html><html><head><title>Page not found | LinkedIn</title></head><body><main>
<h1>This page doesn't exist</h1><p>Please check your URL or return to LinkedIn home.</p></main></body></html>"""


def render_profile(slug: str) -> str:
    sc = scenario_for(slug)
    first = sc["name"].split()[0]
    mode = sc["mode"]
    primary = ""
    menu_connect = ""
    if mode == "direct":
        primary = f'<button type="button" class="ckymnz ckyr4" data-connect aria-label="Invite {sc.get("label_name", sc["name"])} to connect">Connect</button>'
    elif mode == "instant":
        primary = f'<button type="button" class="ckymnz ckyr4" data-connect aria-label="Invite {sc["name"]} to connect">Connect</button>'
    elif mode == "pending":
        primary = '<button type="button" class="ckymnz ckyr4">Pending</button>'
    elif mode == "connected":
        menu_connect = '<a role="menuitem" href="#" class="ckym1">Remove connection</a>'
        primary = f'<button type="button" class="ckymnz ckyr4" aria-label="Follow {sc["name"]}">Follow</button>'
    elif mode == "follow_only":
        primary = f'<button type="button" class="ckymnz ckyr4" aria-label="Follow {sc["name"]}">Follow</button>'
    else:  # more_menu, limit, email, how_know
        primary = f'<button type="button" class="ckymnz ckyr4" aria-label="Follow {sc["name"]}">Follow</button>'
        menu_connect = '<a role="menuitem" href="#" class="ckym1" data-connect>Connect</a>'
    title = sc.get("title", sc["name"])
    decoy = ('<section class="ckyum9"><h2>People also viewed</h2><button type="button" data-decoy>Connect</button></section>'
             if sc.get("decoy_first") else "")
    widget = f'<div role="status" aria-live="polite">{html.escape(sc["widget"])}</div>' if sc.get("widget") else ""
    return PAGE.format(name=sc["name"], first=first, primary=primary, menu_connect=menu_connect, decoy=decoy, widget=widget,
                       mode_json=json.dumps(mode), name_json=json.dumps(sc["name"]), variant_json=json.dumps(sc.get("variant", "")),
                       heading=sc.get("heading", "h2"), container=sc.get("container", "section"), about=html.escape(sc.get("about", "")),
                       title_prefix="(5) " if title else "", title=html.escape(title) if title else "LinkedIn",
                       title_suffix=" | LinkedIn" if title else "")


class MockLinkedIn:
    """Threaded HTTP server; `sent` collects {name, note} for every invitation the mock received."""

    def __init__(self, logged_in: bool = True):
        self.sent: list[dict] = []
        self.visits: list[str] = []          # every path asked for under /in/, exactly as the browser sent it
        self.on_sent = None                  # optional callable(dict): runs the moment an invitation arrives (before the reply)
        self.logged_in = logged_in
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8"):
                data = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path = urlparse(self.path).path
                if path.startswith("/feed"):
                    if not server.logged_in:
                        self.send_response(302)
                        self.send_header("Location", "/login")
                        self.end_headers()
                        return
                    return self._send(200, FEED)
                if path.startswith("/login"):
                    return self._send(200, LOGIN)
                if path.startswith("/checkpoint"):
                    return self._send(200, CHECKPOINT)
                if path.startswith("/in/"):
                    server.visits.append(path)
                    slug = path.split("/")[2]
                    if slug in REDIRECTS:
                        self.send_response(302)
                        self.send_header("Location", REDIRECTS[slug])
                        self.end_headers()
                        return
                    if (scenario_for(slug) or {}).get("mode") == "checkpoint":
                        self.send_response(302)
                        self.send_header("Location", "/checkpoint/challenge/abc123")
                        self.end_headers()
                        return
                    if scenario_for(slug) is not None:
                        return self._send(200, render_profile(slug))
                    return self._send(404, NOT_FOUND)
                return self._send(404, NOT_FOUND)

            def do_POST(self):
                if urlparse(self.path).path == "/__sent":
                    length = int(self.headers.get("Content-Length", "0"))
                    received = json.loads(self.rfile.read(length) or b"{}")
                    if server.on_sent is not None:
                        server.on_sent(received)
                    server.sent.append(received)
                    return self._send(200, "{}", "application/json")
                return self._send(404, "{}", "application/json")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":  # manual poke: python tests/mock_linkedin.py -> open the printed URL
    with MockLinkedIn() as m:
        print(m.base_url + "/in/paul-witten-39579715a/")
        threading.Event().wait()
