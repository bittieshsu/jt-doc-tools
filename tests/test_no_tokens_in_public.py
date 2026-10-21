"""公開樹裡不可以有存取權杖（GitHub token 等）—— 推送前 `tools/check_private_names.py` 會擋。

權杖檢查不需要私有名單，所以這支在公開的 clone 上也照常跑（名單那一支在公開版會整支略過）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import check_private_names as cpn            # noqa: E402
from tools.repo_paths import is_development_tree, public_root  # noqa: E402



#: 拼出來的假權杖（直接寫成一整串，這個測試檔自己就會被掃到）
_FAKE_PAT = "github" + "_pat_" + "11AAAAAAA0" + "x" * 70
_FAKE_GHP = "gh" + "p_" + "A1b2C3d4" * 5


@pytest.mark.parametrize("tok", [_FAKE_PAT, _FAKE_GHP])
def test_a_planted_token_stops_the_publish_even_without_the_name_list(tmp_path, monkeypatch, tok, capsys):
    """權杖檢查不靠名單：公開的 clone 上沒有名單，也要擋得下來；輸出不印權杖本身。"""
    (tmp_path / "notes.md").write_text(f"token = {tok}\n", encoding="utf-8")
    monkeypatch.setattr(cpn, "NAMES_FILE", tmp_path / "missing.txt")
    monkeypatch.setattr(cpn, "HOSTS_FILE", tmp_path / "missing2.txt")
    monkeypatch.setattr(cpn, "load_patterns", lambda path=None: [])
    assert cpn.main([str(tmp_path)]) == 1
    out = capsys.readouterr()
    assert "notes.md:1" in out.out and tok not in out.out + out.err


def test_ordinary_text_about_tokens_is_not_a_token(tmp_path):
    (tmp_path / "doc.md").write_text("在 GitHub 建立 github_pat_ 開頭的細粒度權杖；ghp_ 是傳統權杖。\n", encoding="utf-8")
    assert cpn.scan([str(tmp_path)], cpn.LEAK_FORMATS) == []


def test_no_token_is_in_what_gets_published():
    roots = [public_root(ROOT)] if not is_development_tree(ROOT) else list(cpn.DEFAULT_ROOTS)
    assert cpn.scan(roots, cpn.LEAK_FORMATS) == []
