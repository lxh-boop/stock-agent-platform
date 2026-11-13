"""Regression tests: user credentials live in user_account and never cross users."""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from core.config.user_credentials import decrypt_credential, encrypt_credential
import application.user_credentials_service as service


class FakeAccounts:
    records = {}

    def account_settings(self, user_id):
        if user_id not in self.records:
            raise ValueError("account_not_found_or_disabled")
        return dict(self.records[user_id])

    def save_account_settings(self, user_id, *, config, llm_ciphertext, tushare_ciphertext):
        self.records[user_id] = {
            "config": dict(config),
            "llm_api_key_ciphertext": llm_ciphertext,
            "tushare_token_ciphertext": tushare_ciphertext,
        }


@pytest.fixture()
def accounts(monkeypatch):
    monkeypatch.setenv("STOCK_USER_CREDENTIAL_KEY", Fernet.generate_key().decode("ascii"))
    FakeAccounts.records = {
        user: {"config": {}, "llm_api_key_ciphertext": "", "tushare_token_ciphertext": ""}
        for user in ("alice", "bob")
    }
    monkeypatch.setattr(service, "AuthRepository", FakeAccounts)
    return FakeAccounts.records


def test_user_tokens_are_encrypted_and_isolated(accounts):
    service.update_user_settings(
        "alice", config={"llm_mode": "api", "llm_api_model": "test-model"},
        llm_key="alice-key", tushare_token="alice-tushare",
    )
    assert "alice-key" not in str(accounts["alice"])
    assert accounts["bob"]["llm_api_key_ciphertext"] == ""
    assert decrypt_credential(accounts["alice"]["llm_api_key_ciphertext"]) == "alice-key"
    assert service.get_user_tushare_token("alice") == "alice-tushare"
    assert service.get_user_llm_settings("alice").credential == "alice-key"
    with pytest.raises(ValueError, match="user_llm_credential_not_configured"):
        service.get_user_llm_settings("bob")
    with pytest.raises(ValueError, match="user_tushare_token_not_configured"):
        service.get_user_tushare_token("bob")
    assert service.public_user_settings("alice")["llm_configured"] is True
    assert "alice-key" not in str(service.public_user_settings("alice"))


def test_clear_key_does_not_affect_other_account(accounts):
    service.update_user_settings("alice", config={}, llm_key="alice-key")
    service.update_user_settings("bob", config={}, llm_key="bob-key")
    service.update_user_settings("alice", config={}, clear_llm=True)
    assert accounts["alice"]["llm_api_key_ciphertext"] == ""
    assert decrypt_credential(accounts["bob"]["llm_api_key_ciphertext"]) == "bob-key"


def test_encryption_fails_closed_without_master_key(monkeypatch):
    monkeypatch.delenv("STOCK_USER_CREDENTIAL_KEY", raising=False)
    with pytest.raises(RuntimeError, match="user_credential_encryption_key_not_configured"):
        encrypt_credential("secret")
