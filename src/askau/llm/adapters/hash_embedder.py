"""Deterministic offline embeddings for development, CI and tests.

Not a semantic model — it hashes token n-grams into a fixed vector space. That
makes it useless for ranking quality and perfect for everything else: it needs
no credentials, no download and no GPU, it is byte-identical across runs, and it
exercises the entire pipeline including pgvector indexing and distance ordering.

Lexical overlap does move the vectors closer together, so tests can assert
"this query is nearer that chunk than the other one" — enough to verify plumbing
and ordering without pretending to verify relevance.

``Settings`` refuses this provider in production for exactly that reason.
"""

from __future__ import annotations

import hashlib
import math
import re
from itertools import pairwise

_TOKEN = re.compile(r"\w+", re.UNICODE)


class HashEmbedder:
    def __init__(self, dimensions: int = 1024, model_name: str = "hash-1024") -> None:
        self._dim = dimensions
        self._model = model_name

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dim

    async def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    def _embed(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        tokens = [t.lower() for t in _TOKEN.findall(text)]
        if not tokens:
            # A zero vector has no direction, and cosine distance against it is
            # undefined. Return a fixed unit vector instead so ingestion of an
            # empty-ish chunk cannot poison the index.
            vec[0] = 1.0
            return vec

        # Unigrams plus bigrams: bigrams give word order a little weight, which
        # keeps "leave policy" and "policy leave" from being identical.
        grams = tokens + [f"{a}_{b}" for a, b in pairwise(tokens)]
        for gram in grams:
            digest = hashlib.blake2b(gram.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self._dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[index] += sign

        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            vec[0] = 1.0
            return vec
        return [v / norm for v in vec]
