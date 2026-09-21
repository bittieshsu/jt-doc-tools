"""把產出跟標準答案對起來 —— **用引用的段號比對，不用文字比對**。

## 為什麼用段號

「這條決議跟那條算不算同一條」用字串比會永遠吵不完（同義改寫、長短不一），
而且**比對規則一旦模糊，分數就可以被解釋成任何樣子**。

引用哪幾段是**機械可驗的**：語料裡每一條標準答案都標了它由哪幾段支持，
產出的每一條也必須附 `segment_ids`。兩邊有交集就是同一條。

## 四個數字

| 指標 | 意思 | 錯的話使用者會怎樣 |
|---|---|---|
| **抓到率** | 標準答案被抓到幾條 | 會議決議了，摘要沒寫 —— 沒人會發現 |
| **捏造率** | 產出裡有幾條沒有依據 | 摘要寫了一條沒發生的事，而且看起來很合理 |
| **引用精準度** | 引用的段落裡，真的是依據的比例 | 點進去跳到不相干的地方，信任一次就沒了 |
| **內容檢查** | 被推翻的決議有沒有復活、專有名詞有沒有被改掉… | 逐條在語料裡標好的陷阱 |

**捏造率比抓到率重要。** 漏一條的後果是「少一條」；捏造一條的後果是
**使用者照著一件沒發生的事去做**，而且他沒有理由懷疑。
"""
from __future__ import annotations

from dataclasses import dataclass, field

_KINDS = ("decisions", "actions", "risks", "questions")

#: 否定詞。判斷「被推翻的選項」是被**主張**還是被**提到**。
_NEG = ("不", "沒", "否", "未", "取消", "放棄", "改用", "排除", "改成", "撤")
_NEG_WINDOW = 10


def _asserted(text: str, term: str) -> bool:
    """`term` 在 `text` 裡有沒有被**當成結論主張**（而不是被否定地提到）。

    **這是啟發式的，而且只抓得到「附近有沒有否定詞」。**
    寫成子字串比對的話會誤報 —— 正確的摘要一定會提到被否決的選項
    （「**不用**永昌的方案」），而 use vs mention 分不出來的檢查
    會一直亮紅燈，然後被當成雜訊忽略。

    抓不到的：跨句的否定（「永昌那家…我們最後沒有選」）。
    那一類要靠 `context` 與人看。
    """
    i = 0
    while True:
        i = text.find(term, i)
        if i < 0:
            return False
        near = text[max(0, i - _NEG_WINDOW): i + len(term) + _NEG_WINDOW]
        if not any(n in near for n in _NEG):
            return True          # 出現了，而且附近沒有否定 → 被主張
        i += len(term)


@dataclass
class Report:
    per_kind: dict = field(default_factory=dict)
    content_fails: list[str] = field(default_factory=list)

    @property
    def totals(self) -> dict:
        hit = sum(k["hit"] for k in self.per_kind.values())
        want = sum(k["want"] for k in self.per_kind.values())
        fab = sum(k["fabricated"] for k in self.per_kind.values())
        mis = sum(k["misclassified"] for k in self.per_kind.values())
        acc = sum(k["acceptable"] for k in self.per_kind.values())
        got = sum(k["got"] for k in self.per_kind.values())
        prec = [p for k in self.per_kind.values() for p in k["cite_precision"]]
        return {
            "recall": hit / want if want else 1.0,
            "fabrication": fab / got if got else 0.0,
            "cite_precision": sum(prec) / len(prec) if prec else 0.0,
            "misclassified": mis, "acceptable": acc,
            "hit": hit, "want": want, "fabricated": fab, "got": got,
            "content_fails": len(self.content_fails),
        }

    def ok(self) -> bool:
        t = self.totals
        return (t["recall"] >= 1.0 and t["fabrication"] <= 0.0
                and not self.content_fails)


def _ids(item: dict) -> set[int]:
    raw = item.get("segment_ids") or item.get("support") or []
    out = set()
    for v in raw:
        try:
            out.add(int(v))
        except (TypeError, ValueError):
            continue
    return out


def _text(item: dict) -> str:
    for k in ("text", "gist", "title", "summary", "content"):
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def _acceptable(gi: set[int], kind: str, truth: dict) -> bool:
    """這一條雖然不在標準答案裡，但**能幹的記錄者可能也會寫**嗎？

    **不計入抓到率**，只是不算捏造 —— 否則會為了追求分數，
    把模型合理的行為一起調掉。
    """
    for a in truth.get("acceptable") or []:
        if a.get("kind") == kind and gi & set(a["support"]):
            return True
    return False


