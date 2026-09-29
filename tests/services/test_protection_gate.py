"""Tests for ProtectionGateMixin — the §CP.R protected-account guard."""

from __future__ import annotations

import pytest

from socialhome.domain.child_protection import (
    AccountProtectedError,
    ProtectedCapability,
)
from socialhome.services.protection_gate import ProtectionGateMixin


class _FakeCp:
    def __init__(self, protected: set[str]):
        self._protected = protected
        self.calls: list[tuple[str, ProtectedCapability]] = []

    async def is_restricted(self, user_id, capability):
        return user_id in self._protected

    async def require_unrestricted(self, user_id, capability):
        self.calls.append((user_id, capability))
        if user_id in self._protected:
            raise AccountProtectedError(capability)


class _Svc(ProtectionGateMixin):
    __slots__ = ("_child_protection",)

    def __init__(self):
        self._child_protection = None


async def test_gate_raises_for_protected_user():
    svc = _Svc()
    cp = _FakeCp({"kid"})
    svc.attach_child_protection(cp)
    with pytest.raises(AccountProtectedError) as exc_info:
        await svc._require_unrestricted("kid", ProtectedCapability.BAZAAR)
    assert exc_info.value.capability is ProtectedCapability.BAZAAR
    assert cp.calls == [("kid", ProtectedCapability.BAZAAR)]


async def test_gate_passes_for_unprotected_user():
    svc = _Svc()
    svc.attach_child_protection(_FakeCp({"kid"}))
    await svc._require_unrestricted("adult", ProtectedCapability.API_TOKENS)


async def test_gate_is_noop_when_child_protection_is_not_wired():
    # Unit-test construction path — production always wires it in app.py.
    svc = _Svc()
    await svc._require_unrestricted("kid", ProtectedCapability.BAZAAR)
    assert await svc._is_restricted("kid", ProtectedCapability.BAZAAR) is False


async def test_is_restricted_reports_without_raising():
    svc = _Svc()
    svc.attach_child_protection(_FakeCp({"kid"}))
    assert await svc._is_restricted("kid", ProtectedCapability.PUBLIC_MOMENTS)
    assert not await svc._is_restricted("adult", ProtectedCapability.PUBLIC_MOMENTS)


def test_mixin_owns_no_slots_so_it_composes():
    assert ProtectionGateMixin.__slots__ == ()

    class _OtherSlotted:
        __slots__ = ("_other",)

    class _Both(_OtherSlotted, ProtectionGateMixin):
        __slots__ = ("_child_protection",)

    _Both()
