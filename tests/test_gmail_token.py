import json
from pathlib import Path

import pytest

from app import config
from app.gmail_client import (
    gmail_token_present,
    host_setup,
    load_credentials,
    read_gmail_token_text,
    token_missing_message,
)


@pytest.fixture()
def fresh_config():
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def test_token_from_inline_json_env(monkeypatch, fresh_config):
    monkeypatch.setenv("GMAIL_TOKEN_JSON", '{"refresh_token": "abc", "client_id": "x"}')
    text = read_gmail_token_text(config.get_settings())
    assert json.loads(text)["refresh_token"] == "abc"
    assert gmail_token_present(config.get_settings())


def test_token_from_path_in_env(monkeypatch, tmp_path, fresh_config):
    token_file = tmp_path / "token.json"
    token_file.write_text('{"refresh_token": "from-file"}', encoding="utf-8")
    monkeypatch.setenv("GMAIL_TOKEN_JSON", str(token_file))
    text = read_gmail_token_text(config.get_settings())
    assert json.loads(text)["refresh_token"] == "from-file"


def test_token_from_directory_mount(monkeypatch, tmp_path, fresh_config):
    mount = tmp_path / "gmail-token"
    mount.mkdir()
    (mount / "token.json").write_text('{"refresh_token": "mounted"}', encoding="utf-8")
    monkeypatch.setenv("GMAIL_TOKEN_JSON", str(mount))
    text = read_gmail_token_text(config.get_settings())
    assert json.loads(text)["refresh_token"] == "mounted"


def test_missing_token_names_the_github_copy(monkeypatch, fresh_config):
    monkeypatch.delenv("GMAIL_TOKEN_JSON", raising=False)
    monkeypatch.setenv("GMAIL_TOKEN_FILE", "secrets/does-not-exist.json")
    monkeypatch.setenv("K_SERVICE", "inbox-job-agent-git")
    message = token_missing_message()
    assert "inbox-job-agent-git" in message
    assert "us-east1" in message
    with pytest.raises(RuntimeError, match="inbox-job-agent-git"):
        load_credentials(config.get_settings())


def test_host_setup_warns_only_on_cloud_without_token(monkeypatch, fresh_config):
    monkeypatch.delenv("GMAIL_TOKEN_JSON", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)
    local = host_setup(config.get_settings())
    assert local["on_cloud"] is False
    assert local["warning"] == ""

    monkeypatch.setenv("K_SERVICE", "inbox-job-agent-git")
    cloud = host_setup(config.get_settings())
    assert cloud["wrong_service"] is True
    assert cloud["gmail_ok"] is False
    assert "inbox-job-agent-git" in cloud["warning"]
