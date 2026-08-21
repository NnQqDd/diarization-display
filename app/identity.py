"""Who submitted a save.

There is no machine ID available to a web server: the browser sandbox exposes
no hardware, MAC address or OS identifier, by design. What can honestly be
recorded is this, strongest signal first:

``user``
    The username nginx already validated for HTTP basic auth. This is a real
    identity, and the only field here that someone cannot simply choose for
    themselves -- but it is empty unless ``auth_basic`` is switched on.
``client_id``
    A random UUID the browser generated once and kept in ``localStorage``. It
    is stable for that browser profile, which in practice usually means one
    person on one machine. It resets when site data is cleared, and differs
    per browser and in private windows -- so it groups saves together, it does
    not prove who made them.
``ip`` / ``user_agent``
    Where the request came from and what it was made with. Useful for telling
    two annotators apart, weak as identity on a shared network.

Everything except ``user`` is supplied by the client, so treat it as a label,
not as proof. The values are sanitised here because they are written verbatim
into the dataset sidecar.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import asdict, dataclass

# A client id we generated is a UUIDv4. Accept anything of that shape and
# nothing else: this string is written into a JSON file people later read.
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,63}$")

_MAX_USER_AGENT = 200
_MAX_USER = 64


@dataclass(frozen=True)
class Annotator:
    """Identity attached to one save. Any field may be ``None``."""

    user: str | None = None
    client_id: str | None = None
    ip: str | None = None
    user_agent: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "user": self.user,
            "clientId": self.client_id,
            "ip": self.ip,
            "userAgent": self.user_agent,
        }

    def is_empty(self) -> bool:
        return not any(asdict(self).values())


def clean_client_id(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    return text if _CLIENT_ID_RE.match(text) else None


def _clean(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    # Control characters would corrupt the sidecar for whoever reads it next.
    text = "".join(ch for ch in value if ch.isprintable()).strip()
    return text[:limit] or None


def basic_auth_user(authorization: str | None) -> str | None:
    """Username from an ``Authorization: Basic`` header, ignoring the password.

    nginx forwards the header upstream after it has checked the credentials, so
    the name is trustworthy when ``auth_basic`` is on -- and meaningless when it
    is off, since anyone can send the header themselves.
    """
    if not authorization:
        return None
    scheme, _, encoded = authorization.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return None
    return _clean(decoded.split(":", 1)[0], _MAX_USER)


def client_ip(headers, fallback: str | None) -> str | None:
    """The caller's address, preferring what the reverse proxy observed.

    ``X-Forwarded-For`` accumulates left to right, so the first entry is the
    original client. It is only worth reading because nginx sets it and the app
    listens on localhost; a directly reachable app could be told anything.
    """
    forwarded = headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0]
        if cleaned := _clean(first, 64):
            return cleaned
    if real := _clean(headers.get("x-real-ip"), 64):
        return real
    return _clean(fallback, 64)


def identify(request, client_id: str | None = None) -> Annotator:
    """Build an :class:`Annotator` from a Starlette/FastAPI request."""
    headers = request.headers
    user = _clean(headers.get("x-auth-user"), _MAX_USER)
    if not user:
        user = basic_auth_user(headers.get("authorization"))
    return Annotator(
        user=user,
        client_id=clean_client_id(client_id),
        ip=client_ip(headers, request.client.host if request.client else None),
        user_agent=_clean(headers.get("user-agent"), _MAX_USER_AGENT),
    )
