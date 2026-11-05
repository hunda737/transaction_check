# Receipt Verification API: Guide for Integrating Applications

This guide is for developers connecting their application to the Receipt
Verification Service, so that customer bank deposits are checked automatically.

The service takes a **bank** and a **transaction reference**. It looks up the receipt at
the bank and tells your application what the receipt says. **Your application
decides** whether to credit the deposit, using the [checklist](#7-before-you-credit-a-deposit) at the end of this guide.

---

## 1. How it works

You never wait for the bank inside your own request. The service queues each check,
talks to the bank at a pace the bank allows, and sends you the result:

```
 Your app                         Receipt Service                    Bank
    │  POST /verifications            │                                │
    │ ───────────────────────────────▶│                                │
    │  202 {id, status: "queued"}     │                                │
    │ ◀───────────────────────────────│                                │
    │                                 │  lookup (rate-limited,         │
    │  (show customer "verifying…")   │  retried if the bank is slow)  │
    │                                 │ ──────────────────────────────▶│
    │                                 │ ◀──────────────────────────────│
    │  POST <your webhook URL>        │                                │
    │  {status: completed | ...}      │                                │
    │ ◀───────────────────────────────│                                │
    │  200 OK                         │                                │
    │ ───────────────────────────────▶│                                │
```

In short:

1. **Submit** each deposit once with `POST /verifications`. The reply comes back in milliseconds.
2. **Receive** the result on your webhook, usually within seconds. During rush hour it can take several minutes.
3. **If no webhook arrives**, check with `GET /verifications/{id}`.

---

## 2. Connection details

| | |
|---|---|
| Base URL | `https://receipts.example.com` (the operator gives you the real one) |
| Auth header | `X-API-Key: <your key>` on every request except `/health` and `/banks` |
| Body format | JSON (`Content-Type: application/json`) |
| Webhook URL | You give it to the operator, and they register it on the server |
| Webhook secret | The operator gives it to you. Keep it as safe as a password |

Every response includes a boolean `success` field. Errors always look like this:

```json
{ "success": false, "detail": "Invalid or missing X-API-Key" }
```

---

## 3. Submit a deposit: `POST /verifications`

### Request

```bash
curl -X POST https://receipts.example.com/verifications \
  -H "X-API-Key: your-key-here" \
  -H "Content-Type: application/json" \
  -d '{"bank": "tele", "reference": "DK16KQZWCG", "client_ref": "deposit-81723"}'
```

| Field | Required | Description |
|---|---|---|
| `bank` | yes | `tele`, `cbe`, `awash`, `zemen`, `dashen` or `boa`. `GET /banks` lists each bank's reference format |
| `reference` | yes | The customer's transaction reference (max 512 characters) |
| `client_ref` | strongly recommended | **Your own deposit ID** (max 128 characters). It makes retrying this request safe |

Send the **reference, not a receipt URL**. If a customer pastes a link, take the
reference out of it first.

### Responses

**`202 Accepted`: queued.** The result will arrive by webhook.

```json
{
  "success": true,
  "id": "4f0c9d2a8b7e4c1f9a3d5e6b7c8d9e0f",
  "bank": "tele",
  "reference": "DK16KQZWCG",
  "client_ref": "deposit-81723",
  "status": "queued",
  "reason": null,
  "attempts": 0,
  "receipt": null,
  "queue_position": 3,
  "estimated_wait_seconds": 123,
  "created_at": "2026-11-04T13:40:02+00:00",
  "updated_at": "2026-11-04T13:40:02+00:00"
}
```

Save the `id` with your deposit. `estimated_wait_seconds` is a rough estimate that
you can show to the customer.

**`200 OK`: already known.** The body has the same shape, but `status` is already
final. This happens in two cases:

- The service already had this receipt, so `status` is `"completed"` and `receipt` is filled in.
- You re-sent a `client_ref` you had used before, so you get back that original verification.

> **No webhook follows a `200`.** Process the body as if it were the webhook.

**Errors:**

| Code | Meaning | What to do |
|---|---|---|
| `400` | The reference is malformed or the bank is unknown | Ask the customer to re-enter it. Don't retry |
| `401` | The API key is missing or wrong | Fix your configuration. Never show this to the customer |
| `422` | The request body is invalid (for example, a field is missing) | Fix your request |
| `503` | The service isn't configured | Alert the operator, then retry later |
| network error / `5xx` | The service couldn't be reached | Retry with the **same `client_ref`**. This is safe |

### Duplicate protection

- **Same `client_ref` again:** you get the original verification back. This holds even if the body differs, and even after the verification has finished.
- **Same bank and reference while the first check is unfinished:** you get the same verification back, so the bank isn't asked twice.

Because of this, you can retry the POST after any network failure or timeout.
Use a client timeout of **about 10 seconds**.

---

## 4. Receive the result: webhook

When a check finishes, the service sends a POST to your webhook URL:

```http
POST /your/webhook/path HTTP/1.1
Content-Type: application/json
User-Agent: ethiobank-receipts-webhook/1
X-Ethiobank-Timestamp: 1793800202
X-Ethiobank-Signature: sha256=5d41402abc4b2a76b9719d911017c592...

{"success":true,"event":"verification.finished","verification":{"id":"4f0c9d2a...","bank":"tele","reference":"DK16KQZWCG","client_ref":"deposit-81723","status":"completed","reason":null,"attempts":1,"receipt":{...},"created_at":"...","updated_at":"..."}}
```

`verification` has the same fields as the POST response. It doesn't include
`queue_position` or `estimated_wait_seconds`.

### Verify the signature (required)

The signature is an HMAC-SHA256, made with your webhook secret, of
`"<timestamp>.<raw request body>"`. Compute it over the **raw bytes** you
received, before parsing the JSON.

**Python (Flask):**

```python
import hashlib, hmac, time
from flask import Flask, request, abort

app = Flask(__name__)
SECRET = b"your-webhook-secret"

@app.post("/hooks/receipts")
def receipt_webhook():
    ts = request.headers.get("X-Ethiobank-Timestamp", "")
    sig = request.headers.get("X-Ethiobank-Signature", "")
    body = request.get_data()                      # raw bytes, before JSON parsing

    if not ts.isdigit() or abs(time.time() - int(ts)) > 300:
        abort(401)                                 # missing or stale: possible replay
    expected = "sha256=" + hmac.new(SECRET, ts.encode() + b"." + body,
                                    hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        abort(401)

    v = request.get_json()["verification"]
    enqueue_for_processing(v)                      # your code: store it, process later
    return "", 200                                 # answer fast
```

**Node.js (Express):**

```javascript
const crypto = require("crypto");
const express = require("express");
const app = express();
const SECRET = "your-webhook-secret";

app.post("/hooks/receipts", express.raw({ type: "application/json" }), (req, res) => {
  const ts = req.get("X-Ethiobank-Timestamp") || "";
  const sig = req.get("X-Ethiobank-Signature") || "";
  if (!/^\d+$/.test(ts) || Math.abs(Date.now() / 1000 - Number(ts)) > 300) {
    return res.sendStatus(401);
  }
  const expected = "sha256=" + crypto.createHmac("sha256", SECRET)
    .update(ts + ".").update(req.body).digest("hex");
  if (sig.length !== expected.length ||
      !crypto.timingSafeEqual(Buffer.from(sig), Buffer.from(expected))) {
    return res.sendStatus(401);
  }
  const v = JSON.parse(req.body).verification;
  enqueueForProcessing(v);                         // your code
  res.sendStatus(200);
});
```

### Webhook rules

- **Reply with a `2xx` within a few seconds.** Store the payload and process it
  afterwards. The service waits at most 10 seconds for your reply.
- **Failed deliveries are retried.** Any non-`2xx` reply or a timeout counts as a
  failure. The service retries after **10s, 1 min, 5 min and 30 min**, then stops.
  After that the result is only available through `GET /verifications/{id}`.
- **Expect duplicates.** The same result can arrive more than once. Key your
  processing on `verification.id` (or your `client_ref`) so that a repeat does
  nothing.
- Your webhook endpoint should use **HTTPS**.

---

## 5. Polling fallback: `GET /verifications/{id}`

```bash
curl https://receipts.example.com/verifications/4f0c9d2a8b7e4c1f9a3d5e6b7c8d9e0f \
  -H "X-API-Key: your-key-here"
```

This returns the same body as the POST. You only see verifications created with
your own API key. Any other ID returns `404`.

Use it only for deposits that are still waiting after `estimated_wait_seconds`
plus a few minutes, or after your webhook endpoint was down.
**Poll no more often than every 15 seconds**, and stop once the status is final.

---

## 6. Statuses and what to do

| `status` | Final? | Meaning | Your action |
|---|---|---|---|
| `queued` | no | Waiting for its turn at the bank | Show the customer "verifying…" |
| `processing` | no | The bank is being asked right now | Show the customer "verifying…" |
| `completed` | **yes** | The receipt was fetched. `receipt` holds the data | Run the [checklist](#7-before-you-credit-a-deposit), then accept or reject |
| `not_found` | **yes** | The bank says this receipt doesn't exist. It was checked twice, 60s apart | **Reject automatically.** No human needed |
| `needs_review` | **yes** | The service could not check this deposit. `reason` explains why | Send to a human reviewer |

`needs_review` happens only in these cases:

- The bank kept failing (timeouts, outages) for **10 minutes**, counted from the
  first real attempt. Time spent waiting in the queue doesn't count, so a busy
  rush hour doesn't send deposits to humans.
- The bank changed its receipt page and it could no longer be read. The
  operator is alerted.

A slow or unavailable bank is **never** a reason to reject a deposit. Only
`not_found` (or a failed check of your own) is.

### The `receipt` object (when `status` is `completed`)

```json
{
  "bank": "tele",
  "reference": "DK16KQZWCG",
  "status": "COMPLETED",
  "amount": "2500.00",
  "currency": "ETB",
  "paid_at": "2026-11-04T10:12:55",
  "payer_name": "ABEBE KEBEDE",
  "payer_account": "2519****1234",
  "receiver_name": "YOUR COMPANY PLC",
  "receiver_account": "2519****5442",
  "fees": { "service_fee": "5.22", "vat": "0.78", "payer_total_debit": "2506.00" },
  "raw": { "...": "the bank's original fields, for audit" }
}
```

| Field | Notes |
|---|---|
| `amount` | **What the receiving account actually got, with fees excluded. Credit this.** It's a string, so use a decimal type and never a float |
| `fees.payer_total_debit` | What the payer was charged including fees. **Do not credit this** |
| `reference` | The bank's own transaction ID. **It may differ from the one you sent**, for example for CBE, Awash and Zemen. Store both |
| `status` | Upper-cased bank status. It's `null` for banks that don't report one (Awash, Dashen, BoA) |
| `paid_at` | ISO 8601. It's `null` if the bank's date couldn't be read |
| `receiver_account` | Usually masked (`2519****5442`). Awash shows the full account |
| other fields | Any field the bank doesn't provide is `null` |

---

## 7. Before you credit a deposit

The service tells you what the receipt says. It can't tell you whether the
payment is **yours** or **new**. Check all of these:

1. **Amount**: `receipt.amount` equals the amount the customer claims, compared as decimals.
2. **Receiver**: `receiver_account` (the visible digits) and `receiver_name` match your receiving account.
3. **Status**: not failed and not pending. Remember that it's `null` for some banks.
4. **Freshness**: `paid_at` is recent enough for your policy.
5. **Not used before**: the returned `receipt.reference` has never been credited.
   Enforce this with a **unique database constraint on `(bank, receipt.reference)`**.
   An application-level check isn't enough, because two simultaneous submissions
   would both pass it.

> Point 5 matters most. A genuine receipt stays valid forever. Without the unique
> constraint, one real payment can be submitted again and again for repeated credit.

If any check fails, reject the deposit or send it for review, according to your own policy.

---

## 8. Timing and limits

- **telebirr is slow on purpose.** It allows only a few lookups per minute from
  the service, so during a telebirr rush expect about **1.5–2 checks per
  minute**. For example, the 30th telebirr deposit in a burst can take
  **15–20 minutes**. Other banks are much faster.
- **A `not_found` takes at least about 60 seconds**, because the bank is asked twice
  to allow for payments that haven't shown up yet.
- **Don't re-check a reference yourself.** telebirr appears to block repeated
  lookups of the same reference. Submit once and wait.
- **Avoid `GET /receipts/{bank}/{reference}`** in automated flows. It calls the bank
  directly, can take more than 30 seconds, and uses up the same bank quota as the
  queue. It's meant for occasional manual checks by staff.

---

## 9. Integration checklist

- [ ] The API key is stored as a secret on your server and never sent to browsers or mobile apps
- [ ] Every deposit is submitted once with `client_ref` set to your deposit ID
- [ ] The POST is retried with the same `client_ref` on network errors or `5xx`, with a client timeout of about 10s
- [ ] A `200` reply to the POST is processed right away, because no webhook follows it
- [ ] The webhook endpoint checks the timestamp and signature and replies `2xx` quickly
- [ ] Webhook processing ignores duplicates (keyed on `verification.id` / `client_ref`)
- [ ] There's a polling fallback for deposits with no webhook, at most every 15s
- [ ] `completed` results go through the checklist in section 7
- [ ] `not_found` results are rejected automatically, and `needs_review` results go to a human
- [ ] There's a unique database constraint on `(bank, receipt.reference)`
- [ ] Amounts are handled as decimals, never floats
- [ ] Customers see a "verifying…" state with the estimated wait

---

## 10. Quick reference

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/verifications` | yes | Submit a deposit for checking |
| `GET` | `/verifications/{id}` | yes | Current state of a submitted check |
| `GET` | `/banks` | no | Supported banks and reference formats |
| `GET` | `/health` | no | Liveness check, for your monitoring |
| `GET` | `/receipts/{bank}/{reference}` | yes | Synchronous lookup, for manual use only |
| `GET` | `/docs` | no | Interactive API documentation |
