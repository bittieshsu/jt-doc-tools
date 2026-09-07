"""升級前的備份：**該留的要留、空間不夠要在停服務之前就擋下來**。

由來（v1.15.17，主動稽核）：正式機的資料目錄 2.0 GB，其中 **1.4 GB 是統編
資料庫**（政府公開資料，排程會自己重新下載）。每次升級整份複製、還留三份
= 4.2 GB 的純浪費，而那台磁碟只剩 11 GB —— 再幾次更新就滿了。

更糟的是失敗的時機：原本的順序是**先停服務再複製**，磁碟在複製途中滿掉的
話，使用者拿到的是「服務停著、備份寫到一半、空間還被吃掉」。
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

cli = importlib.import_module("app.cli")
SRC = Path(cli.__file__).read_text(encoding="utf-8")
TREE = ast.parse(SRC)

#: 弄丟就回不來的東西 —— **永遠不可以**出現在跳過清單裡。
#: 這條守門的用途就是擋住「為了省空間順手多跳過一個」。
IRREPLACEABLE = {
    "auth.sqlite", "audit.sqlite", "jobs.sqlite",
    "auth_settings.json", "form_templates.json", "llm_settings.json",
    "api_tokens.json", "label_synonyms.json",
    "fill_history", "stamp_history", "watermark_history",
    "assets", "fonts", "workspace", "branding",
    "transit_proof_files", "submission_check",
}


def _func(name: str) -> ast.FunctionDef:
    for node in TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"`app/cli.py` 裡找不到 {name}")


@pytest.mark.parametrize("name", sorted(IRREPLACEABLE))
def test_irreplaceable_data_is_always_backed_up(name: str):
    assert name not in cli._BACKUP_SKIP, (
        f"`{name}` 弄丟了長不回來，不可以從升級備份裡跳過。"
        "跳過清單的判準只有一條：**這東西自己會長回來**。")


def test_the_skip_list_is_only_rebuildable_or_temporary():
    """每一項都要說得出「怎麼長回來」。"""
    reasons = {
        "vat_db.sqlite": "排程會重新下載（政府公開資料）",
        "vat_db.sqlite-wal": "同上",
        "vat_db.sqlite-shm": "同上",
        "temp": "暫存檔，本來就有 2 小時保留期",
        "jobs": "作業產出，本來就有 24 小時保留期",
        "db_backups": "那是備份的備份",
    }
    unexplained = sorted(cli._BACKUP_SKIP - reasons.keys())
    assert not unexplained, (
        f"這些被跳過但沒有理由：{unexplained}。"
        "要加進跳過清單，先在這裡寫下它為什麼長得回來。")


def test_the_space_check_runs_before_the_service_is_stopped():
    """順序就是重點。

    先停服務再複製的話，磁碟在複製途中滿掉 = 服務停著 + 備份寫到一半 +
    空間被吃光。空間檢查一定要在 `svc_stop()` **之前**。
    """
    body = _func("svc_update").body
    order: list[str] = []
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in ("_backup_space_needed", "svc_stop",
                                "_backup_data_dir"):
                order.append((node.lineno, node.func.id))
    seq = [n for _, n in sorted(set(order))]
    assert "_backup_space_needed" in seq, "升級流程裡沒有做空間檢查"
    assert seq.index("_backup_space_needed") < seq.index("svc_stop"), (
        f"空間檢查跑在停服務之後了：{seq}")
    assert seq.index("svc_stop") < seq.index("_backup_data_dir")


def test_a_failed_backup_is_not_silent():
    """備份失敗要講出來 —— 使用者要知道這次升級沒有回頭路。"""
    src = ast.get_source_segment(SRC, _func("_backup_data_dir")) or ""
    assert "WARNING" in src and "stderr" in src


def test_backup_keeps_the_important_files_and_drops_the_rest(tmp_path,
                                                             monkeypatch):
    """行為層：真的跑一次，看備份裡到底有什麼。

    只驗清單不驗行為的話，`ignore=` 寫錯（例如比對到路徑而不是檔名）
    照樣全綠。
    """
    data = tmp_path / "data"
    (data / "temp").mkdir(parents=True)
    (data / "fill_history").mkdir()
    (data / "temp" / "scratch.pdf").write_text("x", encoding="utf-8")
    (data / "fill_history" / "rec.json").write_text("x", encoding="utf-8")
    (data / "auth.sqlite").write_text("x", encoding="utf-8")
    (data / "vat_db.sqlite").write_text("x" * 1000, encoding="utf-8")
    monkeypatch.setattr(cli, "_data_dir", lambda: data)

    cli._backup_data_dir()
    backups = list(tmp_path.glob("data.backup-*"))
    assert len(backups) == 1, backups
    got = {p.relative_to(backups[0]).as_posix()
           for p in backups[0].rglob("*") if p.is_file()}
    assert got == {"auth.sqlite", "fill_history/rec.json"}, got
    # 原始資料一個位元都不可以動
    assert (data / "vat_db.sqlite").exists()
    assert (data / "temp" / "scratch.pdf").exists()
