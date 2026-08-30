# 📄 ethiobank-receipts

**Receipt extraction for Ethiopian banks**, as a Python library and an HTTP service.

Extracts structured data from digital bank receipts for CBE, Dashen, Awash, BOA, Zemen
and telebirr, and returns it in one normalized shape so callers don't branch per bank.

---

## ⚠️ Disclaimer

Not an official package. Not affiliated with Ethio Telecom, telebirr, or any Ethiopian
bank. It works by reading publicly available receipt pages.

**A public receipt page is a display surface, not an authenticated system of record.**
See [Verification limits](#-verification-limits) before using this to credit money.

---

## 💰 The one rule that matters

`amount` is the **settled/transferred** figure — what the receiving account actually
got. It **excludes** the provider's service fee and VAT. The payer's total debit is
reported separately under `fees.payer_total_debit`.

| Bank | Settled (`amount`) | Payer paid (`fees.payer_total_debit`) |
|---|---|---|
| telebirr | 2,000.00 | 2,006.00 |
| Zemen | 4,000.00 | 4,023.00 |

Crediting the payer's total over-pays every deposit by the fee.

---

## 📦 Installation

```bash
pip install -e .            # library only
pip install -e ".[api]"     # library + HTTP service
```

---

## 🌐 HTTP service

**PowerShell (Windows):**

```powershell
.\.venv\Scripts\Activate.ps1                  # puts uvicorn on PATH
$env:ETHIOBANK_API_KEYS = "your-key-here"     # comma-separated for multiple keys
uvicorn ethiobank_receipts.api:app --port 8060
```

Without activating, call the venv's executable directly:
`.\.venv\Scripts\uvicorn.exe ethiobank_receipts.api:app --port 8060`

**bash / Linux:**

```bash
source .venv/bin/activate
export ETHIOBANK_API_KEYS="your-key-here"
uvicorn ethiobank_receipts.api:app --port 8060
```

Interactive docs at `/docs`. Every `/receipts` call needs an `X-API-Key` header; with
no keys configured the service fails closed (`503`).

```bash
curl -H "X-API-Key: your-key-here" \
  http://localhost:8060/receipts/tele/DHS48P7HGQ
```

> In PowerShell `curl` is an alias for `Invoke-WebRequest`, so use `curl.exe` for the
> command above, or:
> `Invoke-RestMethod "http://localhost:8060/receipts/tele/DHS48P7HGQ" -Headers @{ "X-API-Key" = "your-key-here" }`

```json
{
  "bank": "tele",
  "reference": "DHS48P7HGQ",
  "status": "COMPLETED",
  "amount": "2000.00",
  "currency": "ETB",
  "paid_at": "2026-08-28T16:39:13",
  "payer_name": "...",
  "payer_account": "2519****8522",
  "receiver_name": "...",
  "receiver_account": "2519****5442",
  "fees": {
    "service_fee": "5.22", "vat": "0.78",
    "stamp_duty": "0.00", "discount": "0.00",
    "payer_total_debit": "2006.00"
  },
  "raw": { }
}
```

Money is returned as a **string**, never a float — parse it as a decimal.
`raw` holds the bank's own field names, unmodified.

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness; no key required |
| `GET` | `/banks` | Supported banks and reference formats |
| `GET` | `/receipts/{bank}/{reference}` | Fetch one receipt |

### Status codes

| Code | Meaning | Caller should |
|---|---|---|
| `200` | Receipt extracted | Proceed to your own checks |
| `400` | Malformed reference / unknown bank | Reject the input |
| `401` | Missing or wrong `X-API-Key` | Fix credentials |
| `404` | No receipt for that reference | Reject the deposit |
| `502` | Bank unreachable | Retry later; **do not** treat as invalid |
| `500` | Parsing broke — the bank changed their layout | **Alert someone.** Verification is degraded |

`502` and `500` are deliberately distinct from `404`: "we couldn't check" must never be
confused with "this payment doesn't exist".

---

## 📖 Python usage

```python
from ethiobank_receipts import get_receipt, extract_receipt

receipt = get_receipt("tele", "DHS48P7HGQ")   # normalized shape
raw = extract_receipt("zemen", "94497018108ATWR2520600HM")  # bank's own field names
```

```python
from ethiobank_receipts.errors import (
    InvalidReference, ReceiptNotFound, BankUnavailable, ReceiptParseError)
```

Never returns a partial record: if a required field (`reference`, `amount`) is missing,
it raises `ReceiptParseError` rather than yielding a `None` amount that a caller might
treat as zero.

---

## 🧰 CLI

```bash
python -m ethiobank_receipts.cli tele DHS48P7HGQ
python -m ethiobank_receipts.cli zemen 94497018108ATWR2520600HM --json
python -m ethiobank_receipts.cli cbe v2-hfHCxFSFTS8uPZdBBOLz
python -m ethiobank_receipts.cli awash -E41AE0D86FFA-21XYYW --raw
```

---

## 🏦 Bank reference formats

Callers pass a **reference, never a URL** — the URL is built internally, so the service
can't be steered at arbitrary hosts (SSRF). A full URL is accepted only if its host
matches that bank's own host.

| Bank | Key | Reference example |
|---|---|---|
| telebirr | `tele` | `DHS48P7HGQ` |
| Commercial Bank of Ethiopia | `cbe` | `v2-hfHCxFSFTS8uPZdBBOLz` (share token from the CBE app) |
| Awash Bank | `awash` | `-E41AE0D86FFA-21XYYW` |
| Zemen Bank | `zemen` | `94497018108ATWR2520600HM` |
| Dashen Bank | `dashen` | `387ETAP2522000WK` |
| Bank of Abyssinia | `boa` | `FT252113TRLT13487` |

---

## 🔒 Verification limits

This service returns data. It **cannot** establish that a payment is *yours* or *new*.
The calling application must handle both:

- **Masked recipient accounts.** telebirr returns `2519****5442` and Zemen `20****20`,
  so a receipt only proves money reached *some* account with that suffix. Match on
  suffix plus name and treat it as corroboration, not proof. Awash is the exception —
  it returns the beneficiary account unmasked.
- **Replay.** A valid reference verifies successfully forever. Enforce a uniqueness
  constraint on `(bank, reference)` before crediting.

Also check `status`, the amount, and `paid_at` freshness against what you expect.

For a stronger guarantee, the authenticated path is partner onboarding at
[developer.ethiotelecom.et](https://developer.ethiotelecom.et/) (appId/appKey, Fabric
Token, RSA-signed requests, `queryOrder` + `notify_url`), which gives unmasked,
authoritative confirmation that scraping a public page cannot.

---

## 🛠 Operational notes

- **Timeouts**: default 10s connect / 30s read. Tune with `ETHIOBANK_CONNECT_TIMEOUT`
  and `ETHIOBANK_READ_TIMEOUT`. Without these a stalled bank host hangs forever.
- **BOA needs Chrome + ChromeDriver** (Selenium). Its shared WebDriver is serialized
  behind a lock, so BOA throughput is one request at a time.
- **CBE** verifies via the share link the CBE app generates
  (`https://mbreciept.cbe.com.et/v2-...`); pass the whole link or just its `v2-...`
  token. The old FT-number + account lookup was retired by CBE and no longer works.
- **telebirr** may block or time out from non-Ethiopian IPs.

---

## 🧪 Tests

```bash
pytest -q
```

Tests run **offline** against redacted fixtures of real receipts — no network, no live
bank endpoints, deterministic.

---

## 📜 License

MIT License — see [LICENSE](https://opensource.org/license/mit).
