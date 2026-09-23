"""轉逐字稿的波形：游標旁的標籤要寫出**那一刻是誰在講**（v1.16.10，使用者要求）。

判斷「那一刻是哪一段」在 `static/js/meeting_wave_tip.js`，這裡**在 node 裡真的跑它**
測邊界 —— 畫面上那個標籤要有逐字稿資料才看得到，而瀏覽器測試開的都是空頁面。

判準：

* 落在某一段的 [start_ms, end_ms) 裡 → 那一段
* **兩段之間的空檔回 null，不猜一個人上去**（空檔沒人講話；硬挑最近的一位會讓人以為那裡有聲音）
* 兩人重疊 → **後開始**的那一段（通常是插話的人）
* 沒有起訖時間的段落不參與（三層對不上時可能只有文字沒有時間）
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
JS = ROOT / "static" / "js" / "meeting_wave_tip.js"
TEMPLATE = (ROOT / "app" / "tools" / "meeting_transcribe" / "templates"
            / "meeting_transcribe.html")

_SEGS = [
    {"seq": 3, "speaker": "S1", "start_ms": 21000, "end_ms": 25000},   # 刻意亂序
    {"seq": 1, "speaker": "S1", "start_ms": 0, "end_ms": 9000},
    {"seq": 2, "speaker": "S2", "start_ms": 10000, "end_ms": 20000},
    # S3 在 S2 講到一半插話（重疊 12~14 秒）
    {"seq": 4, "speaker": "S3", "start_ms": 12000, "end_ms": 14000},
    {"seq": 5, "speaker": "S2", "text": "沒有時間的段落"},
    {"seq": 6, "speaker": "S1", "start_ms": 30000, "end_ms": 30000},  # 長度 0
]

_HARNESS = r"""
// `node -e` 的 argv 沒有腳本檔那一格：argv[1] 就是第一個參數
global.window = {};
require(process.argv[1]);
var T = global.window.MtWaveTip;
var segs = JSON.parse(process.argv[2]), probes = JSON.parse(process.argv[3]);
var idx = T.index(segs);
var out = probes.map(function (ms) { var s = T.segmentAt(idx, ms); return s ? s.seq : null; });
process.stdout.write(JSON.stringify({ indexed: idx.map(function (s) { return s.seq; }), at: out }));
"""


def _node(segs, probes):
    node = shutil.which("node")
    if not node:
        pytest.skip("沒有 node")
    r = subprocess.run([node, "-e", _HARNESS, str(JS), json.dumps(segs), json.dumps(probes)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_segments_without_times_are_left_out_and_the_rest_sorted():
    out = _node(_SEGS, [])
    assert out["indexed"] == [1, 2, 4, 3], out


@pytest.mark.parametrize("ms,seq", [
    (0, 1), (8999, 1),
    (9000, None),          # 第 1 段結束時刻起就不算它了
    (9500, None),          # 空檔：不猜人
    (10000, 2), (11999, 2),
    (12000, 4), (13000, 4),  # 重疊：後開始的（插話的人）
    (14000, 2), (19999, 2),  # 插話結束後回到原本的人
    (20500, None), (21000, 3), (24999, 3),
    (25000, None), (30000, None), (99999, None),
])
def test_which_segment_is_speaking_at_each_moment(ms, seq):
    assert _node(_SEGS, [ms])["at"] == [seq]


def test_a_long_segment_is_found_even_after_many_short_ones_start_inside_it():
    """長段落裡面後開始了好幾段短的（例如主持人講很久、別人插了好幾句）：
    短的都結束之後，要找得回那一長段 —— 不可以只看「最後一個開始的」。"""
    segs = [{"seq": 1, "speaker": "S1", "start_ms": 0, "end_ms": 100000}]
    segs += [{"seq": 10 + i, "speaker": "S2", "start_ms": 1000 + i * 1000,
              "end_ms": 1500 + i * 1000} for i in range(20)]
    out = _node(segs, [1200, 1800, 50000])
    assert out["at"] == [10, 1, 1], out


def test_bad_input_does_not_throw():
    out = _node([], [5000])
    assert out["at"] == [None]


def _script_body() -> str:
    html = TEMPLATE.read_text(encoding="utf-8")
    return re.sub(r"\{#.*?#\}", "", html, flags=re.S)


def test_the_page_loads_the_helper_and_uses_it_for_the_tip():
    body = _script_body()
    assert '<script src="/static/js/meeting_wave_tip.js"></script>' in body
    assert 'id="mtWaveTip"' in body and 'id="mtTipWho"' in body
    assert "MtWaveTip.segmentAt(timed" in body, "標籤沒有用共用的判斷"
    # 名字要走 speakerName（改過名的顯示新名字），不是直接印 S1
    assert re.search(r"who\.textContent\s*=\s*speakerName\(seg\)", body)
    # 滑鼠移動與離開都要更新標籤，不然離開後標籤會一直掛著
    assert re.search(r"mousemove[\s\S]{0,200}showWaveTip\(\)", body)
    assert re.search(r"mouseleave[\s\S]{0,120}showWaveTip\(\)", body)


def test_the_time_label_is_no_longer_painted_into_the_canvas():
    """標籤改成 HTML 之後 canvas 裡不可以再畫一份（會出現兩個標籤疊在一起）。"""
    body = _script_body()
    assert "fillText(" not in body


def test_the_index_is_declared_before_render_uses_it():
    """`var timed = []` 若寫在 render 之後，初始化會把 render 算好的蓋回空陣列。"""
    body = _script_body()
    decl = body.index("var timed = [];")
    assert decl < body.index("function render()"), "timed 的宣告在 render 後面"
    assert body.count("timed = [") == 1, "timed 在別處又被重設"
