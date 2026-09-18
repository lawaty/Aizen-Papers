# Layer — presentation

**Breadcrumb:** [Home](../../index.md) / [Architecture](../architecture.md) / [Layers](.) / [Presentation](presentation.md)

---

## Responsibility

The human/tool interface and the **composition root** — the only place in the
codebase allowed to import all layers and wire them together. It is deliberately
dumb: parse args, build the object graph, call the use case, print output.

## Contents (`sender/presentation/`)

### `cli.py` — `main(argv)`

`argparse`-based entry point exposing four subcommands:

| Command | Flags | Net effect |
|---|---|---|
| `send` | `--invoice-id` (req), `--to`, `--dry-run` | build + send; prints the wamid on success |
| `preview` | `--invoice-id` (req), `--to` | print payload JSON, **never sends** |
| `show` | `--invoice-id` (req), `--raw` | print normalized invoice (or raw Daftra JSON) |
| `list` | `--limit` (default 10) | print recent invoices as a table |

Flow inside `main`:

1. `load_dotenv()` — read `.env`.
2. Parse args; decide if WhatsApp credentials are needed
   (`send`/`preview` → yes).
3. `Settings.from_env(require_whatsapp=…)`.
4. `build_service(...)` — construct `DaftraClient`, optional `WhatsAppClient`,
   `InvoiceTemplateBuilder`, then hand them to `InvoiceNotificationService`.
5. Dispatch to the matching use case; pretty-print results.
6. Catch the handled error family (`ApiError`/`ValueError`/`RuntimeError`), print
   a single `ERROR: …` line to stderr, return exit code `1`.

### `__main__.py`

Two lines — imports the real entry and calls it — so `python -m sender` keeps
working and the actual logic stays in `presentation`.

## Why the CLI is the composition root

`InvoiceNotificationService` refuses to construct its collaborators itself. Only
here, at the edges of the program, do protocols become concrete classes. That
means a future Web UI shell could grow its own composition root and reuse the
same `application` + `domain` untouched.

## Back / drill down

- [Up: Architecture](../architecture.md)
- [Up: Documents](../../index.md)
- [Previous layer: infrastructure](infrastructure.md)
- [Start over: overview](../../index.md)