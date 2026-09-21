"""HTML 轉檔一律走 **Writer** 篩選器，不可以落到 Writer/Web。

## 踩到的（使用者 2026-09-18 回報）

`markdown-to-doc` 轉出來的 PDF「第 2、3、4 頁怎麼這麼空」。實測同一份文件：

| | 頁數 | 空白頁（<150 字） | 每頁字數中位數 |
|---|---:|---:|---:|
| Writer/Web（原本） | 36 | **3** | 687 |
| Writer（`--infilter`） | **25** | **0** | **1029** |

soffice 對 HTML 輸入**預設走 Writer/Web**（「網頁檢視」模式），
它的分頁很糟：表格會被切成一列一頁，欄寬也會亂掉。

**這條規則 v1.11.36 就記過**（當時是為了 ODT 的 mimetype 變成 `text-web`），
`convert_to_odt` / `convert_to_docx` 都加了 —— **只有 `convert_to_pdf` 漏掉**，
而那正是使用者最常看的那個輸出。同一個家族要一次掃完。
"""
from __future__ import annotations

import ast
from pathlib import Path
import inspect

import pytest

from app.core import office_convert as oc

#: 會收 HTML 當輸入的轉檔函式。**新增第四支時這裡要跟著加。**
_HTML_CONVERTERS = ("convert_to_pdf", "convert_to_odt", "convert_to_docx")

_FILTER = "HTML (StarWriter)"


@pytest.mark.parametrize("name", _HTML_CONVERTERS)
def test_html_input_selects_the_writer_filter(name):
    fn = getattr(oc, name)
    src = inspect.getsource(fn)
    assert "input_filter" in inspect.signature(fn).parameters, (
        f"{name} 沒有 input_filter 參數 —— HTML 輸入會落到 Writer/Web")
    assert _FILTER in src, f"{name} 沒有指定 {_FILTER}"
    assert ".html" in src and ".htm" in src, (
        f"{name} 沒有依副檔名自動選篩選器 —— 呼叫端忘記傳就又掉回 Web 排版")


@pytest.mark.parametrize("name", _HTML_CONVERTERS)
def test_the_filter_actually_reaches_the_command_line(name):
    """**光是算出 `input_filter` 不算數** —— 要真的加進 soffice 的參數。

    只驗「程式碼裡有那個字串」的話，把 `soffice_args += [...]` 那行刪掉
    測試照樣綠。所以判準放在 AST 節點上：**要有一個把含 `--infilter=`
    的東西接到 `soffice_args` 上的 `+=`**。
    """
    tree = ast.parse(Path(oc.__file__).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == name)
    ok = False
    for node in ast.walk(fn):
        if not isinstance(node, ast.AugAssign):
            continue
        if not (isinstance(node.target, ast.Name)
                and node.target.id == "soffice_args"):
            continue
        blob = " ".join(c.value for c in ast.walk(node.value)
                        if isinstance(c, ast.Constant) and isinstance(c.value, str))
        if "--infilter=" in blob:
            ok = True
    assert ok, f"{name} 沒有把 --infilter 真的加進 soffice_args"


def test_the_scan_actually_covers_every_converter():
    """「掃 0 支」跟「全部合格」在 pytest 輸出裡長得一樣。"""
    names = [n for n in dir(oc)
             if n.startswith("convert_to_") and not n.endswith("_async")
             and callable(getattr(oc, n))]
    missing = [n for n in _HTML_CONVERTERS if n not in names]
    assert not missing, f"清單裡的函式已經不存在：{missing}"
    assert len(names) >= 3, f"只掃到 {names}"
