"""Utility package for TARGETVAL gateway.

This package provides helper functions for input validation and (in the
future) other utilities.  Currently it exposes simple validators that
ensure required parameters are present and non-empty.  More complex
validation, such as pattern checks or schema enforcement, can be added
as needed.
"""

from .validation import validate_symbol, validate_condition  # noqa: F401
