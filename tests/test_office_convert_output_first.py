"""soffice 的離開碼不可靠 —— 判準是「有沒有拿到可用的檔案」。

## 由來（v1.15.34）

`convert_with_filter` 的註解從 v1.14.46 就寫著這件事：

> soffice 可能一邊印無關的警告（javaldx 找不到 Java）一邊正常轉完，也可能在
> 收尾階段才死掉。真正的判準是「有沒有拿到一份可用的檔案」；反過來先看回傳碼
> 的話，會把已經轉好的檔案白白丟掉。

**但只有那一支照做。** 另外六支（PDF / odg / docx / pptx / odt / 文字）都是
先看 `returncode != 0` 就丟例外。用一支「產出檔照寫、離開碼回 1」的假 soffice
實測：六支全部丟例外，而那份轉好的檔案就在暫存目錄裡被一起刪掉 ——
使用者看到的是「轉檔失敗」，訊息內容還是那句無關的 Java 警告，
**於是他會跑去裝 Java**。

## 為什麼用假的 soffice

要驗的正是「**離開碼與產出不一致**」這個組合，真的 soffice 不會照著演。
假的那支只做兩件事：把產出檔寫出來、回非零離開碼。

Windows 上跑不了 `.sh`，那裡誠實 skip（不是 fail）—— 這條驗的是我們自己的
判準，平台無關。
"""
from __future__ import annotations

import io
import pathlib
import sys

import pytest

from app.core import office_convert as oc

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="假 soffice 是 POSIX shell script；這條驗的是判準，與平台無關")

_FAKE = r"""#!/bin/sh
outdir=""; target=""; fmt=""
while [ $# -gt 0 ]; do
  case "$1" in
    --outdir) outdir="$2"; shift 2;;
    --convert-to) fmt=$(printf %s "$2" | cut -d: -f1); shift 2;;
    -*) shift;;
    *) target="$1"; shift;;
  esac
done
base=$(basename "$target"); stem=${base%.*}
__BODY__
echo "Warning: failed to launch javaldx - java may not function correctly" >&2
exit __RC__
"""

_GOOD_BODY = 'printf "%%PDF-1.4\\n%%%%EOF\\n" > "$outdir/$stem.$fmt"'
_EMPTY_BODY = ': > "$outdir/$stem.$fmt"'
_NOTHING_BODY = ':'


def _fake(tmp_path: pathlib.Path, body: str, rc: str) -> str:
    p = tmp_path / "fake_soffice.sh"
    p.write_text(_FAKE.replace("__BODY__", body).replace("__RC__", rc),
                 encoding="utf-8")
    p.chmod(0o755)
    return str(p)


@pytest.fixture
def src(tmp_path):
    from docx import Document
    f = tmp_path / "a.docx"
    d = Document(); d.add_paragraph("output-first 測試"); d.save(f)
    return f


def _patch(monkeypatch, soffice: str):
    monkeypatch.setattr(oc, "find_soffice", lambda: soffice)
    monkeypatch.setattr(oc, "ensure_readable", lambda p: None)


_CONVERTERS = [
    ("convert_to_pdf", "pdf"),
    ("convert_to_odt", "odt"),
    ("convert_to_docx", "docx"),
    ("convert_to_pptx", "pptx"),
    ("convert_to_odg", "odg"),
]


@pytest.mark.parametrize("fn_name,ext", _CONVERTERS)
def test_a_usable_output_is_kept_even_when_soffice_exits_nonzero(
        tmp_path, src, monkeypatch, fn_name, ext):
    """離開碼 1 但檔案好端端在 → 要採用它，不可以丟掉。"""
    _patch(monkeypatch, _fake(tmp_path, _GOOD_BODY, "1"))
    out = tmp_path / f"out.{ext}"
    getattr(oc, fn_name)(src, out)
    assert out.exists() and out.stat().st_size > 0, (
        f"{fn_name} 把已經轉好的檔案丟掉了")


def test_convert_to_text_also_keeps_it(tmp_path, src, monkeypatch):
    _patch(monkeypatch, _fake(tmp_path, _GOOD_BODY, "1"))
    assert oc.convert_to_text(src).strip(), "convert_to_text 把轉好的內容丟掉了"


@pytest.mark.parametrize("fn_name,ext", _CONVERTERS)
def test_no_output_still_fails(tmp_path, src, monkeypatch, fn_name, ext):
    """**反向對照**：只驗「非零也會成功」的話，把檢查整段拿掉也會過。

    真的沒有產出時必須還是丟 `RuntimeError`。
    """
    _patch(monkeypatch, _fake(tmp_path, _NOTHING_BODY, "0"))
    with pytest.raises(RuntimeError):
        getattr(oc, fn_name)(src, tmp_path / f"out.{ext}")


def test_an_empty_output_is_not_accepted(tmp_path, src, monkeypatch):
    """0 bytes 的產出不算「拿到可用的檔案」—— 那是無聲失敗的樣態之一。"""
    _patch(monkeypatch, _fake(tmp_path, _EMPTY_BODY, "0"))
    with pytest.raises(RuntimeError) as e:
        oc.convert_to_pdf(src, tmp_path / "out.pdf")
    assert "空檔案" in str(e.value), f"訊息沒說清楚是空檔案：{e.value}"


def test_a_killed_process_says_so_instead_of_blaming_the_file(
        tmp_path, src, monkeypatch):
    """被訊號中止（實測 137 = SIGKILL）多半是記憶體 / 併行太多 ——
    訊息不可以指向來源檔，那會讓使用者一直換檔案試。"""
    _patch(monkeypatch, _fake(tmp_path, _NOTHING_BODY, "137"))
    with pytest.raises(RuntimeError) as e:
        oc.convert_to_pdf(src, tmp_path / "out.pdf")
    msg = str(e.value)
    assert "訊號" in msg and "記憶體" in msg, f"訊息沒指出真正的方向：{msg}"


def test_the_fake_really_reproduces_the_old_behaviour(tmp_path, src, monkeypatch):
    """**檢查自己要有牙齒。** 這支假 soffice 必須真的造出「非零 ＋ 有產出」
    這個組合 —— 否則上面那幾條就是在驗一個不存在的情境。
    """
    import subprocess
    soffice = _fake(tmp_path, _GOOD_BODY, "1")
    outdir = tmp_path / "probe"
    outdir.mkdir()
    r = subprocess.run([soffice, "--convert-to", "pdf", "--outdir", str(outdir),
                        str(src)], capture_output=True)
    assert r.returncode == 1, "假 soffice 沒有回非零離開碼"
    made = list(outdir.iterdir())
    assert made and made[0].stat().st_size > 0, "假 soffice 沒有產出可用的檔案"
