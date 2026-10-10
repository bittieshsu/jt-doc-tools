"""客戶公司名稱、客戶人名、真實會議裡的詞，不可以出現在會公開的檔案裡（2026-10-03 使用者要求）。

程式、註解、測試、文件、介面上的範例文字都算 —— 寫測試時順手拿真實會議裡的名字與聽錯的詞
當素材，就會跟著同步出去。名單在開發樹的私有目錄（`tools/check_private_names.py` 讀它），
**這份測試與那支程式都不含任何名字**；公開的 clone 上沒有名單，這裡整份略過。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools import check_private_names as cpn            # noqa: E402
from tools.repo_paths import is_development_tree, public_root  # noqa: E402

#: 開發樹裡會被同步出去的來源（跟 `sync-to-github.sh` 的 ITEMS 對齊）
_SOURCES = ("app", "static", "tests", "tools", "scripts", "TEST_PLAN.md", "TEST_PLAN_SECURITY.md")

pytestmark = pytest.mark.skipif(not is_development_tree(ROOT),
                                reason="公開的 clone 上沒有名單（名單只在開發樹）")


def _publish_roots() -> list[Path]:
    """同步之後會公開的內容：開發樹的來源 ＋ 公開樹**自己的**檔案。

    公開樹裡那幾份同步過去的副本（`github/app` 等）不看 —— 那是上一次同步的結果，
    下一次同步會整份換掉；那一份由同步腳本在推送前另外掃。"""
    gh = public_root(ROOT)
    roots = [ROOT / s for s in _SOURCES]
    roots += [p for p in gh.iterdir() if p.name not in _SOURCES and p.name != ".git"]
    return roots


def test_the_list_exists_in_the_development_tree():
    """名單不見的話，檢查會安靜地什麼都不做（「沒有名單，略過」）。"""
    pats = cpn.load_patterns()
    assert len(pats) >= 10, f"名單只有 {len(pats)} 條 —— 檔案不見了或被清空了？"


def test_nothing_on_the_list_is_in_what_gets_published():
    hits = cpn.scan(_publish_roots(), cpn.load_patterns())
    shown = [f"{f.relative_to(ROOT).as_posix()}:{n}" for f, n in hits]
    assert not shown, ("會公開的檔案裡有客戶名稱、人名或真實會議裡的詞（換成虛構的，"
                       "例如 Acme / Kevin / Bianca）：\n" + "\n".join(shown[:50]))


def test_the_scan_reaches_the_published_files():
    """「掃 0 個檔」跟「掃過都乾淨」長得一樣 —— 先證明真的掃到東西。"""
    files = list(cpn.iter_files(_publish_roots()))
    names = {f.relative_to(ROOT).as_posix() for f in files}
    assert len(files) > 500, len(files)
    for must in ("app/core/meeting_insight.py", "tests/test_term_fix.py",
                 "github/CHANGELOG.md", "github/API.md", "github/docs/api.html"):
        assert must in names, f"沒有掃到 {must}"


def test_the_checker_finds_a_planted_word(tmp_path):
    """反向對照：名單上的字放進一個檔案，要抓得到（而且只報檔案與行號）。"""
    plain = [p.pattern for p in cpn.load_patterns() if re.escape(p.pattern) == p.pattern]
    assert plain, "名單裡沒有可以直接當字串用的條目"
    f = tmp_path / "x.md"
    f.write_text("第一行\n範例：" + plain[0] + " 的報價\n", encoding="utf-8")
    assert cpn.scan([tmp_path], cpn.load_patterns()) == [(f, 2)]
    # 第三方原始碼（vendor）不算
    v = tmp_path / "vendor"
    v.mkdir()
    (v / "y.js").write_text(plain[0], encoding="utf-8")
    assert cpn.scan([tmp_path], cpn.load_patterns()) == [(f, 2)]


def test_the_sync_script_checks_before_publishing():
    """同步腳本推送前要跑這支、有命中就停 —— 測試只看開發樹的來源，同步過去的那一份靠它。"""
    src = (ROOT / "sync-to-github.sh").read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert re.search(r'check_private_names\.py"?\s+"\$GH"', code), "同步腳本沒有掃公開樹"
    assert "exit 1" in code[code.index("check_private_names"):], "命中了也沒有停下來"


# ── 內部主機代號（2026-10-10 使用者核准）─────────────────────────────────
# 程式註解、測試、測試計畫、更新記錄原本會寫內部機器的代號（IP 最後一段、機器名稱）。
# 既有的「不可以有內網 IP」檢查只認完整的位址，抓不到簡寫，所以每一版都會再帶進去幾處。
# 代號放在開發樹的另一份私有清單（不是名單那一份：名單也給合作對象的推送前檢查讀，
# 他們的文件會寫他們自己機器的代號）。**這裡不寫任何代號**，正反兩面的樣本都從私有清單推出來。

def _sample_codes() -> set[str]:
    codes = set()
    for s in cpn.host_samples():
        codes.update(re.findall(r"(?<![0-9])\.(\d{2,3})(?![0-9])", s))
    return codes


def test_the_host_list_exists_in_the_development_tree():
    assert len(cpn.load_host_patterns()) >= 4, "主機代號清單不見了或被清空了？"
    assert len(cpn.host_samples()) >= 5, "主機代號清單裡沒有測試樣本 —— 下面兩條會變成什麼都沒驗"
    assert len(_sample_codes()) >= 3


def test_every_sample_in_the_host_list_is_caught():
    """式子寫錯一個字元就會安靜地什麼都擋不到 —— 拿清單自己附的樣本驗。"""
    pats = cpn.load_host_patterns()
    missed = [s for s in cpn.host_samples() if not any(p.search(s) for p in pats)]
    assert not missed, f"這些樣本沒有被擋下：{missed}"


def test_ordinary_numbers_are_not_mistaken_for_host_codes():
    """反向對照：版本號、小數、CSS 與 SVG 的透明度、完整 IP 的最後一段都不算。
    擋太寬的話更新記錄的版本號會整片被擋，這條檢查就會被關掉。"""
    pats = cpn.load_host_patterns()
    for n in sorted(_sample_codes()):
        for text in (f"v1.16.{n}", f"0.{n}", f"KW_MIN = 0.{n}", f"rgba(15, 23, 42, .{n})",
                     f'fill-opacity=".{n}"', f"約 1.{n} 秒"):
            assert not any(p.search(text) for p in pats), f"誤判：{text!r}"


def test_no_internal_host_code_is_in_what_gets_published():
    hits = cpn.scan(_publish_roots(), cpn.load_host_patterns())
    shown = [f"{f.relative_to(ROOT).as_posix()}:{n}" for f, n in hits]
    assert not shown, ("會公開的檔案裡寫了內部主機代號（改成「正式機」「Windows 實機」"
                       "「開發機」「推論機」這類說法）：\n" + "\n".join(shown[:50]))


def test_the_sync_gate_reads_the_host_list_too():
    """推送前那一道（同步腳本跑的 main）也要擋主機代號，不只測試擋。"""
    import inspect
    src = inspect.getsource(cpn.main)
    assert "load_host_patterns()" in src