def _grams(s: str) -> set[str]:
    s = "".join(ch for ch in (s or "") if not ch.isspace())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _match(gi: set[int], want_items: list[dict],
           text: str = "") -> tuple[int | None, int]:
    """引用重疊優先；**重疊一樣時用文字相似度分辨**。

    語料裡「陳經理確認機房電力」與「林工程師負責網路」是**同一句話**裡的
    兩件事，引用的段號完全相同 —— 只比引用的話分不出誰是誰，
    兩條產出會撞到同一條答案，另一條就被誤報成漏掉。
    """
    tg = _grams(text)
    best, best_overlap, best_sim = None, 0, -1.0
    for idx, wt in enumerate(want_items):
        ov = len(gi & set(wt["support"]))
        if ov == 0:
            continue
        wg = _grams(wt.get("gist", ""))
        sim = len(tg & wg) / len(tg | wg) if (tg and wg) else 0.0
        if ov > best_overlap or (ov == best_overlap and sim > best_sim):
            best, best_overlap, best_sim = idx, ov, sim
    return best, best_overlap


def score(truth: dict, produced: dict) -> Report:
    """**分類錯不算捏造。**

    把「這件事該做」寫成未決問題，跟憑空生出一件沒發生的事，是兩種完全不同
    的失敗：前者改提示裡的定義就好，後者是信任問題。混成一個數字的話，
    「捏造率」會被分類錯誤灌水，真正的捏造反而看不見。
    """
    rep = Report()
    all_text = " ".join(_text(it) for kind in _KINDS
                        for it in (produced.get(kind) or []))

    for kind in _KINDS:
        want_items = truth.get(kind) or []
        got_items = produced.get(kind) or []
        matched_truth: set[int] = set()
        fabricated = 0
        misclassified = 0
        acceptable = 0
        cite_precision: list[float] = []

        for got in got_items:
            gi = _ids(got)
            best, best_overlap = _match(gi, want_items, _text(got))
            if best is None:
                # 是不是分到別類去了？（引用對得上，只是類別錯）
                other = None
                for ok in _KINDS:
                    if ok == kind:
                        continue
                    idx2, ov2 = _match(gi, truth.get(ok) or [], _text(got))
                    if idx2 is not None:
                        other = (ok, (truth[ok][idx2])["gist"])
                        break
                if _acceptable(gi, kind, truth):
                    acceptable += 1
                    continue
                if other:
                    misclassified += 1
                    rep.content_fails.append(
                        f"[{kind}] 「{other[1]}」應該是 {other[0]}，卻被歸到 {kind}")
                else:
                    fabricated += 1
                continue
            matched_truth.add(best)
            # **引用完整不該被罰**：算精準度時把 context 也視為合格的依據，
            # 但抓到與否只看 support（只引用到提案不算抓到那條決議）。
            okset = set(want_items[best]["support"]) | set(want_items[best].get("context") or [])
            cite_precision.append(len(gi & okset) / len(gi) if gi else 0.0)

            wt = want_items[best]
            txt = _text(got)
            for must in wt.get("must_keep", []):
                if must.lower() not in txt.lower():
                    rep.content_fails.append(
                        f"[{kind}] 「{wt['gist']}」的產出把「{must}」弄丟了：{txt[:60]}")
            if kind == "actions":
                if "owner" in wt:
                    go = got.get("owner")
                    if wt["owner"] is None and go:
                        rep.content_fails.append(
                            f"[actions] 「{wt['gist']}」語料沒有講負責人，"
                            f"產出卻指派了 {go!r} —— 那個人不知道自己被指派了")
                    elif wt["owner"] and go != wt["owner"]:
                        rep.content_fails.append(
                            f"[actions] 「{wt['gist']}」負責人應為 {wt['owner']}，得到 {go!r}")
                if wt.get("due_text"):
                    blob = f"{got.get('due_text') or ''} {txt}"
                    if wt["due_text"] not in blob:
                        rep.content_fails.append(
                            f"[actions] 「{wt['gist']}」期限原文「{wt['due_text']}」"
                            f"沒有保留（得到 {got.get('due_text')!r}）")

        for wt in want_items:
            for bad in wt.get("must_not_say", []):
                if _asserted(all_text, bad):
                    rep.content_fails.append(
                        f"[{kind}] 被推翻／否決的選項被當成結論寫出來了：「{bad}」")

        rep.per_kind[kind] = {
            "want": len(want_items), "got": len(got_items),
            "hit": len(matched_truth), "fabricated": fabricated,
            "misclassified": misclassified, "acceptable": acceptable,
            "missed": [want_items[i]["gist"] for i in range(len(want_items))
                       if i not in matched_truth],
            "cite_precision": cite_precision,
        }
    return rep


def render(rep: Report) -> str:
    t = rep.totals
    lines = [
        f"抓到率 {t['recall']:.0%}（{t['hit']}/{t['want']}）　"
        f"捏造率 {t['fabrication']:.0%}（{t['fabricated']}/{t['got']}）　"
        f"分類錯 {t['misclassified']}　可接受 {t['acceptable']}　"
        f"引用精準度 {t['cite_precision']:.0%}",
    ]
    for kind, k in rep.per_kind.items():
        lines.append(f"  {kind:10s} 答案 {k['want']} / 產出 {k['got']} / "
                     f"抓到 {k['hit']} / 捏造 {k['fabricated']} / "
                     f"分類錯 {k['misclassified']}")
        for m in k["missed"]:
            lines.append(f"      ✗ 漏掉：{m}")
    for f in rep.content_fails:
        lines.append(f"  ⚠ {f}")
    return "\n".join(lines)
