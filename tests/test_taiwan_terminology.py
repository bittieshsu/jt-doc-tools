"""使用者看得到的文字不可以用中國大陸用詞。

## 由來

使用者已經當場糾正過十幾個詞（豆腐 / 損壞 / 自適應 / 自帶 / 流式 / 後綴 /
宿主機 / 靜默 / 排查 / 查核 / 顯存 / 派工 / 麵包屑 / 標紅…），每一次都是
「講過了又犯」。靠記得不會有用 —— 寫的當下不會想到那是禁用詞，只有測試擋得住。

## 掃描範圍與例外

只掃**新寫的東西**：工具與管理區的樣板、Python 的中文訊息、前端 JS、
以及 `CHANGELOG` 的**最新一版**。

刻意不掃的：

* **`CHANGELOG` 的歷史版本** —— 那是已經發佈出去的記錄，改掉等於竄改歷史；
  而且很多條目本身就在說明「把某某詞改掉了」，必然含有那個詞。
* **禁用詞清單本身**（`TEST_PLAN` 的用詞檢查項、翻譯工具的對照表、
  這一份測試）—— 它們必須寫出那個詞才能說明要避免什麼。

## 「後綴」是特例

技術固定詞可以留（副檔名的英文叫 suffix、「公司名稱 Co./Ltd. 後綴」）；
**描述位置或規則時**要用「尾端 / 尾端欄位 / 接續欄位」。這條沒辦法自動判斷
語境，所以只在最新版 CHANGELOG 擋 —— 那是最容易寫錯的地方。
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: 禁用詞 → 該用什麼。使用者逐一糾正過的，見 CLAUDE.md 的用詞段落。
BANNED = {
    "豆腐": "方框 / 缺字方框",
    "損壞": "毀損（檔案）/ 不完整（資料）",
    "自適應": "自動調整 / 回應式",
    "自帶": "內建 / 預載",
    "流式": "流動 / 流動排版",
    "宿主機": "實體主機",
    "靜默": "無提示 / 沒有任何反應",
    "排查": "檢視 / 排除 / 追查",
    "查核": "查驗 / 核對 / 確認",
    "顯存": "顯示記憶體 / VRAM",
    "麵包屑": "導覽路徑",
    "標紅": "以紅色標示",
    "並發": "並行 / 同時",
    "回滾": "還原 / 復原",
    "默認": "預設",
    "設置": "設定",
    "文檔": "文件 / 文書",
    "信息": "訊息 / 資訊",
    "網絡": "網路",
    "服務器": "伺服器",
    "字體": "字型",
    "軟件": "軟體",
    "硬件": "硬體",
    "打印": "列印 / 印表機",
    "菜單": "選單",
    "屏幕": "螢幕",
    "保存": "儲存",
    "圖像": "圖片 / 影像",
    "視頻": "影片",
    "一站式": "整合式 / 全方位",
    # ---- 2026-08-15 使用者逐一指出（安裝說明「雙擊安裝程式（推薦）」）----
    # 「按兩下」是微軟繁中與 Adobe 繁中的正式譯法；「雙擊」是中國用語。
    "雙擊": "按兩下",
    # 選項標「推薦」是中國用語；台灣正式文件用「建議」。
    # （「推薦某人 / 某產品」仍然可以用推薦，所以這條只掃使用者看得到的文案。）
    "推薦": "建議",
    # 「點擊」偏中國；台灣用「點選」或「按一下」。
    "點擊": "點選 / 按一下",
    # 「高亮」是中國用語；台灣用「標示」或「醒目提示」。
    "高亮": "標示 / 醒目提示",
    "內核": "核心",
    "端口": "連接埠",
    "緩存": "快取",
    "隊列": "佇列",
    "調試": "除錯",
    "分辨率": "解析度",
    # **刻意不收的詞**（字面撞到，但台灣有完全正確的用法，收了只會製造誤報）：
    #   * 代碼 —— 「銀行代碼」「機關代碼」是正確的（中國用語是把 source code
    #     叫代碼，台灣叫程式碼；那個要靠人看，字串比對分不出來）
    #   * 倉庫 —— 「倉庫租賃」是行業分類
    #   * 鏡像 —— 「左右鏡像」是影像翻轉，跟磁碟映像是兩回事
    "郵箱": "信箱",
    "短信": "簡訊",
}

#: 這些詞在台灣有**完全正確的用法**，只是字面上撞到上面的禁用詞。
#: 掃描時整段跳過 —— 誤報一多這份檢查就會被當成雜訊忽略，那比漏掉更糟。
#:
#: 實際踩過的例子（2026-08-15 全面掃描時）：
#:   * 「用戶端」是 client 的正確譯法，但含「用戶」
#:   * 「撤銷 token」的撤銷 = revoke，正確；跟「復原」(undo) 是兩回事
#:   * 「SSL 在代理卸載」的卸載 = offload，正確
#:   * 「登錄機碼」是 Windows Registry 的正確譯法
#:   * 「已登錄資產」的登錄 = register，正確
#:   * 「會通過的底線」的通過 = pass，正確
#:   * 「DB 內存 sha256」是「DB 內」+「存」，不是「內存」
SAFE_PHRASES = (
    "用戶端", "撤銷", "卸載", "登錄機碼", "登錄檔", "已登錄", "登錄資產",
    "通過測試", "會通過", "通過的", "內存 sha256", "內存放",
)

#: 這些檔案**必須**寫出禁用詞才能發揮作用，掃了只會誤報。
EXEMPT_PARTS = (
    "tests/test_taiwan_terminology.py",
    # CHANGELOG 是**已發佈的歷史紀錄**，裡面還記著「我們檢查過哪些中國用語」
    # （是談論那個詞，不是使用）。整份掃只會製造大量誤報；新條目的用詞由
    # 下面那個專門的 CHANGELOG 檢查負責。
    "github/CHANGELOG.md",
    "TEST_PLAN.md",                       # 用詞檢查項本身就在列這些詞
    "app/tools/translate_doc/router.py",  # 翻譯對照表（陸→台）
    "CLAUDE.md",                          # 專案筆記裡的用詞規則
)


def _files() -> list[pathlib.Path]:
    """要掃的檔案。

    **一定要包含 `github/` 底下的文件**。第一版只掃 `app/` 與 `static/js/`，
    於是安裝說明裡的「方式一：雙擊安裝程式（推薦）」躲了很久 —— 那是使用者
    第一眼看到的文字，比程式裡的任何字串都顯眼，卻剛好在掃描範圍外
    （2026-08-15 使用者自己讀 .md 時發現的）。

    介紹站的 `docs/index.html` 同理，那是對外的門面。
    """
    out: list[pathlib.Path] = []
    for pat in ("app/**/*.html", "app/**/*.py", "static/js/*.js",
                "github/*.md", "github/docs/*.html"):
        out += list(ROOT.glob(pat))
    return [p for p in out
            if not any(part in str(p.relative_to(ROOT)) for part in EXEMPT_PARTS)]


def _strip_comments(text: str, suffix: str) -> str:
    """把註解換成空白（保留行號）。

    **只掃使用者看得到的文字** —— 註解是寫給自己看的內部筆記，裡面出現
    「不要用靜默」「這個詞是大陸用法」之類的說明是正常的，掃了只會一直誤報，
    而誤報多到一定程度，這份測試就會被當成雜訊忽略掉。
    """
    if suffix == ".py":
        # Python：只留字串常數（使用者看得到的訊息都在字串裡）
        import ast
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return text
        # **docstring 要排除** —— 它是寫給維護者看的說明，裡面出現
        # 「原本無法排查」「那正是自動保存的時機」這種敘述是正常的。
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
                body = getattr(node, "body", None) or []
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    docstrings.add(id(body[0].value))
        # 搜尋關鍵字**刻意**收錄大陸用詞 —— 使用者打「字體」也要找得到
        # 字型管理，那是功能不是錯字。要在 AST 上認，重建後的行只剩字串
        # 內容、看不到 key 名。
        skip = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if (isinstance(k, ast.Constant) and k.value == "keywords"
                            and isinstance(v, ast.Constant)):
                        skip.add(id(v))
            if isinstance(node, ast.keyword) and node.arg == "keywords":
                skip.add(id(node.value))
        keep = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstrings and id(node) not in skip):
                keep.append((getattr(node, "lineno", 0), node.value))
        lines = [""] * (text.count("\n") + 2)
        for ln, val in keep:
            if 0 < ln < len(lines):
                lines[ln] += " " + val.replace("\n", " ")
        return "\n".join(lines)
    # 區塊註解（`<!-- -->`、`/* */`）**用掃描不用正規表示式**：
    # 一來它們會跨行，正規表示式版本本來就處理不到；二來用式子剝 HTML
    # 註解容易被畸形寫法繞過（CodeQL 也會標 bad HTML filtering）。
    # 換成掃描之後行為明確，而且換行數保留、行號不會跑掉。
    txt = _blank_blocks(text, [("<!--", "-->"), ("/*", "*/")])
    # 行註解：到行尾為止，逐行處理就夠了
    return "\n".join(line.split("//")[0] for line in txt.split("\n"))


def _blank_blocks(text: str, pairs: list[tuple[str, str]]) -> str:
    """把成對標記之間的內容抹掉，**換行照原樣留著**（行號才不會跑掉）。"""
    out = []
    i = 0
    while i < len(text):
        for start, end in pairs:
            if text.startswith(start, i):
                j = text.find(end, i + len(start))
                j = len(text) if j < 0 else j + len(end)
                out.append("".join(c if c == "\n" else " " for c in text[i:j]))
                i = j
                break
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _offences(text: str, path: str, suffix: str = "") -> list[str]:
    bad = []
    scanned = _strip_comments(text, suffix) if suffix else text
    for i, line in enumerate(scanned.split("\n"), 1):
        # 搜尋關鍵字**刻意**收錄大陸用詞 —— 使用者打「字體」也要找得到
        # 字型管理，那是功能不是錯字。
        if '"keywords"' in line or "'keywords'" in line or "keywords=" in line:
            continue
        for word, better in BANNED.items():
            if word in line:
                bad.append(f"{path}:{i} 用了「{word}」，請改成 {better}")
    return bad


@pytest.mark.parametrize("kind", ["templates", "python", "js", "docs"])
def test_no_mainland_terms_in_source(kind):
    """樣板 / Python 訊息 / 前端 JS / **公開文件**裡的中文一律台灣用詞。

    **`docs`（`github/*.md`）這一類一定要在**。第一版只有前三類，於是
    安裝說明裡的「方式一：雙擊安裝程式（推薦）」躲了很久 —— 那是使用者
    第一眼看到的文字。把 `.md` 加進檔案清單還不夠：這裡的 `suffix` 對照表
    沒有 `.md`，檔案會在下面那個 `continue` 被靜靜濾掉，測試照樣全綠
    （2026-08-15 變異驗證當場發現）。
    """
    suffix = {"templates": ".html", "python": ".py", "js": ".js",
              "docs": ".md"}[kind]
    bad: list[str] = []
    for p in _files():
        if p.suffix != suffix:
            continue
        bad += _offences(p.read_text(encoding="utf-8"),
                         str(p.relative_to(ROOT)), p.suffix)
    assert not bad, "使用者看得到的文字用了大陸用詞：\n" + "\n".join(bad[:20])


def _drop_mentions(text: str) -> str:
    """把「**提及**某個詞」的部分拿掉，只留「**使用**」的部分。

    語言學上這是 use / mention 的差別，而這份測試只該管 use：

    * `字體 → 字型`  —— 說明「把 A 改成 B」，必須寫出 A
    * `打「字體」也找得到`  —— 引號括起來的是在談那個詞本身

    沒有這一層的話，**「修正用詞」這種條目永遠過不了自己的檢查** ——
    寫 CHANGELOG 說明改掉了哪個詞，反而被判定成用了那個詞。
    """
    # 「A → B」的箭頭映射：連同前面那個詞一起拿掉
    text = re.sub(r"[^\s，。；、）】」]+\s*(?:→|->)\s*", " ", text)
    # 引號內的詞：在談那個詞本身。上限放到 24 字 —— 引用整句原文當反例
    # （「方式一：雙擊安裝程式（推薦）」有 14 字）也是 mention；
    # 真正的誤用發生在行文裡，不會躲在引號中。
    text = re.sub(r"[「『][^」』]{1,24}[」』]", " ", text)
    return text


def test_no_mainland_terms_in_latest_changelog_entry():
    """只擋**最新一版** —— 歷史版本是已發佈的記錄，改掉等於竄改歷史。"""
    text = (ROOT / "github" / "CHANGELOG.md").read_text(encoding="utf-8")
    parts = re.split(r"^## \[", text, flags=re.M)
    if len(parts) < 2:
        pytest.skip("CHANGELOG 格式不符預期")
    latest = _drop_mentions(parts[1])
    bad = _offences(latest, "CHANGELOG(最新版)")
    # 「後綴」描述位置時要用「尾端」；技術固定詞（副檔名 / 公司名稱後綴）可留
    for i, line in enumerate(latest.split("\n"), 1):
        if "後綴" in line and not any(
                k in line for k in ("副檔名", "公司名稱", "前後綴")):
            bad.append(f"CHANGELOG(最新版):{i} 「後綴」在描述位置時請用「尾端」")
    assert not bad, "最新版 CHANGELOG 用了大陸用詞：\n" + "\n".join(bad[:20])
