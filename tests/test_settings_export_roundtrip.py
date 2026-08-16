"""設定備份：**匯出的檔案要匯得回去**。

## 由來

v1.14.31 的對抗式驗證以合法管理員身分照著介面操作，發現整組功能是死的，
而且是**兩層**壞掉：

1. 匯出 / 預覽 / 匯入三個端點都 `NameError` 變成 500 —— `app/admin/router.py`
   用了 `settings.temp_dir`，但那個模組從來沒有 import 過 `settings`
   （檔案裡別處寫的是函式內 `import ... as _s`，所以 grep 得到字串卻沒有
   這個名字）。頁面本身打得開，使用者只會以為是暫時性錯誤。
2. 修好之後**還是匯不回去**：`rbac.json` 放在 zip 的根目錄，而「全部還原」
   那條分支把它跟一般檔案一起丟進 `relative_to("data")` → `ValueError`。
   另外兩條分支都正確排除了它，只有這條漏掉。

## 判準

**跑完整的往返**。只測「匯出有沒有回 200」抓不到第 2 層 —— 那是匯入才炸的。
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("JTDT_CSRF_DISABLE", "1")


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    """**要自己的資料目錄** —— 這份測試會真的把設定「匯入」回去。

    匯入是寫檔動作（還會在旁邊留 `.bak.<時間>` 備份），直接寫進 conftest
    的共用測試資料目錄會蓋掉別的測試依賴的檔案。這種污染只在特定的檔案
    順序下才顯形（同一輪就有一個 `auth_settings._CACHE` 沒還原的例子，
    害三個不相干的測試變紅）。
    """
    from fastapi.testclient import TestClient

    from app.config import settings

    tmp = tmp_path_factory.mktemp("settings_export")
    saved = settings.data_dir
    settings.data_dir = tmp
    # **換了 data_dir 就要自己把子目錄建起來**。`temp_dir` / `jobs_dir` 都是
    # 從 `data_dir` 衍生的屬性，而建目錄的 `ensure_dirs()` 在**啟動時**就跑
    # 過了、用的是舊路徑 —— 不補這一步，暫存那份匯入用的 zip 會寫到一個不
    # 存在的目錄，preview 讀回來就是壞的（實測：小組合裡剛好過、完整套件裡紅）。
    settings.ensure_dirs()
    # **要自己種一個載荷檔**。乾淨的資料目錄匯出來只有 `rbac.json`，
    # 於是「匯入了幾個檔」是 0 —— 那是正確行為，但測試就驗不到「檔案真的
    # 被寫回去」這件事了（實測：完整套件裡因為這個原因紅，單獨跑卻是綠的，
    # 因為單獨跑時啟動流程會把預設檔建起來）。
    (tmp / "profile.json").write_text('{"company_name": "測試工具箱"}',
                                      encoding="utf-8")
    try:
        from app.main import app
        # **一定要用 context manager** —— 啟動流程要跑過，資料表才建得起來。
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        settings.data_dir = saved


def test_settings_export_download_works(client):
    """匯出本身不可以 500（原本是 NameError）。"""
    r = client.post("/admin/settings-export/download",
                    data={"categories": "assets"})
    assert r.status_code == 200, r.text[:200]
    assert r.content[:2] == b"PK", "回傳的不是 zip"


def test_settings_export_roundtrip(client):
    """匯出 → 預覽 → 匯入，三步都要成功。

    這是唯一能抓到「自己匯出的檔自己匯不回去」的測法。
    """
    import io
    import zipfile

    r = client.post("/admin/settings-export/download",
                    data={"categories": "assets"})
    assert r.status_code == 200, r.text[:200]
    blob = r.content

    names = zipfile.ZipFile(io.BytesIO(blob)).namelist()
    assert "rbac.json" in names, (
        "前提變了：rbac.json 不在 zip 根目錄，這個測試要重寫")

    r2 = client.post("/admin/settings-export/preview",
                     files={"file": ("s.zip", blob, "application/zip")})
    assert r2.status_code == 200, r2.text[:200]
    token = (r2.json() or {}).get("token")
    assert token, f"預覽沒有回 token：{r2.text[:200]}"

    r3 = client.post("/admin/settings-export/import", json={"token": token})
    assert r3.status_code == 200, (
        f"匯入失敗（{r3.status_code}）—— 自己匯出的檔自己匯不回去：{r3.text[:200]}")
    assert (r3.json() or {}).get("imported_files", 0) > 0, (
        f"沒有任何檔案被匯入 —— 匯出的 zip 裡應該至少有一個載荷檔："
        f"{names}\n{r3.text[:200]}")


def test_admin_router_has_settings_at_module_level():
    """靜態把關：`app/admin/router.py` 要在**模組層**有 `settings`。

    函式內 `from ..config import settings as _s` 不算 —— 那個名字是 `_s`。
    這條之所以要用靜態檢查：那三個端點只有在管理員真的去按的時候才會執行到，
    平常的煙霧測試碰不到。
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    tree = ast.parse((root / "app" / "admin" / "router.py").read_text(encoding="utf-8"))
    names = set()
    for node in tree.body:            # 只看模組層
        if isinstance(node, ast.ImportFrom):
            names |= {a.asname or a.name for a in node.names}
        elif isinstance(node, ast.Import):
            names |= {a.asname or a.name.split(".")[0] for a in node.names}
    assert "settings" in names, (
        "`app/admin/router.py` 的模組層沒有 `settings` —— "
        "設定匯出 / 匯入會 NameError 變成 500")


