"""聯絡資訊「欄位名稱：值」的拆法（CodeQL #209、#210：多項式回溯的正規式用在使用者輸入上）。

原本用 `^\\s*([^：:]{1,12}?)\\s*[：:]\\s*(.*)$`：欄位名稱那一組也收得到空白，前後又各接一個 `\\s*`，
CodeQL 判定這種寫法遇到大量空白會回溯很多次。改成不用正規式：找第一個冒號、前面去頭尾空白當欄位名稱。

判準：

* **行為跟原本的正規式一模一樣**（拿原本那一支當對照，在大量隨機輸入上逐一比，含全形空白、Tab、
  只有空白的欄位名稱、超過 12 字、換行在中間或結尾）。
* 很長的惡意輸入瞬間算完。
* `official_doc` 裡不再有那一支正規式（CodeQL 看的就是它）。
"""
from __future__ import annotations

import random
import re
import time

from app.core import official_doc as od

#: 原本的寫法，只留在這裡當對照組
OLD = re.compile(r"^\s*([^：:]{1,12}?)\s*[：:]\s*(.*)$")


def _old(s: str):
    m = OLD.match(s)
    if not m:
        return None
    # 呼叫端拿欄位名稱都是先去掉所有空白再查表，所以比「去掉空白之後」相同即可
    return re.sub(r"\s+", "", m.group(1)), m.group(2)


def _new(s: str):
    got = od._label_value(s)
    if got is None:
        return None
    return re.sub(r"\s+", "", got[0]), got[1]


def test_same_as_the_old_pattern_on_known_cases():
    cases = ["電話：04-1234", "電話 ： 04-1234 ", "  地　　址：臺中市", "E-mail: a@b.c", "：開頭就是冒號",
             "沒有冒號", "十三個字十三個字十三個字十：x", "十二個字十二個字十二個：x", "  ：只有空白的名稱",
             "a:", "a:   ", "a: x\ny", "a: x\n", "a: \n", "a:x\n\n", "\t承辦人\t：\t王小明", "傳真：：02", "a：b：c"]
    for s in cases:
        assert _new(s) == _old(s), (s, _new(s), _old(s))


def test_same_as_the_old_pattern_on_random_input():
    rnd = random.Random(20261011)
    alphabet = [" ", "　", "\t", "\n", "：", ":", "a", "電", "話", "1", "-", "\r"]
    for _ in range(20000):
        s = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 24)))
        assert _new(s) == _old(s), repr(s)


def test_a_long_hostile_line_is_instant():
    t = time.perf_counter()
    for s in ("a" + " " * 200_000 + "x", " " * 200_000, "電" + "　" * 200_000 + "：" + " " * 200_000 + "\nx"):
        od._label_value(s)
        od.split_contact(s, company=True)
        od.clean_contact_fields({"phone": s}, company=True)
    assert time.perf_counter() - t < 1.0


def test_the_flagged_pattern_is_gone():
    src = open(od.__file__, encoding="utf-8").read()
    assert "_CONTACT_LINE_RE" not in src
