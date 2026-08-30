""" Download PDF from URL and return local path. """
import os
import requests
import tempfile
import urllib3

session = requests.Session()

# (connect, read) seconds. Bank receipt hosts routinely accept the TLS
# handshake and then never answer for an unknown reference, so a read
# timeout is what keeps a bad reference from hanging forever.
DEFAULT_TIMEOUT = (
    float(os.environ.get("ETHIOBANK_CONNECT_TIMEOUT", 10)),
    float(os.environ.get("ETHIOBANK_READ_TIMEOUT", 30)),
)


def download_pdf_from_url(url, verify_ssl=False, timeout=DEFAULT_TIMEOUT):
    """Downloads a PDF from a URL and saves it to a temp file with connection pooling."""
    if not verify_ssl:
        # Skipping verification is deliberate here (CBE serves an untrusted
        # cert), so don't spam the caller with a warning about it.
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    response = session.get(url, verify=verify_ssl, timeout=timeout)
    response.raise_for_status()
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
        tmp_file.write(response.content)
        return tmp_file.name


def fetch_json(url, headers=None, verify_ssl=False, timeout=DEFAULT_TIMEOUT):
    """GET a URL and return the parsed JSON body, sharing the pooled session.

    Used by banks that serve receipt data as JSON rather than a PDF.
    """
    if not verify_ssl:
        # Same untrusted-cert situation as the PDF hosts.
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    response = session.get(url, headers=headers, verify=verify_ssl, timeout=timeout)
    response.raise_for_status()
    return response.json()
