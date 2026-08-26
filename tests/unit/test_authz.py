"""AuthorizationContext — the type the security model rests on."""

from __future__ import annotations

import pytest

from askau.domain.authz import AuthorizationContext, ClassificationScope, PrincipalId, UserId
from askau.domain.enums import AppRole, Classification, ClassificationRank


def ctx(*principals: int, roles: frozenset[AppRole] = frozenset()) -> AuthorizationContext:
    return AuthorizationContext(
        user_id=UserId("u-1"),
        principals=frozenset(PrincipalId(p) for p in principals),
        acl_version=1,
        roles=roles,
    )


class TestPrincipalArray:
    def test_is_sorted_for_stable_query_parameters(self) -> None:
        assert ctx(9, 3, 7, 1).principal_array() == [1, 3, 7, 9]

    def test_set_ordering_does_not_affect_output(self) -> None:
        assert ctx(5, 2, 8).principal_array() == ctx(8, 5, 2).principal_array()


class TestAclSignature:
    def test_same_principals_same_signature(self) -> None:
        assert ctx(1, 2, 3).acl_signature() == ctx(3, 2, 1).acl_signature()

    def test_different_principals_different_signature(self) -> None:
        """The one place a collision would become a data-leak bug."""
        assert ctx(1, 2, 3).acl_signature() != ctx(1, 2, 4).acl_signature()

    def test_subset_differs_from_superset(self) -> None:
        assert ctx(1, 2).acl_signature() != ctx(1, 2, 3).acl_signature()

    def test_is_stable_across_calls(self) -> None:
        c = ctx(1, 2, 3)
        assert c.acl_signature() == c.acl_signature()


class TestEmptyPrincipals:
    def test_rejected_at_construction(self) -> None:
        """An empty set produces a query that matches nothing — correct, but
        indistinguishable from a bug. Fail loudly instead."""
        with pytest.raises(ValueError, match="zero principals"):
            AuthorizationContext(user_id=UserId("u-1"), principals=frozenset(), acl_version=1)


class TestRoles:
    def test_end_user_is_not_admin(self) -> None:
        assert not ctx(1, roles=frozenset({AppRole.END_USER})).is_admin

    @pytest.mark.parametrize(
        "role",
        [AppRole.KNOWLEDGE_ADMIN, AppRole.SYSTEM_ADMIN, AppRole.SECURITY_ADMIN],
    )
    def test_admin_roles_are_admin(self, role: AppRole) -> None:
        assert ctx(1, roles=frozenset({role})).is_admin

    def test_no_roles_is_not_admin(self) -> None:
        assert not ctx(1).is_admin


class TestAuthorizationSurface:
    def test_context_exposes_no_document_level_decision_method(self) -> None:
        """BR-006 as a structural assertion.

        Authorization is set membership resolved upstream and applied in SQL. If
        someone adds a `can_access(document)` helper here, the model gains a place
        to ask permission questions at generation time — which is exactly the
        pattern the SRS forbids. This test fails when that shape appears.
        """
        suspicious = {
            name
            for name in dir(AuthorizationContext)
            if not name.startswith("_")
            and any(k in name for k in ("can_", "may_", "allow", "authorize", "check"))
        }
        assert suspicious == set(), (
            f"AuthorizationContext gained decision methods {suspicious}; "
            "authorization belongs in the retrieval predicate, not in a callable "
            "the generation path could reach"
        )


class TestClassificationScope:
    def test_no_grants_means_public_only(self) -> None:
        assert ClassificationScope.from_grants(frozenset()).allowed() == (Classification.PUBLIC,)

    def test_scope_is_inclusive_of_lower_tiers(self) -> None:
        scope = ClassificationScope.from_grants(frozenset({Classification.CONFIDENTIAL}))
        assert scope.allowed() == (
            Classification.PUBLIC,
            Classification.INTERNAL,
            Classification.CONFIDENTIAL,
        )

    def test_highly_restricted_sees_everything(self) -> None:
        scope = ClassificationScope.from_grants(frozenset({Classification.HIGHLY_RESTRICTED}))
        assert len(scope.allowed()) == 4

    def test_rank_ordering_matches_sensitivity(self) -> None:
        assert (
            ClassificationRank.PUBLIC
            < ClassificationRank.INTERNAL
            < ClassificationRank.CONFIDENTIAL
            < ClassificationRank.HIGHLY_RESTRICTED
        )
