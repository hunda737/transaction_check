"""Exceptions raised by the extraction layer.

The distinction matters for anything that credits money: a caller must be able to
tell "this reference does not exist" (safe to reject) from "we could not parse the
page" (the bank changed their layout and verification is silently degraded).
"""


class EthiobankError(Exception):
    """Base class for all errors raised by this package."""


class InvalidReference(EthiobankError, ValueError):
    """The supplied reference does not match the bank's expected format."""


class ReceiptNotFound(EthiobankError):
    """The bank responded, but no receipt exists for this reference."""


class ReceiptParseError(EthiobankError):
    """A receipt was returned but a required field could not be read.

    This means the bank changed their HTML/PDF layout. It must never be
    swallowed -- a missing amount silently treated as zero is a financial bug.
    """


class BankUnavailable(EthiobankError):
    """The bank endpoint could not be reached (timeout, connection error, 5xx)."""
