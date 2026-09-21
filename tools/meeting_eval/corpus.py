"""合成會議語料 —— **每一條決議與待辦都有標準答案**。

## 設計

一場會議 = 一串 **beat**（有訊號的片段）＋ 中間塞的**雜訊**（真的會出現、
但不該產生任何項目的對話）。這跟表單回歸的合成樣本是同一個思路：
**真實語料缺的版型要自己補**，不然那一類永遠測不到。

**雜訊不是湊數的。** 摘要最常見的失敗不是「漏掉」而是「把閒聊當成決議」，
所以雜訊要像真的會議：離題、重複、講一半改口、報數字但不是結論。

## 刻意放進去的陷阱（每一個都對應一種真實的錯法）

| # | 陷阱 | 錯的話會怎樣 |
|---|---|---|
| 1 | **決議後來被推翻** | 摘要報第一個決定 —— 使用者照著做，而會議其實改了 |
| 2 | 意見不是決議（「我覺得應該…」沒有結論） | 憑空多一條決議 |
| 3 | 提案被否決 | 否決的提案變成決議 |
| 4 | 待辦**沒有講負責人** | 模型指派一個人 —— 那個人不知道自己被指派了 |
| 5 | 相對期限（「下週三前」） | 轉成錯的日期，或把原文丟掉 |
| 6 | 中英混雜 | 專有名詞被翻掉或被拆開 |
| 7 | 未決問題 | 被寫成已經決定 |
| 8 | 風險提醒 | 混進決議裡 |
| 9 | 純閒聊 | 產生任何項目都是錯 |
| 10 | 決議**跨三個發言**（提案→反對→主席裁示） | 只引用到提案那一句，漏掉真正的結論 |
| 11 | 順口提到的數字 | 被接到不相干的項目上 |

判準用 `segment_ids` 對，不用文字比對 —— **引用哪幾段是機械可驗的**，
而「這段文字算不算同一條決議」用字串比會一直吵。
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Turn:
    speaker: str
    text: str


@dataclass
class Beat:
    """一段有意義的對話。索引都是這個 beat 內的 turn 索引（0 起）。

    **`support` 與 `context` 是兩種不同的依據**，分開才量得準：

    * `support` —— **決定性**的那幾段。產出必須引用到其中至少一段才算抓到。
      「提案 → 反對 → 主席裁示」裡，只引用到提案是**錯的**，
      因為那時候還沒有決議。
    * `context` —— 引用了也很合理的周邊段落（理由、附和、補充）。
      **不算抓到、但也不扣引用精準度** —— 否則「引用完整一點」反而被罰，
      而那正是我們希望它做的事。
    """
    name: str
    turns: list[Turn]
    decisions: list[dict] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    risks: list[dict] = field(default_factory=list)
    questions: list[dict] = field(default_factory=list)
    #: **可以接受但不強求**的項目：能幹的記錄者*可能*會寫，寫了不算捏造。
    #: **不計入抓到率** —— 所以它不能拿來灌水，只能避免把合理的行為判成錯。
    #: 每一條都要寫清楚為什麼它站得住腳，否則這一欄會變成掩蓋失敗的地方。
    acceptable: list[dict] = field(default_factory=list)
    #: 這個 beat 屬於哪個議題。**真實會議會連續談同一個主題好幾分鐘**，
    #: beat 散在雜訊之間的話，「正確的」章節本來就是交替的 ——
    #: 那樣章節切得好不好根本量不出來。
    topic: str = "其他"
    note: str = ""


S1, S2, S3, S4 = "speaker_1", "speaker_2", "speaker_3", "speaker_4"

# ----------------------------------------------------------------- beats

def _beats() -> list[Beat]:
    return [
        Beat(
            name="決議：跨三個發言才成立",
            topic="系統調整",
            note="陷阱 10 —— 提案、反對、主席裁示；真正的結論在最後一句",
            turns=[
                Turn(S2, "我建議這一版先不要動資料庫結構，風險太高。"),
                Turn(S3, "可是不動的話，那個查詢還是會慢，客戶已經反映兩次了。"),
                Turn(S2, "慢是慢，但改結構要停機，這個月有稽核。"),
                Turn(S1, "那這樣，這一版先加索引不改結構，下個版本再處理停機那件事。就這樣定案。"),
            ],
            decisions=[{"gist": "這一版只加索引、不改資料庫結構", "support": [3],
                        "context": [0, 1, 2]}],
            acceptable=[{"kind": "risks", "support": [2],
                         "why": "「改結構要停機，這個月有稽核」講出了後果，"
                                "記成風險站得住腳"}],
        ),
        Beat(
            name="決議後來被推翻",
            topic="採購進度",
            note="陷阱 1 —— 最重要的一個。前面決定用 A 廠商，後面改成 B",
            turns=[
                Turn(S1, "採購那邊，先前初步是傾向用永昌的方案。"),
                Turn(S4, "我要補充一下，永昌那邊回覆說最快要十二月才能交機。"),
                Turn(S1, "十二月？那來不及。"),
                Turn(S4, "立信可以十月中，價格高一點大概多八萬。"),
                Turn(S1, "那改用立信，多八萬可以接受，時程比較重要。永昌那個就不用了。"),
            ],
            decisions=[{"gist": "改用立信、不用永昌", "support": [4], "context": [1, 3],
                        "must_not_say": ["永昌"]}],
            acceptable=[{"kind": "risks", "support": [1, 2],
                         "why": "「永昌最快十二月交機、那來不及」講出了後果，"
                                "記成風險（解釋為什麼改廠商）站得住腳"}],
        ),
        Beat(
            name="意見不是決議",
            topic="系統調整",
            note="陷阱 2 —— 只有想法沒有結論，不可以產生任何決議",
            turns=[
                Turn(S3, "我個人覺得那個流程應該可以再簡化一點啦。"),
                Turn(S2, "嗯，是有討論的空間。"),
                Turn(S3, "不過這個可能要再想想，我先不下定論。"),
            ],
            acceptable=[{"kind": "questions", "support": [2],
                         "why": "「我先不下定論」字面就是還沒有結論，"
                                "記成未決問題站得住腳"}],
        ),
        Beat(
            name="提案被否決",
            topic="系統調整",
            note="陷阱 3 —— 明確否決；否決的提案不可以變成決議",
            turns=[
                Turn(S2, "有沒有可能我們這次直接跳過測試環境，時間比較省？"),
                Turn(S1, "不行，上次就是這樣出事的。測試環境一定要過。"),
            ],
            decisions=[{"gist": "不跳過測試環境", "support": [1], "context": [0]}],
        ),
        Beat(
            name="待辦沒有負責人",
            topic="系統調整",
            note="陷阱 4 —— 沒有講是誰，負責人必須留空不可以猜",
            turns=[
                Turn(S1, "那個授權數量要跟原廠再確認一次，不要到時候不夠。"),
                Turn(S3, "好。"),
            ],
            actions=[{"gist": "跟原廠確認授權數量", "support": [0], "owner": None}],
        ),
        Beat(
            name="待辦有負責人與相對期限",
            topic="採購進度",
            note="陷阱 5 —— 期限留原文；負責人用逐字稿裡的稱呼（分析當下不知道誰是 speaker_4）",
            turns=[
                Turn(S1, "陳經理，報價單請你下週三之前給我。"),
                Turn(S4, "沒問題，下週三之前。"),
            ],
            actions=[{"gist": "提供報價單", "support": [0], "context": [1], "owner": "陳經理",
                      "due_text": "下週三"}],
        ),
        Beat(
            name="中英混雜",
            topic="系統調整",
            note="陷阱 6 —— 專有名詞不可以被翻掉或拆開",
            turns=[
                Turn(S3, "那個 API 的 rate limit 目前是每分鐘六十次，批次一跑就爆掉。"),
                Turn(S1, "那就請他們調高，我們這邊先做 retry。就這麼辦。"),
            ],
            decisions=[{"gist": "請對方調高 rate limit，我方先做 retry",
                        "support": [1], "context": [0], "must_keep": ["rate limit"]}],
            acceptable=[{"kind": "risks", "support": [0],
                         "why": "「rate limit 每分鐘六十次，批次一跑就爆掉」"
                                "講出了後果，記成風險站得住腳"}],
        ),
        Beat(
            name="未決問題",
            topic="系統調整",
            note="陷阱 7 —— 明講「還沒有答案」，不可以寫成已決定",
            turns=[
                Turn(S2, "那舊系統的資料要不要一起搬？"),
                Turn(S1, "這個我還沒有答案，要看法務那邊怎麼說，下次再談。"),
            ],
            questions=[{"gist": "舊系統資料是否一併移轉", "support": [0, 1]}],
        ),
        Beat(
            name="風險",
            topic="採購進度",
            note="陷阱 8 —— 是風險不是決議",
            turns=[
                Turn(S4, "我提醒一下，如果十月中沒有交機，後面的教育訓練整個要往後延。"),
                Turn(S1, "了解，這個記一下。"),
            ],
            risks=[{"gist": "交機延誤會連帶影響教育訓練時程", "support": [0], "context": [1]}],
        ),
        Beat(
            name="順口提到的數字",
            topic="採購進度",
            note="陷阱 11 —— 三百萬是去年的總額，不是這次的預算",
            turns=[
                Turn(S4, "順帶一提，去年這個項目整年花了大概三百萬。"),
                Turn(S1, "今年的還沒核下來，等核定再說。"),
            ],
            acceptable=[{"kind": "questions", "support": [1],
                         "why": "今年預算尚未核定、等核定再談 —— 確實懸而未決"}],
        ),
        Beat(
            name="假設語氣不是決議",
            topic="其他事項",
            note="**最陰險的一種** —— 句子裡有「決定」兩個字，但那是假設",
            turns=[
                Turn(S2, "如果我們決定用外包的話，那合約要重寫一次。"),
                Turn(S1, "那是另一個議題，今天先不談。"),
            ],
        ),
        Beat(
            name="轉述別人的話不是我方決議",
            topic="採購進度",
            note="陷阱 —— 客戶的決定不是我們的決議",
            turns=[
                Turn(S4, "客戶那邊說他們決定先上兩個廠區就好。"),
                Turn(S1, "嗯，那我們的排程要跟著調。"),
            ],
        ),
        Beat(
            name="決議：附條件",
            topic="教育訓練",
            note="條件要跟著一起寫出來，只寫結論會誤導",
            turns=[
                Turn(S3, "如果原廠十月底前給得出中文介面，我們就不自己翻。"),
                Turn(S1, "可以，就這樣定 —— 十月底前有中文介面就不自己翻，"
                         "沒有的話我們自己來。"),
            ],
            decisions=[{"gist": "原廠十月底前給中文介面就不自翻，否則自翻",
                        "support": [1], "context": [0]}],
        ),
        Beat(
            name="決議：帶金額",
            topic="採購進度",
            note="數字不可以抄錯，也不可以從別處抓一個數字過來",
            turns=[
                Turn(S4, "維護費那邊對方開一年三十二萬。"),
                Turn(S1, "三十二萬可以，就簽一年。"),
            ],
            decisions=[{"gist": "維護費一年三十二萬、簽一年", "support": [1],
                        "context": [0], "must_keep": ["三十二萬"]}],
        ),
        Beat(
            name="待辦：明確日期",
            topic="其他事項",
            note="這個是絕對日期不是相對的，due_text 要照抄",
            turns=[
                Turn(S1, "測試報告麻煩在十月三號以前給我。"),
                Turn(S3, "好，十月三號。"),
            ],
            actions=[{"gist": "提供測試報告", "support": [0], "context": [1],
                      "owner": None, "due_text": "十月三號"}],
        ),
        Beat(
            name="待辦：兩個人各做一件",
            topic="其他事項",
            note="不可以併成一條，也不可以把負責人搞混",
            turns=[
                Turn(S1, "這樣，機房那邊請陳經理去確認電力，"
                         "網路的部分林工程師接。"),
                Turn(S4, "好。"),
            ],
            actions=[
                {"gist": "確認機房電力", "support": [0], "owner": "陳經理"},
                {"gist": "負責網路的部分", "support": [0], "owner": "林工程師"},
            ],
        ),
        Beat(
            name="風險：外部依賴",
            topic="其他事項",
            note="有後果才算風險",
            turns=[
                Turn(S3, "那個介面要等對方的窗口回來才做得下去，"
                         "他下週才銷假，整個會壓到月底。"),
                Turn(S1, "了解。"),
            ],
            risks=[{"gist": "對方窗口下週才回，介面進度會壓到月底",
                    "support": [0]}],
        ),
        Beat(
            name="未決問題：要別人決定",
            topic="教育訓練",
            note="明講要等別人拍板",
            turns=[
                Turn(S2, "教育訓練要辦幾場，這個我沒辦法決定。"),
                Turn(S1, "那等總經理那邊回覆再說。"),
            ],
            questions=[{"gist": "教育訓練要辦幾場", "support": [0, 1]}],
        ),
        Beat(
            name="同一件事後段再確認一次",
            topic="系統調整",
            note="**不是新的決議** —— 去重要把它併進前面那一條",
            turns=[
                Turn(S2, "所以索引那件事就這一版做對吧？"),
                Turn(S1, "對，這一版只加索引。"),
            ],
        ),
        Beat(
            name="兩條相似但不同的決議",
            topic="系統調整",
            note="**去重不可以把它們併掉** —— 一個講測試一個講正式環境",
            turns=[
                Turn(S1, "測試環境的版本我們統一升到 3.2。"),
                Turn(S1, "正式環境這一波先不動，維持 3.0。"),
            ],
            decisions=[
                {"gist": "測試環境升到 3.2", "support": [0], "must_keep": ["3.2"]},
                {"gist": "正式環境維持 3.0 不動", "support": [1],
                 "must_keep": ["3.0"]},
            ],
        ),
        Beat(
            name="純閒聊",
            topic="其他事項",
            note="陷阱 9 —— 產生任何項目都是錯的",
            turns=[
                Turn(S3, "欸中午要吃什麼，樓下那間又排隊。"),
                Turn(S2, "我帶便當。"),
                Turn(S3, "厲害。"),
            ],
        ),
    ]


#: 雜訊 —— 真的會出現在會議裡、但**不該產生任何項目**的話。
#:
#: **雜訊不可以只有二十句在那邊重複** —— 重複的東西模型一眼就忽略，
#: 那等於沒有干擾，長會議就只是短會議變胖。所以用樣板組出有變化的內容：
#: 有數字、有日期、有人名、有進度回報，**每一句單看都像正事**。
_FILLER_FIXED = [
    (S2, "好，我們接著看下一頁。"),
    (S1, "先這樣，有問題我們再回頭討論。"),
    (S4, "抱歉我剛剛沒聽清楚，可以再講一次嗎？"),
    (S3, "等一下，我把畫面分享出來。"),
    (S1, "大家看得到嗎？"),
    (S4, "看得到。"),
    (S3, "嗯嗯。"),
    (S1, "時間關係，這一段我們先跳過。"),
    (S4, "我這邊沒有其他問題。"),
    (S3, "收到。"),
    (S1, "還有人要補充嗎？"),
    (S2, "我先講到這裡。"),
    (S1, "我們繼續。"),
]

_ITEMS = ["報表", "測試案例", "簽核流程", "備份排程", "權限清單", "介面文件",
          "教育訓練教材", "驗收表", "問題追蹤單", "設定檔", "監控告警", "帳號清冊"]
_STATES = ["做到一半", "已經送出去了", "還在等對方回覆", "昨天剛完成",
           "卡在權限沒開", "初稿出來了", "正在核對", "下週才會動"]
_UNITS = ["第一季", "第二季", "第三季", "上個月", "這個月", "下個月", "上週", "這週"]

def _filler_pool(rnd) -> list[tuple[str, str]]:
    """組出有變化的雜訊。每一句單看都像正事，但都不是決議也不是待辦。"""
    out = list(_FILLER_FIXED)
    for _ in range(140):
        kind = rnd.randrange(6)
        sp = rnd.choice([S2, S3, S4])
        if kind == 0:
            out.append((sp, f"{rnd.choice(_ITEMS)}那邊{rnd.choice(_STATES)}。"))
        elif kind == 1:
            out.append((sp, f"{rnd.choice(_UNITS)}的數字是 "
                            f"{rnd.randint(12, 980)}，跟前一期差不多。"))
        elif kind == 2:
            out.append((sp, f"我再確認一下{rnd.choice(_ITEMS)}的版本，"
                            f"印象中是 {rnd.randint(1, 4)}.{rnd.randint(0, 9)}。"))
        elif kind == 3:
            out.append((sp, f"那個{rnd.choice(_ITEMS)}我記得是 "
                            f"{rnd.randint(6, 12)} 月 {rnd.randint(1, 28)} 日那次講的。"))
        elif kind == 4:
            out.append((sp, f"{rnd.choice(_ITEMS)}的部分，"
                            f"目前{rnd.choice(_STATES)}，我先講到這裡。"))
        else:
            # **這一句原本是「這個 X 要不要一起看？還是等 Y 再說」——
            # 那本身就像一個被擱置的問題，拿它當「不該抓的」對模型不公平。**
            # 雜訊的職責是「明確不是項目」，曖昧的句子測到的是別的東西。
            out.append((sp, f"{rnd.choice(_ITEMS)}那邊我{rnd.choice(_UNITS)}"
                            f"看過了，沒什麼特別的。"))
    return out


def _record(beat: Beat, seqs: list[int], truth: dict) -> None:
    for item in beat.acceptable:
        truth["acceptable"].append(
            {**item, "support": [seqs[i] for i in item["support"]],
             "beat": beat.name})
    for kind in ("decisions", "actions", "risks", "questions"):
        for item in getattr(beat, kind):
            rec = dict(item)
            rec["support"] = [seqs[i] for i in item["support"]]
            rec["context"] = [seqs[i] for i in item.get("context", [])]
            rec["beat"] = beat.name
            rec["topic"] = beat.topic
            truth[kind].append(rec)


def build(seed: int = 20260917, filler_ratio: int = 2) -> dict:
    """組一場會議。`filler_ratio` = 每個 beat 之間平均塞幾句雜訊。"""
    rnd = random.Random(seed)
    filler = _filler_pool(rnd)
    segments: list[dict] = []
    truth = {"decisions": [], "actions": [], "risks": [], "questions": [],
             "acceptable": []}
    t = 0  # 毫秒

    def emit(speaker: str, text: str) -> int:
        nonlocal t
        seq = len(segments) + 1
        dur = 900 + len(text) * 160          # 約每字 0.16 秒，接近實際語速
        segments.append({"seq": seq, "start_ms": t, "end_ms": t + dur,
                         "speaker": speaker, "text": text,
                         "language": "zh-Hant", "confidence": 0.93})
        t += dur + rnd.randint(150, 700)
        return seq

    emit(S1, "好，我們開始。今天主要三件事：採購進度、系統調整，還有下個月的教育訓練。")

    # **依議題分組** —— 同一個議題的 beat 連在一起談完再換下一個，
    # 這才是真實會議的樣子，章節切得好不好也才量得出來。
    grouped: dict[str, list[Beat]] = {}
    for b in _beats():
        grouped.setdefault(b.topic, []).append(b)

    blocks = []
    for topic, beats in grouped.items():
        start = len(segments) + 1
        emit(S1, f"好，我們看{topic}這一塊。")
        for beat in beats:
            for _ in range(rnd.randint(0, filler_ratio * 2)):
                sp, tx = rnd.choice(filler)
                emit(sp, tx)
            seqs = [emit(tn.speaker, tn.text) for tn in beat.turns]
            _record(beat, seqs, truth)
        for _ in range(rnd.randint(0, filler_ratio)):
            sp, tx = rnd.choice(filler)
            emit(sp, tx)
        blocks.append({"topic": topic, "start_seq": start,
                       "end_seq": len(segments)})
    truth["blocks"] = blocks

    emit(S1, "好，那今天就到這裡，謝謝大家。")

    return {
        "meeting": {
            "title": "專案進度會議",
            "started_at": "2026-09-17T14:00:00+08:00",
            "timezone": "Asia/Taipei",
            "speakers": [S1, S2, S3, S4],
        },
        "segments": segments,
        "truth": truth,
    }


def build_nothing(seed: int = 424242, turns: int = 160) -> dict:
    """**一場什麼都沒決定的會議。** 標準答案是四類全空。

    這是捏造率最純粹的測試 —— 沒有東西可抓的時候會不會自己生出來。
    實務上這種會議很常見（純進度同步、純簡報），
    **而使用者會直接看摘要**，摘要憑空生出三條決議是災難。
    """
    rnd = random.Random(seed)
    filler = _filler_pool(rnd)
    segments: list[dict] = []
    t = 0

    def emit(sp: str, tx: str) -> None:
        nonlocal t
        dur = 900 + len(tx) * 160
        segments.append({"seq": len(segments) + 1, "start_ms": t,
                         "end_ms": t + dur, "speaker": sp, "text": tx,
                         "language": "zh-Hant", "confidence": 0.93})
        t += dur + rnd.randint(150, 700)

    emit(S1, "今天就是各組同步一下進度，沒有要決定什麼。")
    for _ in range(turns):
        sp, tx = rnd.choice(filler)
        emit(sp, tx)
    emit(S1, "好，那就這樣，謝謝大家。")
    return {"meeting": {"title": "進度同步會議",
                        "started_at": "2026-09-18T10:00:00+08:00",
                        "timezone": "Asia/Taipei",
                        "speakers": [S1, S2, S3, S4]},
            "segments": segments,
            "truth": {"decisions": [], "actions": [], "risks": [],
                      "questions": [], "acceptable": [], "blocks": []}}


def main() -> None:
    out = Path("temp/meeting-eval")
    out.mkdir(parents=True, exist_ok=True)
    for name, kw in (("short", dict(filler_ratio=0)),
                     ("medium", dict(filler_ratio=2)),
                     ("long", dict(filler_ratio=9)),
                     ("xlong", dict(filler_ratio=90))):
        data = build(**kw)
        p = out / f"{name}.json"
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        n = len(data["segments"])
        tr = data["truth"]
        chars = sum(len(s["text"]) for s in data["segments"])
        print(f"{name:7s} {n:4d} 段 / {chars:6,} 字 / "
              f"{data['segments'][-1]['end_ms']/60000:5.1f} 分鐘  "
              f"決議 {len(tr['decisions'])}、待辦 {len(tr['actions'])}、"
              f"風險 {len(tr['risks'])}、未決 {len(tr['questions'])}  → {p}")


def _write_nothing() -> None:
    out = Path("temp/meeting-eval")
    out.mkdir(parents=True, exist_ok=True)
    data = build_nothing()
    p = out / "nothing.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"nothing {len(data['segments']):4d} 段 / "
          f"{sum(len(s['text']) for s in data['segments']):6,} 字 / "
          f"{data['segments'][-1]['end_ms']/60000:5.1f} 分鐘  "
          f"**標準答案：四類全空** → {p}")


if __name__ == "__main__":
    main()
    _write_nothing()
