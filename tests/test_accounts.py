"""Tests for the multi-account resolver."""

from __future__ import annotations

import pytest

from linkuma_mcp import accounts
from linkuma_mcp.errors import LinkumaConfigError


@pytest.fixture(autouse=True)
def _reset_clients(monkeypatch):
    # The conftest sets LINKUMA_API_KEY; clear any prior cache.
    accounts.reset_clients()
    yield
    accounts.reset_clients()


def test_single_key_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("LINKUMA_API_KEYS_JSON", raising=False)
    monkeypatch.setenv("LINKUMA_API_KEY", "lkm_single_" + "x" * 32)
    alias, key = accounts.resolve_account(None)
    assert alias == "default"
    assert key.startswith("lkm_single_")


def test_list_accounts_single():
    # Conftest already sets LINKUMA_API_KEY
    assert accounts.list_accounts() == ["default"]


def test_multi_account_first_alias_when_none(monkeypatch):
    monkeypatch.setenv(
        "LINKUMA_API_KEYS_JSON",
        '{"perso":"lkm_perso_aaaaaaaaaaaaaaaaaaa","client1":"lkm_c1_bbbbbbbbbbbbbbbb"}',
    )
    alias, key = accounts.resolve_account(None)
    assert alias == "perso"
    assert key.startswith("lkm_perso_")


def test_multi_account_explicit_alias(monkeypatch):
    monkeypatch.setenv(
        "LINKUMA_API_KEYS_JSON",
        '{"perso":"lkm_perso_aaaaaaaaaaaaa","client1":"lkm_c1_bbbbbbbbbbb"}',
    )
    alias, key = accounts.resolve_account("client1")
    assert alias == "client1"
    assert key.startswith("lkm_c1_")


def test_unknown_alias_raises(monkeypatch):
    monkeypatch.setenv(
        "LINKUMA_API_KEYS_JSON",
        '{"perso":"lkm_perso_aaaaaaaaaaaaa"}',
    )
    with pytest.raises(LinkumaConfigError):
        accounts.resolve_account("nope")


def test_no_keys_raises(monkeypatch):
    monkeypatch.delenv("LINKUMA_API_KEY", raising=False)
    monkeypatch.delenv("LINKUMA_API_KEYS_JSON", raising=False)
    with pytest.raises(LinkumaConfigError):
        accounts.resolve_account(None)


def test_malformed_json_raises(monkeypatch):
    monkeypatch.setenv("LINKUMA_API_KEYS_JSON", "{not-json}")
    with pytest.raises(LinkumaConfigError):
        accounts.resolve_account(None)


def test_empty_map_raises(monkeypatch):
    monkeypatch.setenv("LINKUMA_API_KEYS_JSON", "{}")
    with pytest.raises(LinkumaConfigError):
        accounts.resolve_account(None)


def test_get_client_returns_cached_instance(monkeypatch):
    monkeypatch.setenv(
        "LINKUMA_API_KEYS_JSON",
        '{"a":"lkm_aaaaaaaaaaaaaaaaaaaaaaaaaa","b":"lkm_bbbbbbbbbbbbbbbbbbbbbbbb"}',
    )
    c1 = accounts.get_client("a")
    c2 = accounts.get_client("a")
    assert c1 is c2
    c3 = accounts.get_client("b")
    assert c3 is not c1
