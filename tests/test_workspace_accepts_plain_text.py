"""工作區收純文字（.txt / .md）—— 判準是**內容**，不是副檔名。

## 由來（使用者 2026-09-19：「我的工作區 要允許 .txt .md 等格式上傳」）

工作區其餘每一種型別都有明確的訊號：PDF / PNG 看魔術位元組、Office 檔開 zip
驗內部結構。**純文字兩者都沒有** —— 如果照副檔名收，任何東西改名成 `.txt`
都進得來，而工作區的型別檢查本來就是為了擋這件事
（`detect_kind` 的說明寫著「改名的 zip 不可以冒充文件」）。

所以判準是「內容本身說得通」：整份 UTF-8 解得開、而且沒有 `\t\n\r` 以外的
控制字元。檔名**只在 .txt 與 .md 之間二選一**，永遠不能讓不是文字的東西過關。
"""
from __future__ import annotations

import pytest

from app.core import workspace as ws

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class _State:
    def __init__(self, user):
        self.user = user


class _Req:
    def __init__(self, user):
        self.state = _State(user)


def _user(uid=1):
    return _Req({"user_id": uid, "username": "u", "source": "local"})


@pytest.fixture
def wsenv(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path, raising=False)
    monkeypatch.setattr(ws, "_CACHE", None, raising=False)
    monkeypatch.setattr("app.core.auth_settings.is_enabled", lambda: True)
    ws.save_settings({"enabled": True, "per_user_quota_mb": 500,
                      "max_file_mb": 50, "retention_hours": -1})
    return tmp_path


def test_a_text_file_can_be_saved(wsenv):
    req = _user()
    meta = ws.save_bytes(req, "會議記錄\n第二行\t有 tab\r\n".encode("utf-8"),
                         "記錄.txt", "meeting-summary")
    assert (meta["ext"], meta["mime"]) == (".txt", "text/plain")
    assert meta["name"] == "記錄.txt"
    assert len(ws.list_files(req)) == 1


def test_markdown_is_labelled_by_its_filename(wsenv):
    req = _user()
    md = ws.save_bytes(req, b"# Title\n\n- a\n- b\n", "notes.md", "t")
    assert (md["ext"], md["mime"]) == (".md", "text/markdown")
    # 同樣的內容、換個檔名 → 就是純文字。檔名只在這兩者之間挑。
    txt = ws.save_bytes(req, b"# Title\n\n- a\n- b\n", "notes.txt", "t")
    assert (txt["ext"], txt["mime"]) == (".txt", "text/plain")


def test_the_filename_can_never_let_a_non_text_file_through(wsenv):
    """**這條是整份的重點。**

    只驗「.txt 收得進來」的話，照副檔名收也會過 —— 而那會讓任何檔案改個名
    就繞過工作區的型別檢查。
    """
    req = _user()
    for label, data in (
        ("夾著 NUL 的 ASCII", b"hello\x00world"),
        ("gzip", b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03abc"),
        ("UTF-16（解不開 UTF-8）", "中文".encode("utf-16")),
    ):
        with pytest.raises(ws.UnsupportedType):
            ws.save_bytes(req, data, "evil.txt", "t")

    # 認得出來的型別**不會**因為改名就變成文字：PNG 仍然是 PNG。
    meta = ws.save_bytes(req, PNG_BYTES, "evil.txt", "t")
    assert meta["ext"] == ".png"


def test_the_whole_file_is_checked_not_just_the_first_few_kb():
    """抽樣看前面幾 KB 的話，這種檔案會整個進來。

    前 8 KB 乾淨、後面接二進位 —— 真的做得出來，而且做起來很容易。
    """
    data = b"a" * 8192 + b"\x00\xff\xfe"
    assert ws.detect_kind(data, "x.txt") is None
    assert ws.detect_kind(b"a" * 8192, "x.txt") == ("text/plain", ".txt")


def test_empty_file_is_not_text():
    assert ws.detect_kind(b"", "x.txt") is None


def test_the_front_end_list_includes_the_new_types():
    """前端的「存至工作區」按鈕是照 `data-ws-exts` 決定出不出現的。

    伺服器收得下、前端清單沒有的話，症狀是**按鈕不出現而且沒有任何錯誤訊息**
    （v1.14.6 加試算表 / 簡報時就是這樣漏掉的）。
    這裡驗的是那個清單真的從 `ALLOWED` 算出來，不是另外抄一份。
    """
    from app.main import _tpl_workspace_extensions
    exts = set(_tpl_workspace_extensions().split())
    assert {"txt", "md"} <= exts, exts
    assert exts == {e.lstrip(".") for e in ws.ALLOWED.values()}


def test_text_files_say_they_have_no_preview(wsenv):
    """純文字沒有「第一頁」可以畫 —— 要明講，不要掉進「當成 PDF」那條路。

    掉下去的話它會去找一個不存在的 `file.pdf`，錯誤訊息變成「檔案不存在」，
    看起來像使用者的檔案掉了（其實好好的）。
    """
    req = _user()
    meta = ws.save_bytes(req, b"hello\n", "a.txt", "t")
    with pytest.raises(ws.WorkspaceError) as e:
        ws.get_thumbnail(req, meta["file_id"])
    assert not isinstance(e.value, ws.NotFound), (
        "純文字的縮圖報成「檔案不存在」—— 那會讓使用者以為檔案掉了"
    )


def test_the_upload_accept_list_is_not_hardcoded_in_the_template():
    """`<input accept>` 的清單要由伺服器端給，樣板不可以自己抄一份。

    原本 `my_workspace.html` 兩處各寫死了
    `application/pdf,image/png,.docx,…` —— 症狀是**檔案選擇器把收得下的檔案
    濾掉**，使用者只會覺得「這個檔不能傳」，沒有任何錯誤訊息可循。
    這跟 v1.14.6 的 `data-ws-exts` 是同一個家族（同一份清單寫在兩個地方）。
    """
    import pathlib
    from app.main import _tpl_workspace_accept

    tpl = (pathlib.Path(__file__).resolve().parent.parent
           / "app" / "web" / "templates" / "my_workspace.html")
    body = tpl.read_text(encoding="utf-8")
    assert body.count("workspace_accept()") >= 2, (
        "my_workspace.html 的 accept 沒有走伺服器端的清單"
    )
    for bad in ("application/pdf,", ".docx,.xlsx", ".odt,.ods"):
        assert bad not in body, f"樣板裡又寫死了一份清單：{bad}"

    accept = _tpl_workspace_accept()
    assert ".txt" in accept and ".md" in accept, accept
    assert set(accept.split(",")) == set(ws.ALLOWED.values())
