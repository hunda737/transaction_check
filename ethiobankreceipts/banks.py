"""Per-bank reference formats and URL construction.

Callers pass a *reference*, never a URL. The extractors will fetch whatever URL
they are given, so accepting a user-supplied link would turn this service into an
SSRF proxy into internal infrastructure. Every URL is built here from a validated
reference instead.
"""
import re
from urllib.parse import urlparse

from ethiobank_receipts.errors import InvalidReference


class BankSpec:
    def __init__(self, key, name, host, pattern, template, example):
        self.key = key
        self.name = name
        self.host = host
        self.pattern = re.compile(pattern)
        self.template = template
        self.example = example

    def build_url(self, reference: str) -> str:
        return self.template.format(ref=reference)


BANKS = {
    "tele": BankSpec(
        "tele", "telebirr (Ethio Telecom)", "transactioninfo.ethiotelecom.et",
        r"^[A-Za-z0-9]{8,20}$",
        "https://transactioninfo.ethiotelecom.et/receipt/{ref}",
        "DHS48P7HGQ",
    ),
    "cbe": BankSpec(
        "cbe", "Commercial Bank of Ethiopia", "mbreciept.cbe.com.et",
        r"^v2-[A-Za-z0-9_-]{6,}$",
        "https://mbreciept.cbe.com.et/{ref}",
        "v2-hfHCxFSFTS8uPZdBBOLz",
    ),
    "awash": BankSpec(
        "awash", "Awash Bank", "awashpay.awashbank.com",
        r"^-?[A-Za-z0-9]+(-[A-Za-z0-9]+)*$",
        "https://awashpay.awashbank.com:8225/{ref}",
        "-E41AE0D86FFA-21XYYW",
    ),
    "zemen": BankSpec(
        "zemen", "Zemen Bank", "share.zemenbank.com",
        r"^[A-Za-z0-9]{10,40}$",
        "https://share.zemenbank.com/rt/{ref}/pdf",
        "94497018108ATWR2520600HM",
    ),
    "dashen": BankSpec(
        "dashen", "Dashen Bank", "receipt.dashensuperapp.com",
        r"^[A-Za-z0-9]{10,30}$",
        "https://receipt.dashensuperapp.com/receipt/{ref}",
        "387ETAP2522000WK",
    ),
    "boa": BankSpec(
        "boa", "Bank of Abyssinia", "cs.bankofabyssinia.com",
        r"^[A-Za-z0-9]{10,30}$",
        "https://cs.bankofabyssinia.com/slip/?trx={ref}",
        "FT252113TRLT13487",
    ),
}


def get_bank(bank: str) -> BankSpec:
    spec = BANKS.get((bank or "").lower())
    if spec is None:
        raise InvalidReference(
            f"Unsupported bank: {bank!r}. Supported: {', '.join(sorted(BANKS))}")
    return spec


def resolve_url(bank: str, reference_or_url: str) -> str:
    """Validate a reference (or a same-host URL) and return the URL to fetch."""
    spec = get_bank(bank)
    value = (reference_or_url or "").strip()
    if not value:
        raise InvalidReference(f"A {spec.key} reference is required")

    if value.lower().startswith(("http://", "https://")):
        # A full URL is accepted only when it points at this bank's own host,
        # so a caller cannot steer the fetch anywhere else.
        host = (urlparse(value).hostname or "").lower()
        if host != spec.host:
            raise InvalidReference(
                f"URL host {host!r} is not {spec.host!r} for bank {spec.key}")
        return value

    if not spec.pattern.match(value):
        raise InvalidReference(
            f"Invalid {spec.key} reference {value!r}; expected format like {spec.example!r}")
    return spec.build_url(value)
