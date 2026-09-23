"""設定檔一律原子寫入（`app/core/atomic_json.py`），不可以直接覆寫。

## 為什麼要有這一份

直接覆寫時，寫到一半斷電 / 行程被殺就留下一個**截斷的檔案**，而每一支
`load()` 都是「剖析失敗就回預設值」—— 管理員建的資料安靜地變成空的，
畫面上看不出異常、也沒有錯誤訊息。認證設定那一支更嚴重：v1.14.31 的對抗式
驗證抓到 **0 bytes 的認證設定曾經等於「認證關閉」**。

v1.15.20 為翻譯對照字典先補了這件事，當時記下「全站還有六支不是原子寫入」。
六支補完之後又實算一次，發現**還有五支從來沒被列進那份清單**
（`auth_settings` / `asset_manager` / `ocr_engine` / `llm_model_profile` /
`workspace` 的檔案中繼資料）—— 所以這件事不能靠清單，要靠檢查。

## 判準

掃 AST 的 **Call 節點**，不掃字串也不掃註解。這個專案被自己寫的註解騙過
不只一次（v1.15.14 的 zip 炸彈檢查、v1.15.16 的 root chown 檢查），
所以這裡刻意用 `ast`：

- `<expr>.write_text(json.dumps(...))` → 直接覆寫，紅。
- `json.dump(obj, fh)` → 同樣是直接寫檔，紅。

例外要寫進 `_EXEMPT` 並**說明為什麼**。
"""
from __future__ import annotations

import ast
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "app"

#: 例外：**相對於專案根的 posix 路徑** → 為什麼可以不走 helper。
#:
#: 用 `.as_posix()` 當鍵是這個專案踩出來的規則 —— Windows 上
#: `str(p.relative_to(ROOT))` 給的是反斜線，豁免清單就整份對不上，
#: 於是該跳過的檔案全變成假陽性（v1.15.7 一次抓到 21 條）。
_EXEMPT: dict[str, str] = {
    "app/core/atomic_json.py": "它自己就是那支 helper",
    "app/core/vat_db.py": (
        "統編匯入的**進度檔**，每幾千筆寫一次；內容自己會被下一次寫蓋掉，"
        "而且政府公開資料排程會重新下載 —— 這裡 fsync 只是白付 I/O。"
        "它本來就已經是 tmp + replace（不會留下截斷檔）。"),
    "app/core/audit_forward.py": (
        "轉發書籤（下一筆要送的 id），每批寫一次；掉了就從舊書籤重送，"
        "重複送一筆遠比多付 fsync 划算。同樣已經是 tmp + replace。"),
    "app/tools/translate_doc/router.py": (
        "逐句翻譯的**進度檔**，一份文件會寫幾百次（每翻完一段就更新畫面）。"
        "已經是 tmp + replace；掉的是進度不是結果。"),
}


def _scanned_files() -> list[pathlib.Path]:
    return sorted(p for p in APP.rglob("*.py") if p.name != "__init__.py")


def _rel(p: pathlib.Path) -> str:
    return p.relative_to(ROOT).as_posix()


def _direct_writes(tree: ast.AST) -> list[tuple[int, str]]:
    """回 [(行號, 形狀)] —— 直接把 JSON 覆寫進檔案的呼叫。"""
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr == "write_text":
            for a in list(node.args) + [k.value for k in node.keywords]:
                if any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                       and n.func.attr == "dumps"
                       and isinstance(n.func.value, ast.Name)
                       and n.func.value.id == "json"
                       for n in ast.walk(a)):
                    out.append((node.lineno, "write_text(json.dumps(...))"))
                    break
        elif (isinstance(f, ast.Attribute) and f.attr == "dump"
              and isinstance(f.value, ast.Name) and f.value.id == "json"):
            out.append((node.lineno, "json.dump(...)"))
    return out


