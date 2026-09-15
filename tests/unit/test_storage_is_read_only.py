"""AskAU reads the document store. It never writes to it.

Two things enforce that today — the client has no write method, and the service
principal holds only `Storage Blob Data Reader` — and neither is checked by
anything. A `PUT` added here would compile, typecheck, pass every existing test,
and fail only in production against a credential that refuses it, which is the
good case. Against a credential someone over-granted while debugging, it would
simply work.

The product's premise is that the corpus is what a knowledge administrator
approved (BR-001). An ingestion path that can write is a path that can change
the corpus without an approval, so this is an architectural boundary rather than
a coding preference — the same kind the import-linter contracts guard.
"""

from __future__ import annotations

import re
from pathlib import Path

_CLIENT = Path(__file__).resolve().parents[2] / "src/askau/ingestion/connectors/azure_storage.py"

#: The one non-GET call the client is allowed: the OAuth token exchange, which
#: goes to login.microsoftonline.com and touches no storage endpoint.
_TOKEN_ENDPOINT = "oauth2/v2.0/token"


class TestTheStorageClientCannotWrite:
    def test_it_issues_no_write_verbs_against_storage(self) -> None:
        source = _CLIENT.read_text()
        calls = re.findall(r"_http\.(get|post|put|patch|delete|request)\(", source)

        assert "put" not in calls, "the storage client must not PUT"
        assert "patch" not in calls, "the storage client must not PATCH"
        assert "delete" not in calls, "the storage client must not DELETE"
        assert "request" not in calls, (
            "a generic `request(` hides the verb from this check — name the method"
        )

        # A POST is permitted only for the token exchange. If one appears
        # elsewhere, this test should fail rather than be widened.
        assert source.count("_http.post(") == source.count(_TOKEN_ENDPOINT), (
            "the only POST allowed is the OAuth token request"
        )

    def test_it_exposes_no_write_method(self) -> None:
        from askau.ingestion.connectors.azure_storage import AzureStorageClient

        forbidden = {"upload", "put", "write", "delete", "remove", "create", "set_metadata"}
        public = {n for n in dir(AzureStorageClient) if not n.startswith("_")}
        assert not (public & forbidden), (
            f"write-shaped methods on a read-only client: {public & forbidden}"
        )