def test_import_rejects_a_corrupt_json_in_the_backup(client):
    """備份檔裡有壞掉的 JSON → 整批匯入要失敗，不可以讓它落地。

    原本是原樣寫入不做驗證，於是一份不完整的備份可以把
    `auth_settings.json` 換成壞掉的內容 —— 而壞掉的認證設定曾經等於
    **全站認證關閉**（見 `tests/test_auth_settings_fail_secure.py`）。
    讀取端現在會 fail-secure，但讓壞資料落地本身就不該發生。
    """
    import io
    import zipfile

    r = client.post("/admin/settings-export/download",
                    data={"categories": "assets"})
    assert r.status_code == 200

    src = zipfile.ZipFile(io.BytesIO(r.content))
    # **要弄壞的是「載荷」，不是清單檔**。`manifest.json` 與 `rbac.json` 都是
    # 匯入流程自己要先讀的檔 —— 弄壞它們的話 preview 就先失敗了，測不到
    # 「寫檔前有沒有驗 JSON」這件事，而且會讓同檔的往返測試跟著紅
    # （實測：資料目錄乾淨時 zip 只剩這兩個檔，第一版的挑法正好挑中 manifest）。
    #
    # **而且要自己塞一個進去**，不要挑現成的 —— 挑現成的會讓這個測試在
    # 「剛好沒有載荷檔」的環境下跳過，那個守衛就等於沒被驗到。
    victim = "data/profile.json"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        seen = False
        for n in src.namelist():
            if n == victim:
                seen = True
                z.writestr(n, b"{oops")
            else:
                z.writestr(n, src.read(n))
        if not seen:
            z.writestr(victim, b"{oops")

    r2 = client.post("/admin/settings-export/preview",
                     files={"file": ("s.zip", buf.getvalue(), "application/zip")})
    assert r2.status_code == 200, r2.text[:200]
    r3 = client.post("/admin/settings-export/import",
                     json={"token": (r2.json() or {}).get("token")})
    assert 400 <= r3.status_code < 500, (
        f"壞掉的備份被收下了（{r3.status_code}）—— 那份 JSON 已經寫進 data/")
    # 而且不可以留在磁碟上
    from app.config import settings as _s
    landed = _s.data_dir / "profile.json"
    if landed.exists():
        assert landed.read_bytes() != b"{oops", "壞掉的內容已經落地了"
