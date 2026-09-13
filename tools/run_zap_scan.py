#!/usr/bin/env python3
"""發版前的 OWASP ZAP 掃描（三個目標）—— 一鍵跑完並印出判讀。

**為什麼要有這支腳本**：這件事是發版守則之一（「每次發版前必跑、
High/Medium/Low 全修到 0 才能發」），但過去每次都是臨時手刻 plan 檔與
已登入實例 —— 於是 v1.15.8 ~ v1.15.39 **整整 13 個版本一次都沒跑**，
最後一份報告停在 2026-09-05。**要重建才能做的事，就是會斷掉的事。**

三個目標（缺一個就少驗一片）：

| 目標 | 驗到什麼 |
|---|---|
| 對外網址（經反向代理） | 對外那一層：TLS、標頭、反向代理的設定 |
| 內部正式機（直連應用程式埠） | 沒有反向代理保護時的應用程式本身 |
| **本機拋棄式實例（帶管理員 cookie）** | **登入之後的畫面** —— 前兩個在啟用認證後只看得到登入頁 |

實際位址放在 `temp/zap/targets.json`（見下方 `_TARGETS_FILE`），**不寫進這支
腳本** —— `tools/` 會同步進公開樹。

判讀先看**爬到幾個網址**：只有十幾個就代表 cookie 沒生效，那次掃描等於沒做。

用法：
    python tools/run_zap_scan.py                 # 三個目標都跑
    python tools/run_zap_scan.py --only auth     # 只跑已登入那個
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ZAP = Path("/snap/zaproxy/current/zap.sh")
#: snap 版 ZAP 自帶 JRE，但 `zap.sh` 不會自己找 —— 不設就直接
#: 「requires a minimum of Java 17」離開，**而且回傳碼是 0、報告目錄是空的**
#: （第一次踩到時差點誤以為掃過了）。判讀一律先確認報告檔有產出。
JAVA_HOME = "/snap/zaproxy/current/usr/lib/jvm/java-17-openjdk-amd64"

#: 掃描目標。**位址不寫在這裡** —— `tools/` 會同步進公開樹，寫死內網 IP 或
#: 內部網域就是把部署資訊公開出去（本專案的發版檢查清單裡就有「推之前掃內網
#: IP」那一條）。實際位址放 `temp/zap/targets.json`（`temp/` 不進公開樹）：
#:
#:     {"doc": ["https://你的網域", "對外（經反向代理）"],
#:      "30":  ["http://內網位址:8765", "內部正式機（直連）"]}
#:
#: 沒有那個檔就只跑 `auth`（本機拋棄式實例）—— 那一支本來就只用 127.0.0.1，
#: 任何人 clone 下來都跑得動，而且它是三個裡面**唯一看得到登入後畫面**的。
_TARGETS_FILE = ROOT / "temp" / "zap" / "targets.json"


def _targets() -> dict[str, tuple[str | None, str]]:
    out: dict[str, tuple[str | None, str]] = {
        "auth": (None, "已登入（本機拋棄式實例）")}
    if _TARGETS_FILE.exists():
        raw = json.loads(_TARGETS_FILE.read_text(encoding="utf-8"))
        for k, v in raw.items():
            out[k] = (v[0], v[1] if len(v) > 1 else k)
    return out


TARGETS = _targets()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_authenticated_instance(tmp: Path) -> tuple[subprocess.Popen, int, str]:
    """起一個**啟用認證**的拋棄式實例，回 (行程, 埠, session cookie 值)。

    直接寫資料庫發一張 session，不走登入表單 —— 掃描要的是「帶著有效
    cookie 的爬蟲」，不是驗證登入流程本身（那有自己的測試）。
    """
    port = _free_port()
    env = dict(os.environ, JTDT_DATA_DIR=str(tmp), JTDT_HOST="127.0.0.1",
               JTDT_PORT=str(port))
    seed = ROOT / "tools" / "_zap_seed.py"
    seed.write_text(
        "import os, sys\n"
        "sys.path.insert(0, %r)\n"
        "from app.core import auth_db, auth_settings, user_manager, sessions\n"
        "auth_db.init()          # 全新資料目錄裡還沒有 users 表\n"
        "from app.core import roles\n"
        "roles.seed_builtin_roles()\n"
        "auth_settings.save({'backend': 'local'})\n"
        "pw = %r\n"
        "u = user_manager.get_by_username('jtdt-admin')\n"
        "if not u:\n"
        "    user_manager.create_local('jtdt-admin', 'ZAP admin', pw, roles=['admin'])\n"
        "    u = user_manager.get_by_username('jtdt-admin')\n"
        "tok, _ = sessions.issue(u['id'], remember=True, ip='127.0.0.1', ua='zap')\n"
        "print(tok)\n" % (str(ROOT), secrets.token_urlsafe(18)),
        encoding="utf-8")
    try:
        out = subprocess.run([sys.executable, str(seed)], cwd=ROOT, env=env,
                             capture_output=True, text=True, timeout=120)
        if out.returncode != 0:
            raise SystemExit(f"種子帳號建立失敗：\n{out.stdout}\n{out.stderr}")
        token = out.stdout.strip().splitlines()[-1]
    finally:
        seed.unlink(missing_ok=True)

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            import urllib.request
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2)
            break
        except Exception:
            time.sleep(1)
    else:
        proc.terminate()
        raise SystemExit("拋棄式實例起不來")
    return proc, port, token


def _plan(name: str, url: str, outdir: Path, title: str, cookie: str | None) -> Path:
    jobs = []
    if cookie:
        jobs.append({
            "type": "replacer",
            "parameters": {"deleteAllRules": True},
            "rules": [{"description": "admin session cookie",
                       "matchType": "req_header", "matchString": "Cookie",
                       "matchRegex": False,
                       "replacementString": f"jtdt_session={cookie}"}],
        })
    jobs += [
        {"type": "spider",
         "parameters": {"context": f"jtdt-{name}", "url": url,
                        "maxDuration": 8, "maxDepth": 10}},
        {"type": "passiveScan-wait", "parameters": {"maxDuration": 10}},
        {"type": "report",
         "parameters": {"template": "traditional-html", "reportDir": str(outdir),
                        "reportFile": f"zap-{name}-report", "reportTitle": title}},
        {"type": "report",
         "parameters": {"template": "traditional-json", "reportDir": str(outdir),
                        "reportFile": f"zap-{name}-report"}},
    ]
    plan = {
        "env": {"contexts": [{"name": f"jtdt-{name}", "urls": [url],
                              "includePaths": [f"{url}.*"],
                              "excludePaths": [f"{url}/logout.*",
                                               f"{url}/auth/.*/sls.*"]}],
                "parameters": {"failOnError": False, "failOnWarning": False,
                               "progressToStdout": True}},
        "jobs": jobs,
    }
    p = outdir / f"plan-{name}.yaml"
    p.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def _run(plan: Path) -> None:
    env = dict(os.environ, JAVA_HOME=JAVA_HOME)
    # **plan 檔要給絕對路徑** —— snap 會把相對路徑解到它自己的目錄，
    # 而且 `Cannot access file` 只出現在日誌裡、回傳碼照樣 0。
    subprocess.run([str(ZAP), "-cmd", "-autorun", str(plan.resolve())],
                   env=env, check=False)


def _summarise(report: Path) -> tuple[dict[str, int], int]:
    data = json.loads(report.read_text(encoding="utf-8"))
    risk = {"High": 0, "Medium": 0, "Low": 0, "Informational": 0}
    urls: set[str] = set()
    for site in data.get("site", []):
        for a in site.get("alerts", []):
            name = a.get("riskdesc", "").split(" ")[0]
            if name in risk:
                risk[name] += int(a.get("count", len(a.get("instances", []))))
            for inst in a.get("instances", []):
                if inst.get("uri"):
                    urls.add(inst["uri"])
    return risk, len(urls)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=sorted(TARGETS), action="append")
    args = ap.parse_args()
    if not ZAP.exists():
        raise SystemExit(f"找不到 ZAP：{ZAP}")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    outdir = ROOT / "temp" / "zap" / stamp
    outdir.mkdir(parents=True, exist_ok=True)
    want = args.only or list(TARGETS)

    proc = tmp = None
    results: dict[str, tuple[dict[str, int], int]] = {}
    try:
        for name in want:
            url, title = TARGETS[name]
            cookie = None
            if name == "auth":
                tmp = ROOT / "temp" / f"zap-data-{stamp}"
                tmp.mkdir(parents=True, exist_ok=True)
                proc, port, cookie = _start_authenticated_instance(tmp)
                url = f"http://127.0.0.1:{port}"
            plan = _plan(name, url, outdir, f"jt-doc-tools {title}", cookie)
            print(f"\n=== 掃描 {name}：{url} ===", flush=True)
            _run(plan)
            rep = outdir / f"zap-{name}-report.json"
            if not rep.exists():
                print(f"!! {name}：**沒有產出報告** —— 這次等於沒掃")
                continue
            results[name] = _summarise(rep)
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n報告：{outdir}")
    bad = False
    for name, (risk, n_urls) in results.items():
        flag = "" if (risk["High"] + risk["Medium"] + risk["Low"]) == 0 else "  <== 要修"
        bad = bad or bool(flag)
        print(f"  {name:5s} 網址 {n_urls:4d}  High {risk['High']}  "
              f"Medium {risk['Medium']}  Low {risk['Low']}  "
              f"Info {risk['Informational']}{flag}")
        if name == "auth" and n_urls < 30:
            print("        ⚠ 已登入掃描只爬到這麼少 —— cookie 可能沒生效，這次不算數")
    return 1 if bad or len(results) != len(want) else 0


if __name__ == "__main__":
    raise SystemExit(main())
