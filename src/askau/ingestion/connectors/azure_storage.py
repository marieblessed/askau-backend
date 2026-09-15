"""Authenticated Azure Blob Storage access over the REST API.

The counterpart to `graph.py`, and written the same way for the same reason:
`pyproject.toml` bans the vendor SDK outside `*/adapters/`, so a connector talks
HTTP. That rule earned its keep here — the first draft of this connector
imported `azure.storage.blob` and ruff refused it.

Hand-rolling costs a list-blobs XML parser and a pagination loop. It buys no new
dependency (the Azure SDK pulls `azure-core`, `azure-identity`, `msal` and their
transitive tree into the worker image), one HTTP client shared with everything
else, and the same throttle handling the Graph client already has.

## Two ways to authenticate, and when each is right

* **Service principal** — a client-credentials token for
  `https://storage.azure.com/.default`, exactly like Graph. What a deployment
  uses: the credential is an Entra identity with a role assignment on the
  container, so access is revocable and shows up in an audit trail.
* **SAS token** — appended to the query string. For the emulator and for a
  time-boxed test against a real account. A SAS is a bearer credential in a URL:
  whoever holds it has whatever it grants, until it expires.

There is deliberately no account-key mode. An account key grants everything the
storage account can do, to anybody who has it, with no expiry and no audit —
and once it is in a `.env` file it is in a backup, a screenshot and a chat log.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import parse_qs, quote

import httpx
from lxml import etree

#: The REST API version this client speaks. Pinned: Azure keeps old versions
#: working indefinitely, and an unpinned client silently changes behaviour when
#: the service default moves.
API_VERSION = "2021-12-02"


class AzureStorageError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"Azure Storage {status}: {detail}")
        self.status = status
        self.detail = detail


class AzureStorageClient:
    """Blob access with token caching and throttle handling."""

    #: Refresh this long before expiry, so a token does not lapse mid-request.
    _REFRESH_MARGIN_S = 300

    def __init__(
        self,
        *,
        account_url: str,
        tenant_id: str = "",
        client_id: str = "",
        client_secret: str = "",
        sas_token: str = "",
        client: httpx.AsyncClient | None = None,
        max_attempts: int = 4,
    ) -> None:
        self._account_url = account_url.rstrip("/")
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._sas = sas_token.lstrip("?")
        # Long timeout, for the same reason as the Graph client: a 40 MB PDF
        # over a constrained link is legitimately slow, and giving up on it
        # turns a large document into a permanent ingestion failure.
        self._http = client or httpx.AsyncClient(timeout=60.0)
        self._max_attempts = max_attempts
        self._token: str | None = None
        self._expires_at = 0.0

    @property
    def account_url(self) -> str:
        return self._account_url

    async def _bearer(self) -> str:
        if self._token and time.monotonic() < self._expires_at:
            return self._token

        resp = await self._http.post(
            f"https://login.microsoftonline.com/{self._tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "scope": "https://storage.azure.com/.default",
            },
        )
        if resp.status_code != 200:
            # The body names the misconfiguration — wrong tenant, expired
            # secret, role assignment missing — and an operator cannot fix it
            # without that. It contains no secret of ours; the secret is what
            # we sent.
            raise AzureStorageError(resp.status_code, resp.text[:400])

        payload = resp.json()
        self._token = str(payload["access_token"])
        self._expires_at = time.monotonic() + float(payload.get("expires_in", 3600))
        self._expires_at -= self._REFRESH_MARGIN_S
        return self._token

    async def _request(self, url: str, *, params: dict[str, str] | None = None) -> httpx.Response:
        headers = {"x-ms-version": API_VERSION}
        if not self._sas:
            headers["Authorization"] = f"Bearer {await self._bearer()}"

        # The SAS is merged into `params`, not appended to the URL.
        #
        # httpx *replaces* an existing query string when `params` is given
        # rather than merging, so building "url?sas" and then passing
        # {"restype": "container", ...} silently dropped the credential — and
        # the emulator answered `AuthorizationFailure`, which reads like a bad
        # signature rather than an absent one.
        query = dict(params or {})
        if self._sas:
            query.update({k: v[0] for k, v in parse_qs(self._sas, keep_blank_values=True).items()})
        target = url

        last: httpx.Response | None = None
        for attempt in range(self._max_attempts):
            resp = await self._http.get(target, params=query or None, headers=headers)
            if resp.status_code not in (429, 503):
                if resp.status_code >= 400:
                    raise AzureStorageError(resp.status_code, resp.text[:400])
                return resp
            # Azure names the wait in `Retry-After`. Honoured rather than
            # guessed: backing off faster than the service asked is how a
            # throttle becomes a longer throttle.
            last = resp
            delay = float(resp.headers.get("Retry-After", 2**attempt))
            await _sleep(delay)
        raise AzureStorageError(last.status_code if last else 0, "throttled and out of attempts")

    def blob_url(self, container: str, name: str) -> str:
        return f"{self._account_url}/{quote(container)}/{quote(name)}"

    async def list_blobs(
        self, container: str, *, prefix: str = "", include_metadata: bool = True
    ) -> list[dict[str, Any]]:
        """Every blob under `prefix`, following continuation markers.

        Metadata is requested with the listing rather than fetched per blob: the
        access strategy may read it for every document, and a HEAD per blob over
        a forty-thousand-item container is forty thousand round trips.
        """
        out: list[dict[str, Any]] = []
        marker = ""
        while True:
            params = {"restype": "container", "comp": "list"}
            if prefix:
                params["prefix"] = prefix
            if include_metadata:
                params["include"] = "metadata"
            if marker:
                params["marker"] = marker

            resp = await self._request(f"{self._account_url}/{quote(container)}", params=params)
            # Parsed with entities and network access off. The response comes
            # from our own storage account over TLS, so this is defence against
            # a situation that should not arise — but "should not arise" is a
            # poor reason to leave an XML parser able to fetch a URL somebody
            # else wrote, and ruff was right to refuse the stdlib parser here.
            root = etree.fromstring(resp.content, parser=_PARSER)
            for blob in root.iterfind("./Blobs/Blob"):
                out.append(_parse_blob(blob))
            marker = (root.findtext("NextMarker") or "").strip()
            if not marker:
                return out

    async def download(self, container: str, name: str) -> bytes:
        resp = await self._request(self.blob_url(container, name))
        return resp.content

    async def aclose(self) -> None:
        await self._http.aclose()


#: No entity resolution, no DTD, no network. Rebuilt per call would be wasteful;
#: lxml parsers are not thread-safe but this client is used from one task at a
#: time, and the ingestion pass is sequential by construction.
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)


def _parse_blob(node: Any) -> dict[str, Any]:
    props = node.find("Properties")
    # `is not None`, not truthiness: an lxml element with no children is falsy
    # today and will be truthy in a future release, so `or ()` is a behaviour
    # change waiting to happen — and lxml says so with a FutureWarning.
    meta_node = node.find("Metadata")
    metadata = (
        {(child.tag or "").lower(): (child.text or "") for child in meta_node}
        if meta_node is not None
        else {}
    )
    size = (props.findtext("Content-Length") if props is not None else None) or ""
    return {
        "name": node.findtext("Name") or "",
        "etag": (props.findtext("Etag") if props is not None else None) or "",
        "last_modified": (props.findtext("Last-Modified") if props is not None else None) or "",
        "size": int(size) if size.isdigit() else None,
        "metadata": metadata,
    }


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
