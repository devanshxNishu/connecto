# Connecto improvement plan

The goal: make it easy to understand who will be contacted, why they are in the queue, and what the note says, while
keeping the existing local data model, sending limits and stop conditions. This plan is based on the repository's
current implementation, reviewed on 26 October 2026.

## 1. Review the whole queue — implemented in this change

**Problem:** the setup page offers a random sample of 12 people and three example notes. It has a searchable people
API, but the page does not expose it, and stored people are not necessarily eligible for a run. Removing someone means
copying their link into a separate form. A growing queue becomes difficult to check.

- [x] Add a shared queue-review function using the existing sender eligibility rules.
- [x] Search every eligible person by name, company, role, location or profile URL.
- [x] Keep pages stable for unchanged data, with bounded responses and notes composed only for the requested page.
- [x] Show each template note, character count, or the reason for a plain invitation.
- [x] Add Never contact and Undo skip directly to the setup page's review cards.
- [x] Expose the same review through the terminal, including JSON for local tools.
- [x] Keep reviewing offline: no AI calls, browser session, schedule or invitations.

**Release checks:** regression tests cover source/target/status exclusions, pagination, note parity, Unicode search,
skip/undo, authentication, hostile text, keyboard focus, and a 360 px viewport in light and dark modes. Run the complete
offline and browser suites plus a wheel installation check before merging.

## 2. Explain queue and import results

**Problem:** a count of zero does not distinguish an empty source from a restrictive filter or a fully handled queue.

**Implementation:** centralize eligibility reasons in the planner, then show counts for disabled sources, target
mismatches, handled people and invalid profiles. Add an import summary showing added, updated, duplicate and rejected
rows with useful examples. Reuse the same decisions in previews and sending to prevent drift.

**Done when:** fixtures with mixed sources explain every excluded row; import failures identify the row and a fix;
the UI never calls filtered-out people ready to invite. No contact fields or credentials appear in diagnostics.

## 3. Make first use understandable without signing in

**Implementation:** provide an explicit demo using synthetic people and a separate temporary home, then guide a new
user through choosing sources, reviewing notes and running the existing doctor checks. Show the next actionable step
with a link to its setting. Keep the current risk acknowledgement and explicit sending confirmation.

**Done when:** a new user can explore a realistic queue offline without a LinkedIn session, AI key or scheduler;
demo data cannot enter their real queue; the complete onboarding path works with keyboard navigation on mobile widths.

## 4. Turn history into useful feedback

**Implementation:** summarize invitation outcomes and pauses by date and source using existing local records. Separate
confirmed sends, dry runs and uncertain outcomes. Show a clear next step for each stop reason and provide a redacted
diagnostic export that excludes profiles, names, keys and browser data.

**Done when:** totals reconcile with stored records and quota calculations; timezone boundaries and empty history have
tests; exported diagnostics contain only an explicit allowlist. Do not label an invitation as accepted without evidence.

## 5. Make releases easier to trust

**Implementation:** review the existing dependency-update PRs individually, exercise a clean wheel install on all three
desktop platforms, and promote macOS/Windows CI to required checks once failures are resolved. Add a repeatable manual
scheduler checklist and a changelog. Keep browser integration tests on local stand-ins.

**Done when:** a release has passing supported-platform checks, installation instructions verified from its wheel,
and documented manual scheduler results. Dependency changes keep Python 3.10 compatibility or explicitly announce a
supported-version change.

## Delivery order and limits

Ship each numbered stage as a focused PR with its own acceptance checks. Stage 2 should precede onboarding so the
guided flow can explain empty results. Stage 4 can follow independently of onboarding. Stage 5 is ongoing release work.
These are remaining milestones, not claims that they have already shipped.

Keep the standard-library-first architecture. Do not add a hosted service, telemetry, bulk sending, stealth behavior,
credential handling or higher sending ceilings. A persisted approval queue or editable per-person notes needs a separate
design: it must define what happens when a template, person, AI result or configuration changes after review.
