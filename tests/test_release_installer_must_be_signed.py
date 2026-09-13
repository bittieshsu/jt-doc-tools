"""Release 上掛的安裝程式**只能是簽章過的**。

## 由來（v1.15.36，使用者要求）

使用者問「怎麼會出現 release 安裝檔不是簽過的？」。逐支下載驗過之後：

| Release 的 asset | 簽章 |
|---|---|
| `v1.15.26` / `v1.12.82` | Valid，`CN=SignPath Foundation` ＋ DigiCert 時戳 |
| `v1.12.24` / `v1.12.10` / `v1.12.8` | `CN=Test certificate for 'jt-doc-tools [OSS]'`（Windows 驗出來是 `UnknownError`）|

成因不是誰忘了做，是**流程本身不會出聲**：workflow 裡那一步原本叫
「Select release file (signed if present, **else unsigned**)」——
簽章拿不到檔案時就安靜地改掛未簽章版。那是 2026-06 正式憑證還在審核時的
設計，憑證下來之後沒有人回頭改，於是三個舊版本一直掛著測試憑證的 exe。

## 判準

**掃 workflow 的 YAML**：挑檔案那一步不可以有「退回未簽章版」的分支，
而且拿不到簽章檔時要 `exit 1`。

> 這條守的是**發佈流程**不是程式碼 —— 它不會在功能測試裡露臉，
> 但它決定使用者下載到的東西能不能被 Windows 信任。
"""
from __future__ import annotations

import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.repo_paths import public_root  # noqa: E402

WF = public_root(ROOT) / ".github" / "workflows" / "release-windows-installer.yml"


@pytest.fixture(scope="module")
def wf() -> str:
    assert WF.exists(), f"找不到發版 workflow：{WF}"
    return WF.read_text(encoding="utf-8")


def _step(text: str, name_contains: str) -> str:
    """取出某一個 step 的內容（到下一個 `      - name:` 為止）。"""
    m = re.search(rf"^      - name: [^\n]*{re.escape(name_contains)}[^\n]*$",
                  text, re.M)
    assert m, f"workflow 裡找不到名稱含「{name_contains}」的步驟"
    rest = text[m.end():]
    nxt = re.search(r"^      - name: ", rest, re.M)
    return rest[:nxt.start()] if nxt else rest


def test_the_release_step_refuses_to_publish_an_unsigned_build(wf):
    step = _step(wf, "Select release file")
    assert "exit 1" in step, (
        "挑檔案那一步拿不到簽章檔時沒有失敗 —— 這正是 v1.12.8 / v1.12.10 / "
        "v1.12.24 掛著測試憑證 exe 的原因（流程安靜地退回未簽章版）")
    # 退回未簽章的那個分支不可以再出現：判準是「把未簽章的路徑寫進輸出」
    assert not re.search(r'file=\$UNSIGNED', step), (
        "又出現「退回未簽章版」的分支了")


def test_the_step_name_does_not_promise_the_old_behaviour(wf):
    """**訊息不可以承諾與行為不符的事**（本專案反覆踩過）。"""
    assert "else unsigned" not in wf, (
        "步驟名稱還寫著 else unsigned —— 名稱與行為要一致，"
        "不然下一個人會以為它還會退回未簽章版")


def test_only_the_signed_directory_feeds_the_release(wf):
    """掛上 Release 的檔案必須來自簽章輸出目錄。"""
    pick = _step(wf, "Select release file")
    assert "packaging/windows/signed" in pick, "沒有從簽章輸出目錄取檔"
    attach = _step(wf, "Attach to GitHub Release")
    assert "steps.pick.outputs.file" in attach, (
        "掛上 Release 的不是挑檔案那一步的輸出")
    assert "fail_on_unmatched_files: true" in attach, (
        "檔案對不到時要失敗，不可以發一個沒有附件的 Release —— "
        "v1.15.32 就是 tag 看起來好端端的、Release 上卻沒有東西")


def test_the_signing_wait_is_long_enough_for_a_human(wf):
    """OSS 憑證強制人工核准，預設的 10 分鐘等待一定不夠（v1.15.32 踩過）。"""
    m = re.search(r"wait-for-completion-timeout-in-seconds:\s*(\d+)", wf)
    assert m, "找不到簽章等待時間的設定"
    assert int(m.group(1)) >= 3600, (
        f"簽章只等 {m.group(1)} 秒 —— 人工核准可能隔幾小時才發生，"
        "深夜打 tag 更是如此")
