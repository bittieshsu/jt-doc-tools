"""從逐字稿裡的自我介紹找出發言者可能的名字（2026-10-03 使用者要求）。

「大家好，我是 Wendy」「我叫王小明」「This is Tom from Contoso」—— 發言者在會議裡報過
名字的話，轉逐字稿頂端那一位的標籤上提示「可能是 XXX」，按一下就換進去。

**只建議、不自動換。** 判錯的代價是把 A 的話掛到 B 的名字上 —— 引用機制最在意的那一種錯
（「有一條決議、標了一個錯的人」，而讀的人沒有理由懷疑）。所以判準在
**「不該建議的不建議」**，寧可漏掉也不要亂猜：

* 中文名字一定要是**常見姓氏開頭的三個字**（兩個字的要有「我叫」、問候語或「陳總」這種稱呼），而且**後面緊接著句讀、語助詞或句尾**（沒有標點的原始層另外認幾個常見的下一句開頭）——
  中文沒有詞界，不這樣的話 `我是覺得…`、`我是說的` 都會被當成名字。
* 英文名字要大寫開頭、不在常見詞表裡（`I'm Fine`、`我是 OK 的`）；**後面要是句尾或句讀** ——
  `我是 Proxmox 的愛用者` 的 `Proxmox` 後面接著「的」，不算。
* 「我是 … 的業務 Wendy」（先講單位職稱、名字放最後）：名字前面**一定要是職稱**，
  名字後面一樣要是句尾或句讀。只看「句尾的英文字」的話，`我是用 Proxmox` 會中。
* `this is` / `it's` 太常拿來講東西不講人 —— 後面要接 `from` / `here` / `speaking` 才算。

一位發言者報過不只一個名字（辨識把兩個人併成一位，或有人說「我是替 Tom 來的 Amy」）時，
取出現最多次的、同次數取先出現的，其他的一起回給畫面（滑過看得到）。
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

#: 台灣常見姓氏（內政部姓氏統計前段 ＋ 常見複姓）。**只用來判斷「這兩三個字像不像人名」**，
#: 不在表上的姓氏會漏掉（寧可漏）。
_SURNAMES = set(
    "陳林黃張李王吳劉蔡楊許鄭謝郭洪曾邱廖賴周徐蘇葉莊呂江何蕭羅高潘簡朱鍾游彭詹施胡沈余盧梁"
    "趙顏柯翁魏孫戴范方宋鄧杜傅侯曹薛丁卓阮馬董温溫唐藍石蔣古紀姚連馮歐程湯黎田康姜白汪鄒尤"
    "巫鐘黎涂龔嚴韓袁金童陸夏柳凃邵錢伍倪溫于譚駱熊任甘秦顧毛章史官萬俞雷粘饒張闕凌崔尹孔辛武"
    "辜陶易段龍韋葛池孟褚殷麥賀賈莫文管關向包丘梅華利裴樊房全佘左花鄞鮑")
_COMPOUND = ("歐陽", "司馬", "諸葛", "上官", "張簡", "范姜", "東方", "司徒")

#: 看起來像名字、其實是常見詞的（英文）。大寫開頭只是因為在句首或辨識這樣寫。
_EN_NOT_NAMES = {
    "ok", "okay", "fine", "good", "sorry", "sure", "here", "not", "just", "going", "back",
    "done", "ready", "glad", "happy", "yes", "no", "so", "the", "a", "an", "it", "this",
    "that", "very", "also", "still", "really", "now", "there", "from", "in", "on", "at",
    "with", "and", "or", "but", "hi", "hello", "thanks", "thank", "busy", "free", "late",
    "new", "right", "agree", "afraid", "available", "speaking", "calling", "pm", "ceo",
    "cto", "cfo", "sales", "manager", "engineer", "team", "everyone", "all", "we", "i",
}

#: 「我是 … 的〔職稱〕名字」那種講法裡，名字前面可以出現的職稱
_TITLES = (
    "業務", "經理", "副理", "協理", "襄理", "處長", "課長", "科長", "組長", "部長", "主任",
    "主管", "總監", "副總", "總經理", "董事長", "執行長", "專員", "工程師", "架構師", "顧問",
    "窗口", "負責人", "助理", "秘書", "講師", "老師", "代表", "研究員", "設計師", "分析師",
    "同事", "PM", "AM", "SE", "FAE", "CEO", "CTO", "Sales", "sales",
)

#: 中文自我介紹的開頭。「我叫 / 我的名字」是明確在報名字；「我是」什麼都可以接，要從嚴。
_ZH_MARKERS = re.compile(r"我的名字(?:是|叫)|我這邊是|我叫做|我叫|我是")
_EXPLICIT = ("我的名字是", "我的名字叫", "我叫做", "我叫")
#: 開頭與名字之間常見的贅字（「我是那個王小明」「我是就是業務 Wendy」）
_FILLERS = re.compile(r"^(?:\s|那個|就是|這個|然後|嗯|呃|恩|欸|也|就|那)+")
#: 名字後面允許接的：句尾、句讀、語助詞
_TERM = re.compile(r"^(?:$|[，、,。．.！!？?；;：:）)」』…～~]|啦|喔|哦|啊|呀|囉|嘿|唷|哈|耶)")
#: 沒有標點的逐字稿（原始層）裡，名字後面常直接接的話。只給「我是 / 我的名字」用 ——
#: 「我叫王小明負責」是「叫王小明去負責」，不是自我介紹。
_CONT = ("今天", "目前", "現在", "來自", "跟大家", "向大家", "很高興", "很榮幸", "接下來", "那我", "我")
#: 有問候語的那一段，「我是」後面接兩個字的名字也算（「大家好，我是李四」）
_GREETING = re.compile(r"大家好|各位好|各位|您好|你好|哈囉|嗨|早安|午安|晚安|Hello|hello|Hi\b|hi\b")
#: 一句話到這裡就結束（名字不會跨過去）
_SENT_END = re.compile(r"[。！？!?；;]")
#: 姓氏開頭、但其實是常見詞的（「我是高興啦」「我是方便的」「大家好，我是顧問」）。
#: 只列真的會接在「我是」後面的；不在表上的靠「後面要是句讀」那一道擋。
_WORD_PREFIX = {
    "曾經", "簡單", "簡報", "方便", "方案", "方向", "方面", "方式", "高興", "高手", "高中", "高層",
    "高階", "周末", "周邊", "何時", "許多", "黃色", "黃金", "江湖", "管理", "施工", "全部", "全職",
    "全體", "全新", "文組", "文件", "文書", "文化", "石頭", "石化", "王牌", "胡說", "沈默", "陳述",
    "白做", "白天", "白色", "金牌", "金融", "金額", "連線", "連續", "連結", "程式", "程序", "程度",
    "向您", "向你", "鄭重", "董事", "馬上", "包商", "包含", "包括", "利用", "房東", "左邊", "關於",
    "關心", "關鍵", "任何", "任務", "顧問", "顧客", "紀錄", "顏色", "游泳", "謝謝", "張貼", "官方",
    "尤其", "嚴格", "歐洲", "童年", "韓國", "藍色", "溫度", "萬一", "史上", "段落", "龍頭", "毛利",
    "易用", "卓越", "葉子", "羅列", "賴皮", "洪水", "林業", "陳列", "陸續", "夏天", "錢包", "凌晨",
    "辛苦", "武器", "莫名", "利潤", "房子", "左右", "華人", "花了", "花錢", "古早", "黎明", "湯匙",
    "孫子", "宋朝", "柯南", "康復", "田野", "易於", "向來", "包裝", "梅花", "華為", "雷同", "雷射",
}
#: 「陳總」「林董」「王姐」這種姓氏 ＋ 稱呼，兩個字也是名字
_TITLE_CHARS = set("總董姐哥兄嫂伯叔爺桑媽")
#: 名字的最後一個字不會是這些（「高興啦」「方便的」「白做了」）
_PARTICLES = set("的了啦嗎呢吧喔哦啊呀囉嘿唷哈耶欸嘛咧")

_LATIN = r"[A-Z][a-zA-Z'\-]{1,19}"
_LATIN_NAME = re.compile(rf"^({_LATIN}(?:\s{_LATIN})?)")
_LATIN_TAIL = re.compile(rf"({_LATIN}(?:\s{_LATIN})?)\s*$")

_EN_INTRO = re.compile(
    rf"\b(?:I am|I'm|I’m|my name is|My name is|MY NAME IS)\s+({_LATIN}(?:\s{_LATIN})?)"
    r"(?=\s*(?:$|[,.;:!?]|\s+(?:from|here|and|speaking|calling)\b))")
_EN_THIS_IS = re.compile(
    rf"\b(?:[Tt]his is|[Ii]t'?s|[Ii]t’s)\s+({_LATIN}(?:\s{_LATIN})?)"
    r"(?=\s+(?:from|here|speaking|calling)\b)")
_JA_INTRO = re.compile(r"([一-龯ァ-ヶー]{2,8})(?:と申します|といいます)")

_CJK = re.compile(r"[一-鿿]+")


def _name_ends(rest: str, allow_cont: bool) -> bool:
    """名字後面那段話說得通嗎：句讀 / 句尾 / 語助詞，或（只限「我是」）常見的下一句開頭。"""
    r = rest.lstrip()
    if _TERM.match(r):
        return True
    return allow_cont and r.startswith(_CONT)


def _cjk_name(s: str, *, explicit: bool, greeted: bool) -> Optional[str]:
    """`s` 開頭是不是一個中文名字（後面要接得上 `_name_ends`）—— 是就回那個名字。"""
    allow_cont = not explicit
    two_ok = explicit or greeted
    #: (候選, 應有的字數) —— **字數要剛好**：`s[:3]` 在只剩兩個字的字串上拿到的是兩個字，
    #: 不檢查的話「我是游客。」（句號先被切掉）會繞過兩個字的限制
    cands: list[tuple[str, int]] = []
    if s[:2] in _COMPOUND:                   # 歐陽、司馬…：名字三到四個字
        cands += [(s[:4], 4), (s[:3], 3)]
    elif s[:1] in ("小", "老") and s[1:2] in _SURNAMES:
        cands.append((s[:2], 2))             # 小王、老陳
    elif s[:1] == "阿" and two_ok:
        cands.append((s[:2], 2))             # 阿明（只在明確報名或有問候時）
    elif s[:1] in _SURNAMES and s[:2] not in _WORD_PREFIX:
        cands.append((s[:3], 3))
        if two_ok or s[1:2] in _TITLE_CHARS:
            cands.append((s[:2], 2))
    for cand, n in cands:
        if len(cand) != n or not _CJK.fullmatch(cand) or cand[-1] in _PARTICLES:
            continue
        if _name_ends(s[n:], allow_cont):
            return cand
    return None


def _latin_ok(name: str) -> bool:
    return all(w.lower() not in _EN_NOT_NAMES for w in name.split())


def _from_zh(text: str) -> list[str]:
    out: list[str] = []
    greeted = bool(_GREETING.search(text))
    for m in _ZH_MARKERS.finditer(text):
        explicit = m.group(0) in _EXPLICIT
        tail = text[m.end():]
        end = _SENT_END.search(tail)
        if end:
            tail = tail[:end.start()]
        tail = _FILLERS.sub("", tail)
        if not tail:
            continue
        # 一、開頭直接就是名字（「我是 Wendy」「我叫王小明」）
        lm = _LATIN_NAME.match(tail)
        if lm and _latin_ok(lm.group(1)) and _name_ends(tail[lm.end():], not explicit):
            out.append(lm.group(1))
            continue
        cj = _cjk_name(tail, explicit=explicit, greeted=greeted)
        if cj:
            out.append(cj)
            continue
        # 二、先講單位職稱、名字放在這一段的最後（「我是範例科技這邊的業務 Wendy，…」）
        clause = re.split(r"[，、,]", tail, maxsplit=1)[0].rstrip()
        if len(clause) > 30:                 # 太長就不是一句自我介紹了
            continue
        tm = _LATIN_TAIL.search(clause)
        if tm and _latin_ok(tm.group(1)):
            if clause[:tm.start()].rstrip().endswith(_TITLES):
                out.append(tm.group(1))
                continue
        for t in _TITLES:                    # 中文名字：「…的業務王小明」
            k = clause.rfind(t)
            if k >= 0:
                after = clause[k + len(t):].lstrip()
                cj = _cjk_name(after, explicit=True, greeted=greeted)
                if cj and after == cj:
                    out.append(cj)
                    break
    return out


def names_in(text: str) -> list[str]:
    """一段話裡自我介紹報出來的名字（照出現順序）。"""
    if not text:
        return []
    t = str(text)
    found = _from_zh(t)
    found += [m.group(1) for m in _EN_INTRO.finditer(t) if _latin_ok(m.group(1))]
    found += [m.group(1) for m in _EN_THIS_IS.finditer(t) if _latin_ok(m.group(1))]
    found += [m.group(1) for m in _JA_INTRO.finditer(t)]
    seen: list[str] = []
    for n in found:
        if n not in seen:
            seen.append(n)
    return seen


def hints(segments: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """每一位發言者（代號）可能的名字：

    `{"S1": {"name": "Wendy", "seq": 13, "text": "…我是…Wendy", "count": 2, "others": []}}`

    `text` 是那一段的節錄（給滑過時看「是從哪一句認出來的」）。沒有發言者代號的段落不看。
    """
    found: dict[str, dict[str, dict[str, Any]]] = {}
    for s in segments or []:
        if not isinstance(s, dict):
            continue
        code = s.get("speaker")
        if not code or not isinstance(code, str):
            continue
        for name in names_in(s.get("text") or ""):
            if name == code:
                continue
            per = found.setdefault(code, {})
            if name not in per:
                txt = str(s.get("text") or "")
                per[name] = {"name": name, "seq": s.get("seq"), "count": 0,
                             "text": txt if len(txt) <= 80 else txt[:79] + "…"}
            per[name]["count"] += 1
    out: dict[str, dict[str, Any]] = {}
    for code, per in found.items():
        ranked = sorted(per.values(), key=lambda h: (-h["count"],
                                                     h["seq"] if isinstance(h["seq"], int) else 0))
        best = dict(ranked[0])
        best["others"] = [h["name"] for h in ranked[1:]]
        out[code] = best
    return out
