"""目錄瀏覽「已選定」模式 filter 的純函式 + 設定測試。

涵蓋：規則 → LDAP filter 字串、符合物件 → 剪枝樹、設定 roundtrip / 清洗。
不碰 LDAP（search_selected_objects 走真實目錄，屬 e2e / 手動驗收）。
"""
from __future__ import annotations

import json

import pytest

from app.core import dir_filter as df


# ------------------------------------------------------------- build_rule_filter

def test_rule_filter_single_type_no_name():
    f = df.build_rule_filter({"types": ["ou"]})
    assert f == "(objectClass=organizationalUnit)"


def test_rule_filter_empty_types_means_all_three():
    f = df.build_rule_filter({"types": []})
    assert f.startswith("(|")
    assert "organizationalUnit" in f and "posixGroup" in f and "inetOrgPerson" in f


def test_rule_filter_name_contains_is_escaped_and_multifield():
    f = df.build_rule_filter({"types": ["group"], "name_contains": "sa*les"})
    # '*' 在 name 值裡必須被轉義（escape_filter_chars → \2a），不可當萬用字元
    assert "sa\\2ales" in f or "sa\\2Ales" in f
    assert "(&" in f and "cn=*" in f and "displayName=*" in f


def test_rule_filter_group_types_cover_common_objectclasses():
    f = df.build_rule_filter({"types": ["group"]})
    for oc in ("group", "groupOfNames", "posixGroup"):
        assert oc in f


# --------------------------------------------------------------------- prune_tree

_ROOT = "DC=corp,DC=example"


def test_prune_tree_builds_ancestor_chain():
    matches = [{"dn": f"CN=Sales Team,OU=Sales,OU=TW,{_ROOT}",
                "name": "Sales Team", "type": "group"}]
    tree = df.prune_tree(matches, _ROOT)
    # root 之下第一層 = OU=TW；其下 OU=Sales；其下才是符合的群組
    assert len(tree) == 1
    tw = tree[0]
    assert tw["name"] == "TW" and tw["type"] == "ou" and tw["matched"] is False
    sales = tw["children"][0]
    assert sales["name"] == "Sales" and sales["matched"] is False
    grp = sales["children"][0]
    assert grp["name"] == "Sales Team" and grp["type"] == "group" and grp["matched"] is True
    assert grp["children"] == []


def test_prune_tree_shared_ancestors_merge_and_dedupe():
    matches = [
        {"dn": f"CN=A,OU=Sales,OU=TW,{_ROOT}", "name": "A", "type": "user"},
        {"dn": f"CN=B,OU=Sales,OU=TW,{_ROOT}", "name": "B", "type": "user"},
        {"dn": f"OU=Sales,OU=TW,{_ROOT}", "name": "Sales", "type": "ou"},
    ]
    tree = df.prune_tree(matches, _ROOT)
    tw = tree[0]
    sales = tw["children"][0]
    # OU=Sales 只出現一次；因為也在 matches 內 → matched 應為 True
    assert sales["matched"] is True and sales["type"] == "ou"
    names = sorted(c["name"] for c in sales["children"])
    assert names == ["A", "B"]


def test_prune_tree_matched_flag_wins_over_ancestor():
    # 同一個 OU 先被當祖先（matched=False）建立，後又是規則命中 → matched True
    matches = [
        {"dn": f"CN=X,OU=Eng,{_ROOT}", "name": "X", "type": "user"},
        {"dn": f"OU=Eng,{_ROOT}", "name": "Eng", "type": "ou"},
    ]
    tree = df.prune_tree(matches, _ROOT)
    eng = tree[0]
    assert eng["name"] == "Eng" and eng["matched"] is True


def test_prune_tree_no_root_base_stops_at_dc():
    matches = [{"dn": f"CN=Z,OU=HR,{_ROOT}", "name": "Z", "type": "user"}]
    tree = df.prune_tree(matches, "")  # 無 root → 生到純 DC 層即止
    assert len(tree) == 1
    hr = tree[0]
    assert hr["name"] == "HR"           # 根層是 OU=HR（DC 層當隱含 root）
    assert hr["children"][0]["name"] == "Z"


def test_prune_tree_parent_before_child_and_cycle_safe():
    matches = [{"dn": f"CN=Deep,OU=L3,OU=L2,OU=L1,{_ROOT}", "name": "Deep", "type": "user"}]
    tree = df.prune_tree(matches, _ROOT)
    # 逐層下探，深度 4（L1→L2→L3→Deep）
    depth = 0
    node = tree[0]
    while node:
        depth += 1
        node = node["children"][0] if node["children"] else None
    assert depth == 4


# ------------------------------------------------------------------- settings I/O

def test_settings_default_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(df, "_path", lambda: tmp_path / "dir_filter.json")
    s = df.get_settings()
    assert s["default_mode"] == "all"          # 預設先開「全部」
    assert s["rules"] == []


def test_settings_roundtrip_and_rule_cleaning(tmp_path, monkeypatch):
    monkeypatch.setattr(df, "_path", lambda: tmp_path / "dir_filter.json")
    df.save_settings(default_mode="all", rules=[
        {"name_contains": "sales", "types": ["group", "bogus"], "base_dn": "OU=TW,DC=x"},
        {"name_contains": "", "types": [], "base_dn": ""},   # 全空 → 丟棄
        {"types": ["user"]},
    ])
    s = df.get_settings()
    assert s["default_mode"] == "all"
    assert len(s["rules"]) == 2                              # 全空那條被清掉
    r0 = s["rules"][0]
    assert r0["types"] == ["group"]                         # bogus 被過濾
    assert r0["name_contains"] == "sales"


def test_settings_invalid_mode_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(df, "_path", lambda: tmp_path / "dir_filter.json")
    df.save_settings(default_mode="selected", rules=[])
    df.save_settings(default_mode="nonsense")               # 無效 → 不變
    assert df.get_settings()["default_mode"] == "selected"
