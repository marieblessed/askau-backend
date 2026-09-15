"""BR-001 — an unapproved source cannot be switched on.

The rule is held by a CHECK constraint on `knowledge_sources` rather than by
application code, and the whole point of that choice is that it survives writes
which never touch the application: a data-fix script, a migration, an engineer
connecting directly. Testing it through the API would therefore prove the wrong
thing — the API is the one path that was never in doubt.

So these tests write to the database directly, as those bypass routes would.

Worth stating plainly: the approval *process* is not part of this phase. There
is one source and the seed inserts it already approved. What is being asserted
here is only that the guard is real and will hold when the process arrives —
which had not been checked before, and is the sole basis for claiming BR-001 is
enforced at all.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import requires_db

pytestmark = [pytest.mark.integration, requires_db]

_CONSTRAINT = "active_requires_approval"


class TestUnapprovedSourceCannotBeActive:
    async def test_insert_active_without_approver_is_refused(
        self, admin_engine: AsyncEngine
    ) -> None:
        # Cleaned up in a finally even though the insert is expected to fail.
        # If the constraint is ever missing the insert succeeds, and the row it
        # leaves behind then blocks the constraint from being put back — which
        # turns one failing test into a database an engineer has to repair by
        # hand. Found the hard way.
        try:
            with pytest.raises(IntegrityError) as caught:
                async with admin_engine.begin() as conn:
                    await conn.execute(
                        text("""
                    INSERT INTO knowledge_sources
                        (name, source_type, description, business_owner_id, department,
                         default_classification, location, status)
                    SELECT 'BR-001 probe (insert)', 'filesystem', 'test', u.id, 'MISD',
                           'internal', '{"path": "probe"}', 'active'
                    FROM users u WHERE u.email = 'admin.knowledge@africanunion.org'
                    """)
                    )
            assert _CONSTRAINT in str(caught.value), str(caught.value)[:300]
        finally:
            async with admin_engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM knowledge_sources WHERE name = 'BR-001 probe (insert)'")
                )

    async def test_activating_an_unapproved_source_is_refused(
        self, admin_engine: AsyncEngine
    ) -> None:
        """The likelier mistake: a source created correctly, switched on later."""
        async with admin_engine.begin() as conn:
            await conn.execute(
                text("""
                INSERT INTO knowledge_sources
                    (name, source_type, description, business_owner_id, department,
                     default_classification, location, status)
                SELECT 'BR-001 probe (update)', 'filesystem', 'test', u.id, 'MISD',
                       'internal', '{"path": "probe"}', 'draft'
                FROM users u WHERE u.email = 'admin.knowledge@africanunion.org'
                """)
            )
        try:
            with pytest.raises(IntegrityError) as caught:
                async with admin_engine.begin() as conn:
                    await conn.execute(
                        text("""
                        UPDATE knowledge_sources SET status = 'active'
                        WHERE name = 'BR-001 probe (update)'
                        """)
                    )
            assert _CONSTRAINT in str(caught.value), str(caught.value)[:300]
        finally:
            async with admin_engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM knowledge_sources WHERE name = 'BR-001 probe (update)'")
                )

    async def test_an_approved_source_may_be_activated(self, admin_engine: AsyncEngine) -> None:
        """The positive case. Without it the two refusals above would pass just
        as happily against a table that rejects every activation."""
        async with admin_engine.begin() as conn:
            await conn.execute(
                text("""
                INSERT INTO knowledge_sources
                    (name, source_type, description, business_owner_id, department,
                     default_classification, location, status)
                SELECT 'BR-001 probe (approved)', 'filesystem', 'test', u.id, 'MISD',
                       'internal', '{"path": "probe"}', 'draft'
                FROM users u WHERE u.email = 'admin.knowledge@africanunion.org'
                """)
            )
        try:
            async with admin_engine.begin() as conn:
                await conn.execute(
                    text("""
                    UPDATE knowledge_sources
                    SET approved_by = (SELECT id FROM users
                                       WHERE email = 'admin.knowledge@africanunion.org'),
                        approved_at = now(),
                        status = 'active'
                    WHERE name = 'BR-001 probe (approved)'
                    """)
                )
                status = (
                    await conn.execute(
                        text("""SELECT status::text FROM knowledge_sources
                                WHERE name = 'BR-001 probe (approved)'""")
                    )
                ).scalar_one()
            assert status == "active"
        finally:
            async with admin_engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM knowledge_sources WHERE name = 'BR-001 probe (approved)'")
                )
