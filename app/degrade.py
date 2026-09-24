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
STALE = "stale"
ACTIVITY = "activity"
UNCLASSIFIED = "unclassified"
INTERNAL = "internal"

MESSAGES: dict[str, str] = {
    CREDENTIALS: "stored credential unavailable or unusable",
    AUTH: "upstream rejected the stored credential",
    HTTP: "upstream returned an error response",
    TRANSPORT: "upstream unreachable",
    SHAPE: "upstream response not understood",
    # The source's own thread stopped advancing -- a read with no deadline of
    # its own, hung. Nothing here says *how* old the data is: the age is a
    # number this process computed, but the rule is that `source_error` is a
    # fixed string, and a rule with one exception is not a rule.
    STALE: "provider data is no longer being refreshed",
    # Only ever logged. A failed activity read degrades to `last_activity:
    # null`, which the UI renders as "—"; it has no message of its own.
    ACTIVITY: "local activity reading unavailable",
    UNCLASSIFIED: "provider data unavailable",
    INTERNAL: "internal error",
}

# The codes a *client* may claim. ACTIVITY is diagnostic only: a failed
# activity read degrades to `last_activity: null`, so its message must never
# become a quota section's `source_error`, however a client tagged itself.
# STALE is excluded for the same reason from the other direction: only the
# refresher knows whether a source stopped being refreshed, and a client
# tagging itself with it would report that about a source refreshing perfectly
# well. The boundary sets STALE itself, before this set is consulted.
SERVABLE: frozenset[str] = frozenset(MESSAGES) - {ACTIVITY, STALE}

VOCABULARY: frozenset[str] = frozenset(MESSAGES.values())


def message(code: object) -> str:
    """The fixed message for `code`, or the generic one.

    `code` is whatever an exception carried, so it is treated as untrusted: a
    value that is not one of the codes above never reaches the payload.
    """
    if isinstance(code, str) and code in MESSAGES:
        return MESSAGES[code]
    return MESSAGES[UNCLASSIFIED]
