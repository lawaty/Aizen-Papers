# Layer — presentation

**Breadcrumb:** [Home](../index.md) / [Architecture](../architecture.md) / [Layers](.) / [Presentation](presentation.md)

---

## Responsibility

The human/tool interface and the **composition root** — the only place in the
codebase allowed to import all layers and wire them together. It is deliberately
dumb: parse args, build the object graph, call the use case, print output.

## Contents (`sender/presentation/`)

### `cli.py` — `main(argv)`

`argparse`-based entry point exposing the read/write invoice commands plus
webhook and template-status management commands:

| Command | Flags | Net effect |
|---|---|---|
| `send` | `--invoice-id` (req), `--to`, `--dry-run`, `--freeform`, `--builder` | build + send; prints the wamid on success; `--freeform` sends plain text instead of template; `--builder auto|legacy|new` pins the template builder (auto = resolved via template status) |
| `preview` | `--invoice-id` (req), `--to`, `--freeform`, `--builder` | print payload JSON, **never sends**; `--freeform` previews the plain-text payload |
| `show` | `--invoice-id` (req), `--raw` | print normalized invoice (or raw Daftra JSON) |
| `list` | `--limit` (default 10) | print recent invoices as a table |
| `poll` | `--once`, `--interval`, `--limit`, `--max-cycles`, `--timeout`, `--dry-run`, `--send-existing`, `--stub`, `--builder` | watch Daftra for new invoices and send them automatically |
| `poll-status` | `--stub` | read-only per-app poll state: seen count, pending (retryable) and abandoned (permanent) invoices with their errors, `last_poll_at` |
| `poll-reset` | `--app` (req), `--invoice-id`, `--yes` (req), `--stub` | reset an app's poll state (or one invoice's records); requires `--yes` and is scoped per app |
| `stub-add` | — | append a new invoice to the offline stub fixture (`stub_invoices.json`) |
| `webhook-serve` | `--host`, `--port`, `--events-file` | run the delivery-status webhook receiver |
| `webhook-subscribe` | — | register the app for WhatsApp webhook events |
| `template-status` | `--force` | print the cached/current template state and the active builder |
| `template-watch` | `--interval`, `--timeout` | poll until the reviewed template is approved (exit 0) or times out (exit 1) |
| `template-drop-legacy` | `--force` | confirm the legacy builder is deprecated and print the removal checklist |

Flow inside `main`:

1. `load_dotenv()` — read `.env`.
2. Parse args; decide if WhatsApp credentials are needed
   (`send`/`preview`/`poll` → yes).
3. `Settings.from_env(require_whatsapp=…)` — `poll` also passes
   `require_apps=True` (unless `--stub`) so at least one Daftra app must be
   configured.
4. `build_service(...)` — construct `DaftraClient` (from `Settings.primary_app`),
   optional `WhatsAppClient`, the invoice-**PDF attachment provider**
   (`_attachment_provider` → `UploadedMediaProvider` or `HostedLinkProvider`, per
   `--attachment` / `INVOICE_ATTACHMENT`), the `InvoiceTemplateBuilder`, then
   hand them to `InvoiceNotificationService`. The provider is injected into the
   legacy builder, so `send`, `preview`, and `poll` all attach the same way from
   one place.
5. Dispatch to the matching use case; pretty-print results.
6. Catch the handled error family (`ApiError`/`ValueError`/`RuntimeError`), print
   a single `ERROR: …` line to stderr, return exit code `1`.

The `poll` command builds one `PollApp` per configured `DaftraApp` (or a single
stub app for `--stub`), wires `JsonPollStateStore` (or the stub state file for
`--stub`) and `SystemClock` into `InvoicePoller`, takes an advisory
`PollStateLock` on the real state file (so two pollers cannot double-send),
installs SIGINT/SIGTERM handlers that raise `KeyboardInterrupt` (so the current
send finishes/aborts cleanly and the process exits `0`; the previous handlers
are restored on exit), and prints a concise per-app, per-invoice summary to
stdout for `--once` (cycle progress goes through `logging` to stderr). The exit
code is `1` only when the last cycle had *every* app fail.

`show`/`list` never build a message, so they skip template-builder resolution
entirely (no Meta Graph API round-trip). Stub mode pins a concrete builder
(`legacy` by default, `--builder new` to switch) so it stays fully offline, and
an unspecified attachment mode becomes `link` so a `--stub` run never uploads a
PDF; an explicit `--attachment upload` is honored and warned about.

`preview` and `--dry-run` still resolve the header document, so in the default
`upload` mode they render and upload a PDF — the payload has to reference a real
`media_id` to be worth inspecting. Neither ever sends a message.

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
- [Up: Documents](../index.md)
- [Previous layer: infrastructure](infrastructure.md)
- [Start over: overview](../index.md)