@pytest.mark.parametrize("path", _scanned_files(), ids=_rel)
def test_module_writes_json_atomically(path: pathlib.Path):
    if _rel(path) in _EXEMPT:
        pytest.skip(f"例外：{_EXEMPT[_rel(path)]}")
    hits = _direct_writes(ast.parse(path.read_text(encoding="utf-8")))
    assert not hits, (
        f"{_rel(path)} 直接把 JSON 覆寫進檔案："
        + "、".join(f"第 {ln} 行 {shape}" for ln, shape in hits)
        + "\n改走 `app/core/atomic_json.py` 的 write_json() —— 直接覆寫時"
          "寫到一半被中斷會留下截斷檔，而讀取端一律「剖析失敗就回預設值」，"
          "資料就這樣安靜地不見了。真的有理由例外請寫進這支測試的 _EXEMPT。")


def test_the_scan_actually_sees_the_shape_it_is_looking_for():
    """**檢查自己要有牙齒。** 逐檔參數化最常見的失敗是「一個都沒掃到」——
    那時候 `assert not hits` 永遠成立，輸出看起來跟全部通過一樣。"""
    assert len(_scanned_files()) > 200, "掃到的檔案數不對，比對基準本身就壞了"
    sample = ast.parse(
        "import json\n"
        "p.write_text(json.dumps({'a': 1}), encoding='utf-8')\n"
        "with open(f) as fh:\n"
        "    json.dump({'b': 2}, fh)\n"
        "# 註解裡寫 p.write_text(json.dumps(x)) 不算\n"
        "s = 'p.write_text(json.dumps(x))'\n")
    hits = _direct_writes(sample)
    assert len(hits) == 2, f"應該剛好抓到兩個形狀，實際 {hits}"


def test_comments_and_strings_do_not_count_as_a_violation():
    """註解 / 字串裡提到那個寫法不可以誤報 —— 本專案為此紅過兩次假警報。"""
    src = ("# p.write_text(json.dumps(d))\n"
           "DOC = '''改法：不要 p.write_text(json.dumps(d))'''\n")
    assert _direct_writes(ast.parse(src)) == []


# ---------------------------------------------------------------- helper 行為

def test_no_temp_file_is_left_behind_when_the_write_fails(tmp_path, monkeypatch):
    """失敗要把暫存檔清掉，而且**原本的檔案一個位元都不能動**。"""
    from app.core import atomic_json

    target = tmp_path / "s.json"
    target.write_text('{"keep": true}', encoding="utf-8")

    import os as _os
    real = _os.replace

    def boom(a, b):
        raise OSError("模擬換檔失敗")

    monkeypatch.setattr(_os, "replace", boom)
    with pytest.raises(OSError):
        atomic_json.write_json(target, {"new": 1})
    monkeypatch.setattr(_os, "replace", real)

    assert target.read_text(encoding="utf-8") == '{"keep": true}'
    assert [p.name for p in tmp_path.iterdir()] == ["s.json"], \
        "失敗之後資料目錄裡不可以留著 *.tmp"


def test_permission_is_set_before_the_rename(tmp_path):
    """含密鑰的檔案不可以有「一瞬間是 0644」的窗口 —— 權限要設在暫存檔上。

    `ocr_remote_settings` 原本是 replace 之後才 chmod，就有那個窗口。

    **判準是呼叫順序，不是最後的權限位元。** Windows 沒有 POSIX 權限，
    `os.chmod` 只能切換唯讀旗標、`st_mode` 讀回來是 0o666 —— 第一版驗
    `st_mode == 0o600`，在 Linux 綠、在 `.154` 上紅（v1.15.34 實測）。
    驗順序在兩個平台都成立，而且守的正是那個「有沒有窗口」的問題。
    """
    import os as _os

    from app.core import atomic_json

    order: list[str] = []
    real_chmod, real_replace = _os.chmod, _os.replace

    def spy_chmod(path, mode, *a, **kw):
        order.append("chmod")
        return real_chmod(path, mode, *a, **kw)

    def spy_replace(a, b):
        order.append("replace")
        return real_replace(a, b)

    target = tmp_path / "t.json"
    _os.chmod, _os.replace = spy_chmod, spy_replace
    try:
        atomic_json.write_json(target, {"token": "x"}, mode=0o600)
    finally:
        _os.chmod, _os.replace = real_chmod, real_replace

    assert order == ["chmod", "replace"], (
        f"權限必須在換檔**之前**設好，實際順序：{order}")
    if not sys.platform.startswith("win"):
        assert target.stat().st_mode & 0o777 == 0o600


