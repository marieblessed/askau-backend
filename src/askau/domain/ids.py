"""Identifier newtypes, re-exported from where they are defined.

Distinct types for distinct identifiers: passing a ``DocumentId`` where a
``ChunkId`` belongs is a type error rather than a runtime mystery.
"""

from __future__ import annotations

from askau.domain.authz import PrincipalId, UserId
from askau.domain.retrieval import ChunkId, DocumentId

__all__ = ["ChunkId", "DocumentId", "PrincipalId", "UserId"]
