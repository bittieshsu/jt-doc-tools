"""目錄瀏覽「已選定」模式的全域 filter 設定 + 純函式工具。

目錄瀏覽（/admin/directory）有兩種模式：
  - selected（預設）：只顯示符合「規則式 filter」的 group / ou / user，樹狀只留
    通往這些物件的分支。
  - all：現有的完整逐層樹狀瀏覽。

filter 是「全域一份、admin 共用」的規則清單。每條規則：
  { name_contains: str,  # 名稱關鍵字（子字串，不分大小寫；空=不限）
    types: [str],        # 物件類型子集 ou/group/user（空=三者皆可）
    base_dn: str }       # 限定在此 OU 子樹內搜（空=整個目錄 root）
物件符合 filter = 符合「任一」規則（union）。

本模組只放：設定存取 + 「規則 → LDAP filter 字串」+「符合物件清單 → 剪枝樹」
這些純函式（可單元測試，不碰 LDAP）；實際查目錄在 auth_ldap.search_selected_objects。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

VALID_TYPES = ("ou", "group", "user")

_DEFAULTS: dict[str, Any] = {
    # 預設先開「全部」（完整目錄樹）；admin 設好 filter 規則後可改成 selected。
    "default_mode": "all",        # "selected" | "all"
    "rules": [],                  # list[{name_contains, types, base_dn}]
}

# 物件類型 → objectClass 子過濾（跨 AD / OpenLDAP / Univention 相容）。
_TYPE_FILTER = {
    "ou": "(objectClass=organizationalUnit)",
    "group": ("(|(objectClass=group)(objectClass=groupOfNames)"
              "(objectClass=groupOfUniqueNames)(objectClass=posixGroup))"),
    "user": ("(|(objectClass=inetOrgPerson)(objectClass=posixAccount)"
             "(&(objectClass=user)(!(objectClass=computer))))"),
}


# --------------------------------------------------------------------- settings

def _path():
    from ..config import settings
    return settings.data_dir / "dir_filter.json"


def _clean_rule(r: dict) -> Optional[dict]:
    if not isinstance(r, dict):
        return None
    name = str(r.get("name_contains") or "").strip()[:128]
    types = [t for t in (r.get("types") or []) if t in VALID_TYPES]
    base = str(r.get("base_dn") or "").strip()[:512]
    # 一條規則至少要有一個條件，否則等於「全選」失去 filter 意義 → 丟掉
    if not name and not types and not base:
        return None
    return {"name_contains": name, "types": types, "base_dn": base}


def get_settings() -> dict[str, Any]:
    p = _path()
    data = json.loads(json.dumps(_DEFAULTS))
    try:
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                if raw.get("default_mode") in ("selected", "all"):
                    data["default_mode"] = raw["default_mode"]
                rules = []
                for r in (raw.get("rules") or []):
                    cr = _clean_rule(r)
                    if cr:
                        rules.append(cr)
                data["rules"] = rules
    except Exception:  # noqa: BLE001
        logger.warning("dir_filter settings unreadable; using defaults")
    return data


def save_settings(*, default_mode: Optional[str] = None,
                  rules: Optional[list] = None) -> dict[str, Any]:
    data = get_settings()
    if default_mode in ("selected", "all"):
        data["default_mode"] = default_mode
    if rules is not None:
        cleaned = []
        for r in rules:
            cr = _clean_rule(r)
            if cr:
                cleaned.append(cr)
        data["rules"] = cleaned
    _write(data)
    return data


def _write(data: dict[str, Any]) -> None:
    p = _path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
    except Exception:  # noqa: BLE001
        logger.exception("failed to persist dir_filter settings")


# ------------------------------------------------------ 規則 → LDAP filter（純函式）

def build_rule_filter(rule: dict) -> str:
    """把一條規則轉成 LDAP filter 字串（不含 base；base 另取）。

    (& <type-filter> [<name-filter>])
    name_contains 以 escape_filter_chars 轉義後做多欄位子字串比對。
    """
    from ldap3.utils.conv import escape_filter_chars
    types = [t for t in (rule.get("types") or []) if t in VALID_TYPES] or list(VALID_TYPES)
    type_parts = "".join(_TYPE_FILTER[t] for t in types)
    type_filter = type_parts if len(types) == 1 else f"(|{type_parts})"
    name = str(rule.get("name_contains") or "").strip()
    if not name:
        return type_filter
    esc = escape_filter_chars(name)
    name_filter = (f"(|(cn=*{esc}*)(ou=*{esc}*)(displayName=*{esc}*)"
                   f"(sAMAccountName=*{esc}*)(uid=*{esc}*))")
    return f"(&{type_filter}{name_filter})"


def rule_base(rule: dict, root_base: str) -> str:
    """規則的搜尋 base：規則自訂 base_dn，否則目錄 root。"""
    b = str(rule.get("base_dn") or "").strip()
    return b or (root_base or "")


# ------------------------------------------------------ 符合物件 → 剪枝樹（純函式）

def _split_dn(dn: str) -> list[str]:
    """把 DN 依「未被跳脫的逗號」切成 RDN 片段（保留原字串）。"""
    return re.split(r"(?<!\\),", (dn or "").strip())


def _rdn_value(rdn: str) -> str:
    """'OU=Sales' → 'Sales'；沒有 '=' 就原樣回。"""
    i = rdn.find("=")
    return rdn[i + 1:].strip() if i >= 0 else rdn.strip()


def _norm_dn(dn: str) -> str:
    return ",".join(p.strip() for p in _split_dn(dn)).lower()


def _is_all_dc(dn: str) -> bool:
    parts = _split_dn(dn)
    return bool(parts) and all(p.strip().lower().startswith("dc=") for p in parts)


def prune_tree(matches: list[dict], root_base: str = "") -> list[dict]:
    """把「符合物件清單」建成剪枝樹：只含這些物件 + 通往它們的祖先 OU。

    matches: [{dn, name, type}]（type 為 ou/group/user/…）
    root_base: 目錄 root DN；祖先鏈生到此為止（此節點本身不出現，當隱含 root）。
               空字串時，鏈生到「純 DC」層為止。
    回傳：巢狀 [{dn, name, type, matched, children:[…]}]，parent 一定排在 child 前。
    cycle-safe，每個 DN 只出現一次；祖先 OU 的 name 由 RDN 推得。
    """
    root_norm = _norm_dn(root_base) if root_base else ""
    nodes: dict[str, dict] = {}      # norm_dn -> node
    parent_of: dict[str, Optional[str]] = {}

    def ensure(dn: str, name: str, typ: str, matched: bool) -> str:
        n = _norm_dn(dn)
        cur = nodes.get(n)
        if cur is None:
            nodes[n] = {"dn": dn, "name": name, "type": typ, "matched": matched}
        else:
            if matched:
                cur["matched"] = True
                cur["name"] = name
                cur["type"] = typ
        return n

    for m in matches:
        dn = m.get("dn") or ""
        if not dn:
            continue
        parts = _split_dn(dn)
        child = ensure(dn, m.get("name") or _rdn_value(parts[0]),
                       m.get("type") or "node", True)
        hit_root = False
        for i in range(1, len(parts)):
            anc_dn = ",".join(parts[i:])
            anc_norm = _norm_dn(anc_dn)
            if root_norm and anc_norm == root_norm:
                parent_of.setdefault(child, None)
                hit_root = True
                break
            if not root_norm and _is_all_dc(anc_dn):
                # 沒設 root：鏈生到純 DC 層就停（那層當隱含 root）
                parent_of.setdefault(child, None)
                hit_root = True
                break
            ensure(anc_dn, _rdn_value(parts[i]), "ou", False)
            parent_of[child] = anc_norm
            child = anc_norm
        if not hit_root:
            parent_of.setdefault(child, None)

    children: dict[str, list[str]] = {n: [] for n in nodes}
    roots: list[str] = []
    for n in nodes:
        p = parent_of.get(n)
        if p and p in nodes and p != n:
            children[p].append(n)
        else:
            roots.append(n)

    def build(n: str) -> dict:
        node = dict(nodes[n])
        kids = sorted(children[n], key=lambda x: (nodes[x]["name"] or "").lower())
        node["children"] = [build(k) for k in kids]
        return node

    return [build(r) for r in sorted(roots, key=lambda x: (nodes[x]["name"] or "").lower())]