def test_two_writers_do_not_share_a_temp_file(tmp_path):
    """暫存檔名要獨一無二 —— 固定名稱時兩個同時在寫的人會互相踩。

    這不是理論問題：`jtdt` 的救援指令（`auth disable` / `reset-password`）
    會在服務還跑著的時候寫同一份 `auth_settings.json`。共用暫存檔的話，
    A 寫一半、B 截斷同一個檔、A 接著 rename → 換過去的是半份內容，
    **正好是原子寫入要防的事**。
    """
    from app.core import atomic_json

    names: list[str] = []
    target = tmp_path / "s.json"

    import os as _os
    real = _os.replace

    def spy(a, b):
        names.append(pathlib.Path(a).name)
        return real(a, b)

    _os.replace = spy
    try:
        atomic_json.write_json(target, {"n": 1})
        atomic_json.write_json(target, {"n": 2})
    finally:
        _os.replace = real
    assert len(set(names)) == 2, f"兩次寫入用了同一個暫存檔名：{names}"
    assert all(n.startswith("s.json.tmp") for n in names), names


def test_a_half_written_temp_file_never_becomes_the_target(tmp_path):
    """**行為層**：模擬「另一個寫入者把暫存檔截斷」，換過去的內容仍要完整。

    只驗檔名不同是結構層的判準；這一條驗的是結果 —— 目標檔永遠是某一次
    完整的內容，不會是兩次混在一起的東西。
    """
    import json as _json

    from app.core import atomic_json

    target = tmp_path / "s.json"
    big = {"payload": "x" * 5000}
    atomic_json.write_json(target, big)

    # 第二個寫入者用舊的固定命名規則搶同一個暫存檔並寫進垃圾
    intruder = tmp_path / "s.json.tmp"
    intruder.write_text("{ 這不是完整的 JSON", encoding="utf-8")

    atomic_json.write_json(target, {"payload": "y" * 5000})
    assert _json.loads(target.read_text(encoding="utf-8"))["payload"][0] == "y"
    assert intruder.exists(), "不可以去動別人的暫存檔"


def test_the_temp_file_lives_next_to_the_target(tmp_path):
    """暫存檔必須跟目標同一個目錄 —— 跨檔案系統 `os.replace` 會丟 OSError，
    而資料目錄常常是獨立掛載點。"""
    from app.core import atomic_json

    target = tmp_path / "sub" / "t.json"
    seen: list[str] = []

    import os as _os
    real = _os.replace

    def spy(a, b):
        seen.append(str(pathlib.Path(a).parent))
        return real(a, b)

    _os.replace = spy
    try:
        atomic_json.write_json(target, {"a": 1})
    finally:
        _os.replace = real
    assert seen == [str(target.parent)]


def test_every_exemption_still_points_at_a_real_file():
    """例外清單會過期 —— 檔案改名之後那一條就靜靜地不再豁免任何東西，
    而它看起來還是「有在維護」。"""
    missing = [k for k in _EXEMPT if not (ROOT / k).exists()]
    assert not missing, f"_EXEMPT 列的檔案已不存在：{missing}"


def test_exemptions_are_actually_needed():
    """反向：被豁免的檔案如果早就改乾淨了，那一條要拿掉。

    留著沒必要的豁免比沒有豁免更糟 —— 下一個人以為那支還不能用 helper。
    """
    stale = []
    for k in _EXEMPT:
        if k.endswith("atomic_json.py"):
            continue
        if not _direct_writes(ast.parse((ROOT / k).read_text(encoding="utf-8"))):
            stale.append(k)
    assert not stale, f"這些檔案已經沒有直接寫了，_EXEMPT 可以拿掉：{stale}"
