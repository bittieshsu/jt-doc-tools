"""介紹站的語言下拉只能跳到**同目錄的 `.html`**。

`window.location.href = <DOM 裡的字串>` 這一行，值是我們自己產生的
`<option value="index-ja.html">` —— 但那是 DOM 裡的字串，
**只要有人改得到它，`javascript:…` 就會在這裡執行**。

限制成白名單形狀之後這條路就不存在了，而且對真正的六個檔名完全沒有影響
（`index.html` / `index-en.html` / `api-ja.html` / `troubleshooting-en.html` …）。

**判準是「有沒有先過一道形狀檢查」，不是「有沒有寫某個字」** ——
只驗字串的話，把檢查改成 `if (true)` 也會全綠。所以這裡真的把那支 JS
跑起來（node），餵五種值看它走不走。
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))
from tools.repo_paths import public_root                      # noqa: E402

JS = public_root(ROOT) / "docs" / "lang-switch.js"


def test_the_file_is_where_we_think_it_is():
    """搬走或改名時要先紅 —— 不然下面那條會變成空的。"""
    assert JS.is_file(), f"找不到 {JS}"


@pytest.mark.parametrize("value,should_go", [
    ("index-ja.html", True),
    ("api-en.html", True),
    ("troubleshooting.html", True),
    ("javascript:alert(1)", False),
    ("//evil.example/x.html", False),
    ("https://evil.example/index.html", False),
    ("../../etc/passwd", False),
])
def test_only_a_plain_html_filename_navigates(value, should_go):
    node = shutil.which("node")
    if not node:
        pytest.skip("這台沒有 node")

    harness = f"""
const src = require('fs').readFileSync({json.dumps(str(JS))}, 'utf8');
let went = null;
const handlers = [];
global.document = {{ getElementById: () => ({{
    options: [], value: {json.dumps(value)},
    addEventListener: (_, fn) => handlers.push(fn),
}}) }};
global.window = {{ get location() {{ return {{ set href(v) {{ went = v; }} }}; }} }};
// location 用 getter 會每次換新物件，改成單一物件才收得到
const loc = {{ set href(v) {{ went = v; }} }};
global.window = {{ location: loc }};
eval(src);
handlers.forEach(fn => fn.call({{ value: {json.dumps(value)} }}));
console.log(JSON.stringify(went));
"""
    out = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    went = json.loads(out.stdout.strip())
    if should_go:
        assert went == value, f"正常的檔名 {value} 應該要跳過去，卻沒有"
    else:
        assert went is None, f"{value} 不該被跳過去，卻跳了（{went}）"
