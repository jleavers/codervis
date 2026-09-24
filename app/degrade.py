"""The fixed degrade vocabulary shared by the live clients and the payload boundary.

Both quota endpoints are undocumented and both credential files are written by
another program, so any part of the payload may fail to be produced. When that
happens the payload boundary in `app/main.py` reports the failure with one of
the strings below and with nothing else: never `str(exc)`, never a repr, never
text that came from upstream or from a credential file. `source_error` is served
unauthenticated, so stringifying an exception there would publish whatever the
exception happened to carry — an exception raised while building the request
carries the bearer token itself.

The live clients tag each failure they raise with one of these codes so the
boundary can say *which* kind of failure it was without quoting anything. A
failure that arrives untagged is still contained; it just reports the generic
`UNCLASSIFIED` message.
"""

from __future__ import annotations


CREDENTIALS = "credentials"
AUTH = "auth"
HTTP = "http"
TRANSPORT = "transport"
SHAPE = "shape"
UNCLASSIFIED = "unclassified"
INTERNAL = "internal"

MESSAGES: dict[str, str] = {
    CREDENTIALS: "stored credential unavailable or unusable",
    AUTH: "upstream rejected the stored credential",
    HTTP: "upstream returned an error response",
    TRANSPORT: "upstream unreachable",
    SHAPE: "upstream response not understood",
    UNCLASSIFIED: "provider data unavailable",
    INTERNAL: "internal error",
}

VOCABULARY: frozenset[str] = frozenset(MESSAGES.values())


def message(code: object, default: str = UNCLASSIFIED) -> str:
    """The fixed message for `code`, or the default classification's message.

    `code` is whatever an exception carried, so it is treated as untrusted: a
    value that is not one of the codes above never reaches the payload.
    """
    if isinstance(code, str) and code in MESSAGES:
        return MESSAGES[code]
    return MESSAGES[default]
