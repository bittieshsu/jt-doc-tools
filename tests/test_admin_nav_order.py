"""管理選單的順序：同類的設定排在一起（v1.17.1；MCP 排在 LLM 設定下面）。

* 每一個管理頁都排進 `NAV_SETTINGS_ORDER` 的某一類；排序表裡也不可以有不存在的頁面。
* 畫面上的順序就是排序表的順序（同一類連在一起）。
"""
from __future__ import annotations

from app import main


def _urls():
    return [u for _g, urls in main.NAV_SETTINGS_ORDER for u in urls]


def test_every_admin_page_is_in_a_group():
    pages = [it["url"] for it in main._NAV_SETTINGS_ALL]
    order = _urls()
    assert len(order) == len(set(order)), "排序表有重複"
    assert sorted(pages) == sorted(order), (
        f"沒有排進類別：{sorted(set(pages) - set(order))}；排序表裡不存在的：{sorted(set(order) - set(pages))}")


def test_the_menu_follows_the_groups():
    assert [it["url"] for it in main._NAV_SETTINGS_ALL] == _urls()
    order = _urls()
    assert order.index("/admin/mcp") == order.index("/admin/llm-settings") + 1, "MCP 排在 LLM 設定下面"


def test_the_sidebar_renders_in_that_order(client, auth_off):
    html = client.get("/").text
    side = html[html.index('data-sbkey="設定"'):]
    pos = [side.find(f'href="{u}"') for u in _urls() if f'href="{u}"' in side]
    assert pos == sorted(pos) and len(pos) >= 20
