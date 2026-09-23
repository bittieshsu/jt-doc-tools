"""NSIS 原始碼的「只看程式、不看註解」抽取 —— 給自動檢查用。

**為什麼要收成一份共用**：這個專案的靜態檢查一再被**解釋規則的註解**騙到
（v1.15.28 一輪就三次、加上安裝程式這支共四次）。NSIS 的註解有兩種寫法，
而且**行尾註解最容易漏**：

    ; 整行註解
    Var SM_DIR      ; 行尾註解 ← 只跳過「開頭是 ;」的話，這行會被掃進去

還有一個陷阱：**字串裡也可能有分號**（`MessageBox "a;b"`），所以不能直接
`split(";")`。下面用一個小狀態機追引號。
"""
from __future__ import annotations


def strip_comment(line: str) -> str:
    """去掉 NSIS 的行尾註解（`;` 與 `#`），但不動字串裡的分號。"""
    out = []
    in_str = False
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == '"':
            # NSIS 用 `$\"` 跳脫引號
            if i >= 2 and line[i - 2:i] == "$\\":
                out.append(ch)
                i += 1
                continue
            in_str = not in_str
            out.append(ch)
        elif ch in ";#" and not in_str:
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out).rstrip()


def code_lines(text: str) -> list[tuple[int, str]]:
    """回傳 `(行號, 只剩程式的那一段)`，空行與純註解行直接略過。"""
    out = []
    for n, raw in enumerate(text.splitlines(), 1):
        code = strip_comment(raw)
        if code.strip():
            out.append((n, code))
    return out


def code_text(text: str) -> str:
    return "\n".join(c for _, c in code_lines(text))


# ---------------------------------------------------------------- 語言表

#: 譯文裡出現漢字是**正確的**那幾種語言。
#:
#: 日文的「文書」「必須」「変換」都是正確的日文，只是剛好也是漢字 ——
#: 一律用「有沒有漢字」判斷的話整份日文都是誤報，而**誤報一多這份檢查就會
#: 被當雜訊忽略**（用詞檢查那次的教訓）。
CJK_IS_NATIVE = frozenset({"TRADCHINESE", "SIMPCHINESE", "JAPANESE", "KOREAN"})

#: 中文與日文都用漢字，靠「現代日文不會用的中文詞」分辨「整條忘了翻」。
#: 與語系檔那條檢查**共用同一份清單**，不要在這裡另抄一份。
def _not_japanese() -> tuple[str, ...]:
    from .i18n_untranslated_scan import NOT_JAPANESE
    return tuple(NOT_JAPANESE)


def declared_languages(text: str) -> set[str]:
    """`.nsi` 裡宣告了哪些語言表。

    **不要在測試裡寫死語言清單** —— 加第三種語言之後，寫死的檢查會
    「照樣全綠，只是沒在驗那個語言」（本專案在 i18n 上踩過八次）。
    """
    import re
    # **一律大寫** —— 宣告寫的是 `"TradChinese"`，而 LangString 用的是
    # `${LANG_TRADCHINESE}`。兩邊不正規化的話比對永遠對不上，而那看起來
    # 會像「這個語言沒宣告」。
    return {m.upper() for m in
            re.findall(r'!insertmacro\s+MUI_LANGUAGE\s+"(\w+)"', text)}


def langstring_language(code_line: str) -> "str | None":
    """這一行是某個語言的 LangString 嗎？是的話回語言名稱。"""
    import re
    m = re.match(r'\s*LangString\s+\w+\s+\$\{LANG_(\w+)\}', code_line)
    return m.group(1) if m else None


def looks_like_untranslated_chinese(code_line: str, lang: str) -> bool:
    """這一行的譯文看起來是「整段忘了翻的中文」嗎？

    只對**漢字圈**的語言有意義（其他語言用「不可以有漢字」那條更嚴的判準）。
    這是**啟發式**：抓得到「整段留著中文」，抓不到「翻得不好」。
    """
    if lang != "JAPANESE":
        return False
    return any(w in code_line for w in _not_japanese())
