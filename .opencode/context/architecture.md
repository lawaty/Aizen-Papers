# Architecture

**This repo has its own architecture documentation. Read it before anything else.**

| Need | Go to |
|---|---|
| Layer model, runtime sequence diagrams | [`docs/architecture.md`](../../docs/architecture.md) |
| Why the code is shaped this way | [`docs/design.md`](../../docs/design.md) (the decision log) |
| Per-layer walkthroughs | [`docs/layers/`](../../docs/layers/) — domain, application, infrastructure, presentation |
| Problem/solution overview | [`docs/index.md`](../../docs/index.md) |

This file only records the navigation-level facts. Do not restate the docs here.

## What it is

A single Python package, `sender/`, run as a CLI (`python -m sender …`). It polls
Daftra ERP (invoice source) for new invoices, renders each to a PDF, and sends it
to the customer via the Meta WhatsApp Cloud API. Two external systems, ~6.6k lines
of Python, standard library plus two runtime deps (`requests`,
`python-dotenv`).

## Layers and the dependency rule

```
presentation ──▶ application ──▶ domain
      │                             ▲
      └────────▶ infrastructure ────┘
```

- **Inward dependencies only.** `application` must never import `infrastructure`.
  Dependencies between application and the outside world go through the protocols
  in `sender/domain/ports.py`.
- **`presentation` is the only composition root** — the single place allowed to
  import every layer and wire concrete adapters together.
- `domain` is stdlib-only: models, the template contract, phone normalization,
  and the ports.

The rule is stated as a convention; there is **no automated import-lint test**
enforcing it. > Confidence: high for the absence (searched the suite), and it is
the first thing to check manually when reviewing a new cross-layer import.

## Entry points

| Entry | Role |
|---|---|
| `sender/__main__.py` | 3-line shim: `from sender.presentation.cli import main` |
| `sender/presentation/cli.py` | `main(argv)`; the composition root; the only large dispatcher (757 lines) |
| `sender/presentation/stubs.py` | offline `StubInvoiceSource` + `default_stub_invoices()` for `--stub` |

Read `cli.py:build_service()` first for any wiring question — it constructs the
`WhatsAppClient`, `DaftraClient`/`StubInvoiceSource`, attachment provider, and
template builder, then hands them to `InvoiceNotificationService`.

## Top-level layout

| Path | What lives there |
|---|---|
| `sender/` | the package (the deliverable) |
| `tests/` | pytest suite; `fakes.py` + `conftest.py` hold the shared doubles/fixtures |
| `docs/` | 11 pages, breadcrumb + drill-down linked; **authoritative for design** |
| `tools/` | ops scripts the host runs *around* the app, never imported by it: `build_vendor.py` (regenerates `vendor/` for hosts without `pip`) and `run_poll.sh` (the cron poll wrapper) |
| `vendor/` | gitignored, generated copy of the runtime deps (see `workflows.md`) |
| `HANDOFF-*.md` | gitignored machine-local operator notes (e.g. `HANDOFF-deploy.md`) |

Untracked-but-present files that hold **machine-specific** state and must never be
copied to another machine: `poll_state.json`, `template_state.json`, `.env`,
`vendor/`, `logs/`. See `conventions.md`.

## Deployment reality

The app is deployed to a shared host (cPanel/FileZilla, no `pip`, no shell) and
driven by a user crontab entry every 5 minutes that calls the committed wrapper
`tools/run_poll.sh`. The deployment path is *not* a CI/CD pipeline and *not*
documented in `docs/`; host access, the interpreter, and the workflow are
captured in [`decisions.md`](decisions.md), [`workflows.md`](workflows.md) § 2, and
the gitignored `HANDOFF-deploy.md`.
