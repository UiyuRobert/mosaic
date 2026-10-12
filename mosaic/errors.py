"""MOSAIC error hierarchy and logging setup."""

from __future__ import annotations

import logging

# ---------------------------------------------------------------------------
# Central logger — all modules should use ``logging.getLogger(__name__)``
# which inherits from the root "mosaic" logger configured here.
# ---------------------------------------------------------------------------

logger = logging.getLogger("mosaic")

if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s [%(name)s] %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.ERROR)


def set_verbose_logging(verbose: bool) -> None:
    """Lower log level to WARNING when verbose mode is active."""
    logger.setLevel(logging.WARNING if verbose else logging.ERROR)


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class MosaicError(Exception):
    """Base exception for all MOSAIC-specific errors."""


class SourceError(MosaicError):
    """Raised when a search source fails (network, parsing, auth)."""


class DownloadError(MosaicError):
    """Raised when a PDF download fails.

    ``reason`` carries a machine-readable category for retry/limit logic:
    ``not_found | access_denied | rate_limited | server_error | timeout |
    dns_error | unreachable | invalid_pdf | html_response | too_small |
    empty | missing_eof | unparseable | disk_full | exists | cancelled |
    unsafe_url``.
    """

    def __init__(self, reason: str, detail: str = "", retry_after: float | None = None):
        self.reason = reason
        self.retry_after = retry_after
        super().__init__(detail or reason)


# DownloadError reasons that are worth a bounded retry (transient).
TRANSIENT_REASONS = frozenset(
    {"timeout", "server_error", "rate_limited", "unreachable", "dns_error"}
)


class ConfigError(MosaicError):
    """Raised when configuration is invalid or missing."""
