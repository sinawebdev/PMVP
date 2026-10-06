# Payrolla interface revamp — 6 October 2026

## Recovered working plan

Codex recovered the local Claude conversation and consulted the authenticated
Claude CLI with only approved planning context. The latest work was SMS Phase 3
(recipient/credit confirmation, bulk confirmation, unknown-send settlement and
cancel-confirm feedback), followed by the folded-ribbon logo and desktop
download. SMS Phase 4/webhooks and the rate-limit/routing decisions remain a
separate workstream. The delete-confirm keyboard trap is an interface defect.

The most recent written interface architecture is `UI_ARCHITECTURE.md`: the
September company portal shell/dashboard plan. It is a draft, not an already
completed site-wide redesign. Some shell work shipped; the proposed dashboard
composition did not. The July UX blueprint established principles; it is not
the current implementation checklist. The older Chaos-to-Flow hero is a
storyboard; the October decision approved the folded-ribbon P on the landing
page and desktop splash.

## Today's direction

A calm, precise payroll workspace: Satoshi typography, deep-teal ink,
warm light surfaces, generous but useful spacing, clear table hierarchy and
one prominent action per state. The signature brand moment is the existing
P assembling once in a dark framed landing hero. Work screens stay still.
Self-host fonts so the desktop wrapper can render offline.

All existing destinations and capabilities stay reachable. Keep the current
role predicates, tenant scoping, CSRF wiring, truthful money/counts, trend
chips, status vocabulary and payroll calculations. No fabricated metrics,
unimplemented CTAs or new provider behavior. Do not deploy from this branch.

## Ownership and implementation

- Codex: shared tokens/font assets and interface stylesheet; operator shell,
  marketing, authentication, errors, modal keyboard behavior and integration.
- Claude: company dashboard/portal presentation and worker-facing payslip
  presentation; provide edits for explicitly assigned files only.
- One shared CSS vocabulary supplies cards, forms, actions, tables, states,
  navigation and records across both existing Flask/Jinja shells.
- Keep Bootstrap in the operator shell; keep the company portal lightweight.
- Build on isolated `feat/site-interface-revamp`, integrating the committed
  SMS work and current main branding, so neither workstream is lost.

## Verification

Capture desktop/mobile views and interactive navigation, account-menu and
typed-confirm states against a seeded disposable database. Check focus,
Escape, no page overflow, reduced motion, local font loading and form posts.
Run constitution, tenant/role, engine-parity and affected route tests, then the
full suite. Keep before/after captures outside Git. Review the finished patch
with Claude. Publishing and merging to the auto-deploy branch is separate
from building and reviewing the interface.

## Implemented and locally verified

- Shared warm surfaces, Satoshi typography, readable fields/tables and visible
  keyboard focus across operator and company pages.
- New marketing and authentication layouts; approved one-shot P animation,
  motion fallback and conditional Windows download retained.
- Claude supplied the dashboard composition, portal drawer focus handling and
  public payslip layouts. Health/risk now lead; figures and shortcuts follow.
- Typed-confirm input participates in normal Tab/Shift+Tab navigation; Escape
  cancels and returns focus without submitting an action.
- Original fonts are fetched and hash-checked during build, then served locally.
  Binaries are excluded from the public repository. Manually configured Render
  builds must add `python scripts/fetch_fonts.py` before deployment.
- 14 landing/constitution tests and 67 client/navigation/engine/tenancy/bulk/SMS
  tests passed locally. Browser review passed 247 checks across 56 desktop and
  phone views, including real CSRF-protected login, password visibility,
  motion fallback, drawer focus, confirmation focus and page overflow.
- CI now repeats browser review and retains disposable-demo screenshots. The
  full backend suite remains a required CI gate before merging.
