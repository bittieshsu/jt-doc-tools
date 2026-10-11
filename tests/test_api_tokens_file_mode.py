"""API Token 檔只給服務帳號讀：0600。

原本寫檔沒有指定權限，照 umask 是 0644，而當時檔案裡是 Token 原文，同一台機器的其他帳號讀得到每一張
（2026-10-09 為合規說明逐項查證時發現）。新寫的檔案 0600；舊安裝留下的 0644 在服務啟動時收緊。
v1.16.78 起檔案裡只有雜湊與密文（`test_api_tokens_encrypted_at_rest.py`），權限照樣收緊。
"""
from __future__ import annotations

import os
import stat

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Windows 沒有 POSIX 權限位元")


def _mode(p) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_the_token_file_is_written_owner_only(tmp_path, monkeypatch):
    from app.config import settings
    from app.core import api_tokens
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    m = api_tokens.ApiTokenManager()
    path = tmp_path / "api_tokens.json"
    assert path.exists() and _mode(path) == 0o600, oct(_mode(path))
    m.create("second")
    assert _mode(path) == 0o600, oct(_mode(path))


def test_an_old_world_readable_file_is_tightened(tmp_path, monkeypatch):
    from app.config import settings
    from app.core import api_tokens
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    api_tokens.ApiTokenManager()
    path = tmp_path / "api_tokens.json"
    os.chmod(path, 0o644)
    api_tokens.ApiTokenManager()
    assert _mode(path) == 0o600, oct(_mode(path))
