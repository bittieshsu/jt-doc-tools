"""量出引用驗證的門檻 —— **不要挑一個好看的數字**。

做法：拿語料裡每一條標準答案，算兩種重疊率

* **真的**：這條的文字 vs 它**真正的依據段落**
* **假的**：同一條文字 vs **隨機抽的其他段落**（依定義就是沒有依據）

兩群分得開的話，門檻取中間；分不開就代表這個判準本身不管用，
要換判準而不是調數字。
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from app.core.meeting_insight import _bigrams


def ratios(corpus: str = "xlong", samples: int = 40, seed: int = 7):
    d = json.loads(Path(f"temp/meeting-eval/{corpus}.json").read_text(encoding="utf-8"))
    by = {s["seq"]: s for s in d["segments"]}
    rnd = random.Random(seed)
    real, fake = [], []

    for kind, items in d["truth"].items():
        for it in items:
            want = _bigrams(it["gist"])
            if not want:
                continue
            sup = set(it["support"]) | set(it.get("context") or [])
            have = set()
            for s in sup:
                have |= _bigrams(by[s]["text"])
            real.append(len(want & have) / len(want))

            others = [q for q in by if q not in sup]
            for _ in range(samples):
                k = rnd.randint(1, 3)
                have2 = set()
                for q in rnd.sample(others, k):
                    have2 |= _bigrams(by[q]["text"])
                fake.append(len(want & have2) / len(want))
    return real, fake


def main() -> None:
    for corpus in ("short", "xlong"):
        real, fake = ratios(corpus)
        real.sort(); fake.sort()
        p = lambda xs, q: xs[min(len(xs) - 1, int(len(xs) * q))]
        print(f"=== {corpus} ===")
        print(f"  真的依據 n={len(real):3d}  最低 {min(real):.2f}  "
              f"5% {p(real,.05):.2f}  中位 {p(real,.5):.2f}")
        print(f"  隨機段落 n={len(fake):3d}  最高 {max(fake):.2f}  "
              f"95% {p(fake,.95):.2f}  99% {p(fake,.99):.2f}  中位 {p(fake,.5):.2f}")
        lo, hi = min(real), p(fake, .99)
        if lo > hi:
            print(f"  ✅ 分得開：真的最低 {lo:.2f} > 假的 99% {hi:.2f}"
                  f"  → 門檻取中間 {(lo + hi) / 2:.2f}")
        else:
            print(f"  ⚠ 重疊：真的最低 {lo:.2f} ≤ 假的 99% {hi:.2f}"
                  f"  → 這個門檻擋不乾淨，要看漏掉多少 / 放過多少")
        for th in (0.10, 0.15, 0.18, 0.25, 0.35, 0.50):
            keep = sum(1 for r in real if r >= th) / len(real)
            pass_ = sum(1 for f in fake if f >= th) / len(fake)
            print(f"    門檻 {th:.2f} → 真的留下 {keep:6.1%}　假的混進來 {pass_:6.1%}")


if __name__ == "__main__":
    main()
