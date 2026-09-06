"""乘車證明的**原始檔**：存得下、看得到、別人拿不到、刪掉就不見。

## 由來

使用者要求（2026-09-06）：「上傳過的乘車證明留下來，這裡可以有一個按鈕點下去
看原車票證明；如果日後這邊有清空，就一起清理。」

**存 PDF 不存 PNG**：PNG 本來就是從 PDF 轉的，留 PDF 更小、不失真，
也省掉上傳時的算圖成本（使用者當場改的決定）。

## 歸屬怎麼保證

檔案放在 `<data_dir>/transit_proof_files/<使用者雜湊>/`，而那個雜湊是從
**當前登入者**算出來的、不吃任何請求參數 —— A 拿著 B 的 entry_id 只會在
自己的目錄裡找不到。**歸屬由路徑結構決定**比事後檢查 ACL 難寫錯
（本專案在 fail-open 的 ACL 上吃過虧）。
"""
from __future__ import annotations

import pytest

from app.tools.transit_proof import buffer


def _entry(no: str = "N123"):
    return {"transport": "台鐵", "date": "2026-09-06", "origin": "臺北",
            "destination": "臺中", "fare": 375, "ticket_no": no}


def test_the_original_is_kept_and_can_be_read_back(tmp_path, monkeypatch):
    monkeypatch.setattr(buffer, "_files_dir", lambda u: tmp_path / "files")
    monkeypatch.setattr(buffer, "_buffer_path", lambda u: tmp_path / "buf.json")
    r = buffer.add_entries("alice", [_entry()], [b"%PDF-1.4 original"])
    eid = r["added"][0]["id"]
    assert r["added"][0].get("has_file") is True, "沒有標記成有原件，畫面上就不會出現按鈕"
    got = buffer.entry_file("alice", eid)
    assert got is not None and got.read_bytes() == b"%PDF-1.4 original"


def test_a_duplicate_does_not_store_a_second_copy(tmp_path, monkeypatch):
    """同一張證明重複上傳不該佔第二份空間。"""
    monkeypatch.setattr(buffer, "_files_dir", lambda u: tmp_path / "files")
    monkeypatch.setattr(buffer, "_buffer_path", lambda u: tmp_path / "buf.json")
    buffer.add_entries("alice", [_entry()], [b"%PDF-1.4 a"])
    r2 = buffer.add_entries("alice", [_entry()], [b"%PDF-1.4 a"])
    assert r2["duplicates"] == 1 and not r2["added"]
    assert len(list((tmp_path / "files").iterdir())) == 1


def test_another_user_cannot_reach_it(tmp_path, monkeypatch):
    """**拿著別人的 entry_id 也讀不到** —— 路徑是從自己的身分算出來的。"""
    dirs = {"alice": tmp_path / "a", "bob": tmp_path / "b"}
    monkeypatch.setattr(buffer, "_files_dir", lambda u: dirs[u])
    monkeypatch.setattr(buffer, "_buffer_path",
                        lambda u: tmp_path / f"{u}.json")
    r = buffer.add_entries("alice", [_entry()], [b"%PDF-1.4 secret"])
    eid = r["added"][0]["id"]
    assert buffer.entry_file("alice", eid) is not None
    assert buffer.entry_file("bob", eid) is None, "B 讀到了 A 的原始證明"


@pytest.mark.parametrize("how", ["single", "batch", "clear"])
def test_every_delete_path_also_removes_the_file(tmp_path, monkeypatch, how):
    """**四條刪除路徑都要清檔** —— 漏一條就留下永遠沒人清的孤兒檔，
    而且畫面上看不出來。"""
    monkeypatch.setattr(buffer, "_files_dir", lambda u: tmp_path / "files")
    monkeypatch.setattr(buffer, "_buffer_path", lambda u: tmp_path / "buf.json")
    r = buffer.add_entries("alice", [_entry()], [b"%PDF-1.4 x"])
    eid = r["added"][0]["id"]
    assert buffer.entry_file("alice", eid) is not None

    if how == "single":
        buffer.delete_entry("alice", eid)
    elif how == "batch":
        buffer.delete_entries("alice", [eid])
    else:
        buffer.clear_all("alice")

    assert buffer.entry_file("alice", eid) is None, f"{how} 之後檔案還在"


def test_a_traversal_style_id_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(buffer, "_files_dir", lambda u: tmp_path / "files")
    for bad in ("../../etc/passwd", "a/b", "..", "a\\b"):
        assert buffer.entry_file("alice", bad) is None, bad


def test_a_failed_write_does_not_lose_the_entry(tmp_path, monkeypatch):
    """磁碟寫不下時**清單本身仍要建立** —— 原件只是附加價值，
    不可以因為存不下就讓整批上傳失敗。"""
    class _Boom:
        def mkdir(self, *a, **k): raise OSError("no space")
        def __truediv__(self, other): return self
    monkeypatch.setattr(buffer, "_files_dir", lambda u: _Boom())
    monkeypatch.setattr(buffer, "_buffer_path", lambda u: tmp_path / "buf.json")
    r = buffer.add_entries("alice", [_entry()], [b"%PDF-1.4 x"])
    assert len(r["added"]) == 1
    assert not r["added"][0].get("has_file")
