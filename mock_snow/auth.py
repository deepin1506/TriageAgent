"""Basic or Bearer auth, checked with constant-time comparison."""

import base64
import binascii
from secrets import compare_digest

from fastapi import Request

from .errors import SnowAPIError
from .settings import MockSettings


def _same(a: str, b: str) -> bool:
    return compare_digest(a.encode(), b.encode())


def current_user(request: Request) -> str:
    """FastAPI dependency: returns the authenticated user name or raises 401."""
    settings: MockSettings = request.app.state.settings
    scheme, _, credentials = request.headers.get("Authorization", "").partition(" ")

    if scheme.lower() == "bearer" and settings.snow_token:
        if _same(credentials, settings.snow_token.get_secret_value()):
            return settings.snow_username
    elif scheme.lower() == "basic":
        try:
            user, _, password = base64.b64decode(credentials).decode().partition(":")
        except (binascii.Error, UnicodeDecodeError):
            user, password = "", ""
        if _same(user, settings.snow_username) and _same(
            password, settings.snow_password.get_secret_value()
        ):
            return user

    raise SnowAPIError(401, "User Not Authenticated", "Required to provide Auth information")
