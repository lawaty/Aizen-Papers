# Delivery status & webhook

**Breadcrumb:** [Home](../index.md) / [Guide](.) / [Delivery status](delivery-status.md)

---

## Why `200` is not delivery

The send API returns `200` + a `wamid` as soon as Meta **accepts** the message —
it does not mean the customer received or read it. Actual delivery is reported
asynchronously through the **webhook**: Meta POSTs status events
(`sent`, `delivered`, `read`, `failed`) to a callback URL you register.

## The receiver

`sender/infrastructure/whatsapp/webhook_server.py` is a dependency-free
`http.server` receiver that:

- answers the Meta verification handshake (`hub.mode`, `hub.verify_token`,
  `hub.challenge`) on `GET`,
- appends every status event as one JSON line to an events file on `POST`.

```bash
# 1. Start the receiver, expose it with a tunnel, e.g.:
python -m sender webhook-serve --port 8080 --events-file webhook_events.jsonl
#    ...in another terminal:  ngrok http 8080   (or cloudflared tunnel --url http://localhost:8080)

# 2. In the Meta App Dashboard, paste the https tunnel URL + WHATSAPP_VERIFY_TOKEN
#    and click Verify & Save (the receiver echoes hub.challenge).

# 3. Enable the subscription once (needs WHATSAPP_WABA_ID set):
python -m sender webhook-subscribe
```

Status events are appended to the events file as JSONL, e.g.:

```json
{"object":"whatsapp_business_account","entry":[{"id":"1094775866378896","changes":[{"value":{"messaging_product":"whatsapp","metadata":{"display_phone_number":"+20 12 80805534","phone_number_id":"1304588702742851"},"statuses":[{"id":"wamid.X","status":"failed","errors":[{"code":131042}]}]},"field":"messages"}]}]}
```

## Error codes to know

| Code | Meaning |
|---|---|
| `131026` | Message undeliverable (e.g. the recipient's WhatsApp is not reachable) |
| `131042` | Message failed to send |
| `131047` | Re-engagement message (24h window closed) |
| `132000`-series | Template parameter mismatch — the payload did not match the approved template |

The `132000`-series is the one the CLI actively reacts to: a `send` rejected
with such a code force-refreshes the template state and retries once with the
other builder (see [Template contract](template-contract.md)). The poller reacts
too: it falls back to a free-form message, which is why `131047` matters there —
Meta only delivers free-form messages inside the 24-hour customer service window.

## Related

- [Getting started](getting-started.md)
- [Template contract](template-contract.md)
- [Architecture](../architecture.md)