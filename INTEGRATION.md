# Integration Guide

For applications calling the Receipt Service to verify a customer's bank transfer.

The service takes a **bank** and a **transaction reference**, fetches that receipt
from the bank, and returns it as normalized JSON. It does not decide whether a
payment is valid — [your application does](#verification-checklist).

---

## Base URL and authentication

```
http://your-server:8060
```

Every `/receipts` and `/verifications` request needs an API key header:

```
X-API-Key: your-key-here
```

`/health` and `/banks` need no key. Keys are issued by whoever runs the service.

---

## Quick start

```bash
curl -H "X-API-Key: your-key-here" \
  http://your-server:8060/receipts/tele/DHS48P7HGQ
```

---

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/health` | no | Liveness check |
| `GET` | `/banks` | no | Supported banks and reference formats |
| `POST` | `/verifications` | **yes** | **Automated checks.** Queue a receipt check; result by webhook |
| `GET` | `/verifications/{id}` | **yes** | State of a queued check (fallback when a webhook is missed) |
| `GET` | `/receipts/{bank}/{reference}` | **yes** | Fetch one receipt synchronously (manual checks only) |
| `GET` | `/docs` | no | Interactive OpenAPI docs |

> **Automating deposits? Use `POST /verifications`, not `GET /receipts`.** See
> [Automated deposit checks](#automated-deposit-checks). The synchronous endpoint
> calls the bank inside your request, and telebirr in particular stops answering
> after ~5 lookups in a few minutes — at rush hour that turns into timeouts and
> needless manual reviews.

### `GET /health`

```json
{ "status": "ok", "api_keys_configured": true }
```

`api_keys_configured: false` means the service will reject every `/receipts` call
with `503` until it is configured. Use this in your monitoring.

### `GET /receipts/{bank}/{reference}`

`{bank}` is one of `tele`, `cbe`, `awash`, `zemen`, `dashen`, `boa`.

`{reference}` is the customer's transaction reference. **Send the reference, not a
URL.** If a customer gives you a receipt link, extract the reference from it.

---

## Automated deposit checks

The bank, not this service, is the bottleneck. telebirr advertises
`X-RateLimit-Limit: 5` and, when it throttles, it does not return an error — it
simply never answers. It has also been seen **hanging on a reference that was
looked up many times** while answering other references normally. So the service
queues every check, calls each bank at a conservative pace, never re-fetches a
receipt it already has, and pushes the result to you.

**Don't re-check a reference yourself.** Repeated lookups of the same reference
are what telebirr appears to throttle hardest. Submit once and wait for the
webhook.

```
your app ──POST /verifications──▶ service ── 202 {id, status: queued}
                                     │  (one bank call at a time, rate-limited,
                                     │   timeouts retried automatically)
your app ◀── signed webhook ─────────┘  {status: completed | not_found | needs_review}
```

### 1. Submit once per deposit

```bash
curl -X POST http://your-server:8060/verifications \
  -H "X-API-Key: your-key-here" -H "Content-Type: application/json" \
  -d '{"bank": "tele", "reference": "DHS48P7HGQ", "client_ref": "deposit-81723"}'
```

```json
{
  "success": true, "id": "4f0c…", "bank": "tele", "reference": "DHS48P7HGQ",
  "client_ref": "deposit-81723", "status": "queued", "reason": null, "attempts": 0,
  "receipt": null, "queue_position": 3, "estimated_wait_seconds": 123,
  "created_at": "2026-11-04T13:40:02+00:00", "updated_at": "2026-11-04T13:40:02+00:00"
}
```

- **`202`** = queued. **`200`** = already known (receipt cached from an earlier
  check, or a `client_ref` you already used) — the body then already has the final
  `status` and `receipt`, and **no webhook follows**.
- **`400`** = malformed reference or unknown bank. Ask the customer to re-enter.
- Set **`client_ref` to your own deposit id.** Re-sending it (after a network error,
  a double click, a retry in your queue) returns the same verification instead of
  creating a new one, so retrying the `POST` is always safe.
- Show the customer "verifying…" with `estimated_wait_seconds`. During a telebirr
  rush, expect about **1.5–2 checks per minute**: the 30th telebirr deposit in a
  burst waits roughly 15–20 minutes. Other banks are much faster.

### 2. Receive the webhook

Your callback URL is configured by whoever runs the service (it is never taken
from a request). When a check finishes, the service POSTs:

```json
{ "success": true, "event": "verification.finished",
  "verification": { "id": "4f0c…", "status": "completed", "receipt": { … }, … } }
```

with headers `X-Ethiobank-Timestamp` and `X-Ethiobank-Signature`. **Verify the
signature** before trusting the body:

```python
import hashlib, hmac, time

def verify(raw_body: bytes, timestamp: str, signature: str, secret: str) -> bool:
    if abs(time.time() - int(timestamp)) > 300:          # reject stale replays
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(), f"{timestamp}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
```

- Reply **`2xx` quickly** and process afterwards. Anything else is redelivered
  after 10s, 1m, 5m and 30m.
- Deliveries can repeat. Key your handling on `verification.id` (or your
  `client_ref`) so a duplicate does nothing.

### 3. Fall back to polling

If a webhook hasn't arrived for a deposit that's been `queued` well past its
estimate, `GET /verifications/{id}` returns the same body. Poll **no more often
than every 15 seconds**, and only for verifications still waiting.

### 4. Route on `status`

| `status` | Meaning | Do |
|---|---|---|
| `queued` / `processing` | Waiting for a rate-limit slot, or fetching | Keep the customer on "verifying…" |
| `completed` | Receipt fetched; `receipt` holds it (same shape as `GET /receipts`) | Run the [verification checklist](#verification-checklist) — **you** decide accept/reject |
| `not_found` | The bank answered: no such receipt (checked twice, 60s apart) | **Reject automatically.** No human needed |
| `needs_review` | The bank kept failing for 10 minutes, or its page layout changed. `reason` says which | Send to a human |

A slow bank or a long queue does **not** produce `needs_review`. Timeouts are
retried in the background (30s, 60s, then every 2 min), and the 10-minute clock
starts at the first actual bank attempt, not when you submitted. Humans only see
deposits the service genuinely could not check.

---

## The amount rule

**`amount` is what the receiving account actually got. Credit this.**

`fees.payer_total_debit` is what the *payer* was charged, including the bank's
service fee and VAT. It is larger. Crediting it over-pays every deposit.

| Bank | `amount` (credit this) | `fees.payer_total_debit` (never credit) |
|---|---|---|
| telebirr | `2000.00` | `2006.00` |
| Zemen | `4000.00` | `4023.00` |
| CBE | `10500.00` | `10503.60` |

All money is a **JSON string**, never a number. Parse it with a decimal type
(Python `Decimal`, Java `BigDecimal`, C# `decimal`, PHP `bcmath`). Do not parse it
as a float — binary floats cannot represent values like `0.45` exactly, and the
error compounds across transactions.

---

## Response schema

`200 OK`:

```json
{
  "success": true,
  "bank": "tele",
  "reference": "DHS48P7HGQ",
  "status": "COMPLETED",
  "amount": "2000.00",
  "currency": "ETB",
  "paid_at": "2026-08-28T16:39:13",
  "payer_name": "TEST PAYER",
  "payer_account": "2519****8522",
  "receiver_name": "TEST RECEIVER",
  "receiver_account": "2519****5442",
  "fees": {
    "service_fee": "5.22",
    "vat": "0.78",
    "stamp_duty": "0.00",
    "discount": "0.00",
    "payer_total_debit": "2006.00"
  },
  "raw": {}
}
```

| Field | Type | Notes |
|---|---|---|
| `success` | boolean | `true` on every 200; `false` on every error response |
| `bank` | string | The bank key you requested |
| `reference` | string | **The bank's own reference — may differ from what you sent.** See below |
| `status` | string \| null | Uppercased, e.g. `COMPLETED`. `null` when the bank's receipt has no status field |
| `amount` | string | Settled amount, fees excluded. **Credit this** |
| `currency` | string | Always `ETB` |
| `paid_at` | string \| null | ISO 8601. `null` if the bank's date format was not recognized |
| `payer_name` | string \| null | As printed on the receipt |
| `payer_account` | string \| null | Usually masked by the bank |
| `receiver_name` | string \| null | As printed on the receipt |
| `receiver_account` | string \| null | Usually masked; Awash returns it in full |
| `fees.*` | string \| null | `null` when that bank does not report the fee |
| `raw` | object | The bank's original field names, unmodified. For debugging and audit |

### The returned `reference` may differ from what you send

For several banks the lookup key is not the transaction reference:

| Bank | You send | You get back |
|---|---|---|
| telebirr | `DHS48P7HGQ` | `DHS48P7HGQ` (same) |
| CBE | `v2-hfHCxFSFTS8uPZdBBOLz` | `FT26203953V8` (different) |
| Awash | `-E41AE0D86FFA-21XYYW` | `E41AE0D86FFA` (different) |
| Zemen | `94497018108ATWR2520600HM` | `108ATWR2520600HM` (different) |

**Store both.** Enforce your duplicate check on the returned `reference` — that is
the bank's actual transaction identity — and keep the submitted value for audit.

### Field availability varies by bank

Do not require a field that a bank never sends:

| Field | tele | cbe | awash | zemen |
|---|---|---|---|---|
| `status` | yes | yes | **`null`** | yes |
| `paid_at` | yes | yes (with timezone) | yes | date only, time is `00:00:00` |
| `receiver_account` | masked | masked | **unmasked** | masked |
| `fees.payer_total_debit` | yes | yes | **`null`** | yes |

A rule like `status == "COMPLETED"` will reject every valid Awash payment. Check
`status != "FAILED"` instead, or branch per bank.

---

## Error responses

All errors return `{"success": false, "detail": "..."}` with the status below.
Successful responses carry `"success": true`.

The table below is for the synchronous `GET /receipts` endpoint. There,
connection errors and HTTP 502/503/504 from the bank are retried up to 2 times
(0.5s, then 1s) before a `502` is returned; read timeouts are not. With
`POST /verifications` these cases never reach you as errors — they become
background retries and, at worst, `needs_review`.

| Code | Meaning | What your application should do |
|---|---|---|
| `400` | Malformed reference, or unknown bank | Reject. Ask the customer to re-enter |
| `401` | Missing or wrong `X-API-Key` | Fix your credentials. Alert — never show the customer |
| `404` | No receipt exists for that reference | Reject the deposit. The reference is wrong or fabricated |
| `502` | The bank is unreachable | **Do not reject.** Retry later, or queue for manual review |
| `500` | Parsing failed — the bank changed their page | **Do not reject.** Alert the team; verification is degraded |
| `503` | Service has no API keys configured | Alert operations |

### `404` vs `502` / `500` — the important distinction

- `404` means **"this payment does not exist"** → safe to reject.
- `502` and `500` mean **"we could not check"** → the payment may be perfectly
  valid.

Treating `502`/`500` as rejection will deny real customer deposits. Route them to
a manual review queue instead. Never auto-reject on them.

---

## Worked examples

Names below are placeholders; live responses contain real customer names.

### telebirr

```
GET /receipts/tele/DHS48P7HGQ
```

```json
{
  "success": true, "bank": "tele", "reference": "DHS48P7HGQ", "status": "COMPLETED",
  "amount": "2000.00", "currency": "ETB", "paid_at": "2026-08-28T16:39:13",
  "payer_name": "TEST PAYER", "payer_account": "2519****8522",
  "receiver_name": "TEST RECEIVER", "receiver_account": "2519****5442",
  "fees": { "service_fee": "5.22", "vat": "0.78", "stamp_duty": "0.00",
            "discount": "0.00", "payer_total_debit": "2006.00" }
}
```

### CBE

```
GET /receipts/cbe/v2-hfHCxFSFTS8uPZdBBOLz
```

```json
{
  "bank": "cbe", "reference": "FT26203953V8", "status": "COMPLETED",
  "amount": "10500.00", "currency": "ETB", "paid_at": "2026-07-22T06:31:00+00:00",
  "payer_name": "TEST PAYER", "payer_account": "1********1522",
  "receiver_name": "TEST RECEIVER", "receiver_account": "1********5801",
  "fees": { "service_fee": "3.00", "vat": "0.45", "stamp_duty": null,
            "discount": null, "payer_total_debit": "10503.60" }
}
```

Note `paid_at` carries a timezone offset here and not for other banks — parse with
a library that handles both.

### Awash

```
GET /receipts/awash/-E41AE0D86FFA-21XYYW
```

```json
{
  "bank": "awash", "reference": "E41AE0D86FFA", "status": null,
  "amount": "20.00", "currency": "ETB", "paid_at": "2025-08-04T09:25:12",
  "payer_name": "TEST SENDER", "payer_account": "01320******100",
  "receiver_name": "TEST BENEFICIARY", "receiver_account": "1000000000001",
  "fees": { "service_fee": "0.12", "vat": "0.02", "stamp_duty": null,
            "discount": null, "payer_total_debit": null }
}
```

`status` is `null` and the beneficiary account is **unmasked** — Awash is the one
bank where you can fully verify the receiving account.

### Zemen

```
GET /receipts/zemen/94497018108ATWR2520600HM
```

```json
{
  "bank": "zemen", "reference": "108ATWR2520600HM", "status": "COMPLETED",
  "amount": "4000.00", "currency": "ETB", "paid_at": "2025-07-25T00:00:00",
  "payer_name": "TEST PAYER", "payer_account": "103****0001(923141XXXXXX0001)",
  "receiver_name": "Zemen Bank S.C.", "receiver_account": "20****20",
  "fees": { "service_fee": "20.00", "vat": "3.00", "stamp_duty": null,
            "discount": null, "payer_total_debit": "4023.00" }
}
```

`paid_at` has no time component — Zemen prints only the date.

---

## Code samples

### Python

```python
import requests
from decimal import Decimal

BASE = "http://your-server:8060"
HEADERS = {"X-API-Key": "your-key-here"}


class RetryLater(Exception):
    """The bank could not be reached, or parsing broke. NOT a rejection."""


def fetch_receipt(bank, reference):
    response = requests.get(
        f"{BASE}/receipts/{bank}/{reference}", headers=HEADERS, timeout=(5, 45))

    if response.status_code == 404:
        return None                                    # no such payment -> reject
    if response.status_code in (500, 502, 503):
        raise RetryLater(response.json()["detail"])    # could not check -> review
    response.raise_for_status()

    data = response.json()
    data["amount"] = Decimal(data["amount"])           # never float()
    return data
```

### Node.js

```javascript
const BASE = "http://your-server:8060";
const HEADERS = { "X-API-Key": "your-key-here" };

async function fetchReceipt(bank, reference) {
  const res = await fetch(`${BASE}/receipts/${bank}/${reference}`,
                          { headers: HEADERS });

  if (res.status === 404) return null;               // reject
  if (res.status >= 500) throw new Error("retry");   // review queue
  if (!res.ok) throw new Error(`HTTP ${res.status}`);

  return res.json();   // keep amount as a string; compare with decimal.js
}
```

### PHP

```php
$ch = curl_init("http://your-server:8060/receipts/tele/DHS48P7HGQ");
curl_setopt_array($ch, [
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_HTTPHEADER     => ["X-API-Key: your-key-here"],
    CURLOPT_TIMEOUT        => 45,
]);
$body = curl_exec($ch);
$code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
curl_close($ch);

if ($code === 404) {
    // reject the deposit
} elseif ($code >= 500) {
    // retry later / send to manual review
} else {
    $receipt = json_decode($body, true);
    // bcmath, not floats
    if (bccomp($receipt["amount"], $expectedAmount, 2) !== 0) {
        // amount mismatch -> reject
    }
}
```

**Set a client timeout of at least 45 seconds.** The service itself waits up to
30s for a slow bank before returning `502`.

---

## Verification checklist

The service tells you *what the receipt says*. It cannot tell you the payment is
yours or new. Your application must check all of these before crediting:

1. **Amount** — `amount` equals what the customer claims to have sent.
2. **Not already used** — the returned `reference` has never been credited before.
   Enforce this with a **unique database constraint** on `(bank, reference)`, not
   an application-level check. Two concurrent submissions will otherwise both pass.
3. **Status** — not a failed or pending state. Remember Awash sends `null`.
4. **Recipient** — `receiver_account` matches your receiving account.
5. **Freshness** — `paid_at` is recent enough for your policy.

### Two limits you cannot engineer around

**Masked recipient accounts.** telebirr returns `2519****5442`, Zemen `20****20`,
CBE `1********5801`. A receipt proves money reached *some* account with that
suffix — not necessarily yours. Match on the visible digits plus the receiver
name, and treat it as corroboration rather than proof. Awash is the exception: it
returns the beneficiary account in full.

**Replay.** A valid reference verifies successfully forever. Without a uniqueness
constraint, one genuine receipt can be submitted repeatedly for unlimited credit.
This is the single most likely way to lose money with this service.

For a stronger guarantee, an authenticated merchant integration
([developer.ethiotelecom.et](https://developer.ethiotelecom.et/)) returns unmasked
recipient details and an authoritative payment status. This service reads public
receipt pages, which carry no such guarantee.

---

## Operational notes

- **Use `POST /verifications` for anything automated.** It returns in
  milliseconds; set a short client timeout (10s) and retry the `POST` freely with
  the same `client_ref`.
- **Timeouts (sync endpoint)** — the service waits 10s to connect and up to 30s
  to read from a bank (10s for telebirr). Set your client timeout above 45s.
- **Retries (sync endpoint)** — retry `502` with backoff. Never retry `400`,
  `401`, or `404`.
- **Caching** — finished receipts are cached by the service, so re-checking a
  reference costs no bank call. Still store results in your own database.
- **Rate** — `GET /receipts` calls the bank directly and **shares the same
  telebirr quota** as the queue. Calling it in bulk slows every queued check down.
- **Bank independence** — banks fail independently. A `502` for one bank says
  nothing about the others.
- **Availability** — the server must be able to reach each bank. telebirr blocks
  non-Ethiopian IPs, and some banks are unreachable from some networks.
