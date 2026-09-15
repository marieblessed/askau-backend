"""Server-sent event framing.

One helper, in one place, because there were two — `ask.py` and
`conversations.py` each had a private `_sse`, and they had already drifted: one
passed `default=str` to `json.dumps` and the other did not, so a date in a
payload serialized from one endpoint and raised from the other.

The event names and payload shapes are defined in `schemas/wire.py`. They are
not in the OpenAPI document, because SSE has no representation there — which is
exactly why they need to be models rather than inline dicts. A hand-built dict
is where a stray snake_case key survives a convention change unnoticed.
"""

from __future__ import annotations

import json

from askau.api.schemas.wire import WireModel


def sse(event: str, payload: WireModel) -> str:
    """Format one frame.

    Takes a model, never a dict, so the payload goes through the same camelCase
    serialization as every other response on this API. `mode="json"` handles the
    dates and enums that made the two previous copies of this function differ.
    """
    return f"event: {event}\ndata: {json.dumps(payload.model_dump(mode='json'))}\n\n"
