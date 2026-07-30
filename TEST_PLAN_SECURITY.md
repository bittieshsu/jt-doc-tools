# jt-doc-tools 資安測試計畫

從 `TEST_PLAN.md` 拆出來的獨立計畫。理由：資安項目的**執行方式**與功能驗收不同 ——
它需要「啟用認證 + 兩個以上帳號 + 一個攻擊者視角」，而且判定標準是「拿不到」而不是
「功能正常」。混在功能清單裡會被當成一般項目快速帶過。

主計畫見 `TEST_PLAN.md`；發版前兩份都要跑完。

---

## 0. 這份計畫的原則

1. **先重現、再修、再用同一組攻擊確認關閉。** 沒有重現過的「修好了」不算。
2. **跳過不等於通過。** 探測腳本若因為參數寫錯而沒打到端點，必須顯示為失敗或
   警告，不可印出綠燈。每個探測都要有**反向對照**（同一組請求由擁有者送出必須
   成功）—— 一個驗不出東西的探測比沒有探測更糟，它會給出假的安心。
3. **靜態掃描保證「沒有人被忘記」，動態測試保證「做的是對的事」。** 兩種都要，
   缺一不可：靜態看不出 fail-open，動態看不出「新加的端點沒人測」。
4. **回「找不到」不回「沒有權限」。** 後者等於告訴對方「這份東西存在，只是你不能
   看」。判定時兩者都接受，但也要確認內容真的沒回去。
5. **同一個判斷只能有一份實作。** 歸屬判斷散在兩處時，修一份不會讓另一份跟著好，
   而且兩邊看起來都「有做檢查」—— v1.14.6 的兩個無主漏洞就是同一個 bug 的兩份拷貝。
6. **不在客戶 / 正式機上做破壞性測試。** 這份計畫的攻擊測試一律在本機臨時實例
   （臨時 data dir + 臨時埠）進行。

---

## 1. 自動化（pytest）—— 每次發版必跑

```bash
.venv/bin/python -m pytest -q \
    tests/test_owasp_top10.py \
    tests/test_llm_url_ssrf.py \
    tests/test_path_traversal_audit.py \
    tests/test_redos_ad_dn.py \
    tests/test_authz_boundaries.py \
    tests/test_auth_modes_matrix.py \
    tests/test_id_from_body_acl.py \
    tests/test_job_id_acl.py \
    tests/test_job_api_acl.py \
    tests/test_submission_check_acl.py \
    tests/test_preview_acl_failopen.py \
    tests/test_stamp_watermark_preview_acl.py \
    tests/test_upload_owner_acl.py \
    tests/test_auditor_readonly.py \
    tests/test_roles_rbac.py \
    tests/test_api_gate_and_csrf_edges.py \
    tests/test_csrf.py \
    tests/test_csp_nonce.py \
    tests/test_csv_injection.py \
    tests/test_upload_validation_parity.py
```

各檔的分工：

| 檔案 | 守住什麼 |
|---|---|
| `test_id_from_body_acl.py` | **靜態掃描**：任何吃使用者提供的 id 的端點都要有權限檢查。例外清單每筆都要寫理由，並偵測「例外項已改名 / 刪除」 |
| `test_job_id_acl.py` / `test_job_api_acl.py` | 作業（含無主作業）不可被別人讀 / 取消 / 下載；pdf-to-office 的報告與前後預覽同樣要驗 |
| `test_submission_check_acl.py` | 案件 ACL：無主案件僅管理員、稽核員唯讀、管理員 / 稽核員判定必須真的有效 |
| `test_preview_acl_failopen.py` | 預覽端點認不出 upload_id 時**拒絕**（不可 no-op） |
| `test_auditor_readonly.py` | 稽核員唯讀：讀得到但不可刪紀錄、不可觸發備份輪替 |
| `test_roles_rbac.py` | 內建角色名實相符、工具 id 存在、升級步驟編號連續 |
| `test_api_gate_and_csrf_edges.py` | API token 閘不可誤擋管理區；CSRF 豁免不可只看標頭 |
| `test_auth_modes_matrix.py` | 認證開 / 關兩種模式的行為都要對（很容易只顧一邊） |
| `test_csv_injection.py` | 匯出的 CSV / xlsx 不可被試算表當公式執行；含「所有 xlsx 寫入都要走 helper」的靜態守門 |
| `test_upload_validation_parity.py` | 壞檔要回 400 不是 500；網頁介面與對外 API 判定一致；不可把伺服器回應塞進 innerHTML |

---

## 2. 滲透測試腳本（每次發版跑一次）

```bash
# 1) 起一個乾淨的本機實例（**不要**打客戶機 / 正式機）
rm -rf /tmp/pt-data && mkdir -p /tmp/pt-data
JTDT_DATA_DIR=/tmp/pt-data .venv/bin/python temp/sec-audit/setup_pentest_users.py
JTDT_DATA_DIR=/tmp/pt-data .venv/bin/python -m uvicorn app.main:app \
    --host 127.0.0.1 --port 8794 &

# 2) 打
JTDT_DATA_DIR=/tmp/pt-data .venv/bin/python temp/sec-audit/pentest.py \
    http://127.0.0.1:8794
```

涵蓋的類別（缺一類就會漏掉一整批端點）：

| # | 類別 | 為什麼需要單獨一類 |
|---|---|---|
| 1 | 未登入存取受保護路徑 | 最基本 |
| 2 | 垂直越權（一般使用者碰管理功能） | 讀與寫要分開驗（曾出現「稽核員可刪、管理員被擋」） |
| 3 | 認證繞過技巧 | Host 標頭污染（`request.url.path` 可被污染）+ 路徑變形 |
| 4 | 水平越權：**id 在網址路徑上** | `/api/jobs/{id}` 這一類 |
| 4b | 水平越權：**id 在請求內容裡** | 第一輪就是漏掉這一類 —— 13 項全過卻仍有實際洩漏 |
| 4c | **相鄰端點** | 同一支 router 裡「隔壁有驗、自己沒驗」（實例：預覽有驗、報告沒驗） |
| 5 | id 列舉與回應碼一致性 | 不存在與沒權限要無法區分 |
| 6 | 路徑穿越 | 檔名 / id 拼路徑處 |
| 7 | CSRF | 不帶 token 的寫入要被拒 |

**判讀**：每一項都要是 `[OK]`。出現「（… 跳過）」就要先修腳本再重跑 ——
跳過的項目沒有被驗證過。

---

## 3. 源碼掃描

```bash
.venv/bin/python -m bandit -r app/ -f json -o temp/sec-audit/bandit.json -q
```

判讀方式：只看 HIGH / MEDIUM（v1.14.6 為 30 個 MEDIUM，**全部屬於下表的已知非問題**）。
新增的 MEDIUM 要逐一判斷；判定為非問題就補進下表並寫明理由，不可只是忽略。

已知的**非問題**（複審時不必重開）：

| 規則 | 為什麼不是問題 |
|---|---|
| B608（SQL 字串組合） | 組出來的都是常數片段，值一律走參數化（`?`）；`vat_db` 已於 v1.12.25 改成全常數 query |
| B310（urlopen） | 目標 URL 來自管理員設定，且已過 `url_safety` 檢查 |
| B314（ElementTree） | Python 3.12 的 expat 放大限制擋住 billion-laughs；不展開外部實體，無 XXE |
| B104（綁定 0.0.0.0） | 兩處都不是真的在綁：`auth_router:113` 是**拒絕**把 `0.0.0.0/0` 當成信任的反向代理（那個字串出現在黑名單裡）；`server_template.py` 是外接 OCR 伺服器的預設值，本來就要對外提供服務 |

GitHub 端（推上去 5–15 分鐘後看）：

- Dependabot：Open alert 數應持平或下降
- CodeQL：新警告當天處理或記入 CHANGELOG「已知議題」
- **已決定 dismiss 的不要再去「修」**：匯出目錄的 6 個 path-injection（管理員本來就
  有主機檔案系統權限，且允許指定 `/mnt/backup` 是合理部署；v1.12.99 試過黑名單式
  硬化反而從 5 個變 6 個）

---

## 4. OWASP ZAP DAST（每次發版必跑，兩個目標）

```bash
mkdir -p temp/zap/$(date +%Y%m%d)-NN
# 依前一次的 plan 改 reportDir 後執行
/snap/zaproxy/current/zap.sh -cmd -autorun temp/zap/<日期-NN>/plan-30.yaml
/snap/zaproxy/current/zap.sh -cmd -autorun temp/zap/<日期-NN>/plan-doc.yaml
```

兩個目標都要掃：

1. **經反向代理的正式路徑**（`https://doc.jason.tools`）—— 才驗得到 nginx 的標頭、
   HSTS、TLS 設定
2. **直連**（`http://<內部測試機>:8765`）—— 才驗得到後端自己送的標頭

**通過標準：High / Medium / Low 全部為 0。** Info 屬勸告性可留（例如 `no-store`
會觸發的「Re-examine Cache-control」）。報告存 `temp/zap/<YYYYMMDD-NN>/`（不上
GitHub）。

### 4.1 第三個目標：已登入狀態的掃描（v1.14.6 起）

**啟用認證之後，上面兩個目標的爬蟲只看得到登入頁 —— 各 12 個網址。** 那只驗到公開
表面，管理頁與工具頁完全沒被掃到。所以另加一個帶登入狀態的掃描：

```bash
# 1) 拋棄式實例（**絕不可**對正式機做這件事 —— 帶管理員身分的爬蟲會去點各種
#    設定與刪除端點，那是破壞性的）
rm -rf /tmp/ztdata && mkdir -p /tmp/ztdata
JTDT_DATA_DIR=/tmp/ztdata .venv/bin/python temp/sec-audit/setup_pentest_users.py
JTDT_DATA_DIR=/tmp/ztdata .venv/bin/python -m uvicorn app.main:app \
    --host 127.0.0.1 --port 8795 &

# 2) 發一個管理員 session，寫進 plan 的 replacer 規則
#    （**掃描前重發** —— 舊 cookie 失效時爬蟲會靜靜地退回只爬登入頁，
#      症狀就是「只找到 12 個網址」而不是任何錯誤訊息）
# 3) 跑 plan-auth.yaml
/snap/bin/zaproxy -cmd -autorun temp/zap/<日期-NN>/plan-auth.yaml
```

**判讀時先看爬到幾個網址**：應為數百個（v1.14.6 實測 393）。若是 12 個，代表
cookie 沒生效，這次掃描等於沒做 —— 不可當成通過。

`plan-auth.yaml` 的 replacer 規則 `matchType` 要寫 `req_header`（不是
`request_header`，後者會讓整個計畫失敗），且不要加 `enabled` 欄位（會警告）。

---

## 5. 手動：兩個帳號互相攻擊（抽查，10 分鐘）

自動化測到的是已知形狀，這一段是找**新形狀**。用瀏覽器開兩個無痕視窗分別登入
A / B，然後：

- [ ] A 上傳檔案取得任何 URL（作業、預覽、下載、報告、縮圖）→ 貼到 B 的視窗
- [ ] A 的網址裡有 id 的地方，把 id 換成 B 的 → 應該拿不到
- [ ] 開 DevTools 看 A 的每一個 XHR，把**請求內容裡**帶 id 的那幾個重放成 B 的身分
- [ ] B 用一般使用者身分直接輸入 `/admin/...` 的各頁網址
- [ ] 稽核員帳號：確認看得到稽核 / 歷史，但每一個刪除 / 儲存按鈕都被拒
- [ ] A 登出後，用剛剛那些 URL 再試一次（session 失效要立刻生效）

發現任何一項成功 → 停下來寫成一個 pytest 再修（不可只手動修掉）。

---

## 6. 歷史案例（每次發版必過）

| 版本 | 問題 | 重現方式 |
|---|---|---|
| v1.14.6 | pdf-compress `/submit` 吃請求內容裡的 `upload_id`，B 可取得 A 的 PDF 內容 | 滲透測試 §4b |
| v1.14.6 | 三個根層級 `/api/*` 端點未經認證即可呼叫（CSRF token 從公開登入頁就拿得到） | 滲透測試 §1 |
| v1.14.6 | 無主作業任何登入者可讀 | `test_job_id_acl.py` |
| v1.14.6 | 無主案件任何登入者可讀 / 改 / 刪 | `test_submission_check_acl.py` |
| v1.14.6 | 送件檢核的管理員 / 稽核員判定永遠不成立（讀不存在的欄位 / import 不存在的模組） | `test_submission_check_acl.py` |
| v1.14.6 | 預覽 ACL 在認不出 upload_id 時整個跳過 | `test_preview_acl_failopen.py` |
| v1.14.6 | pdf-to-office 的改善報告沒有驗歸屬（隔壁的預覽有驗） | 滲透測試 §4c |
| v1.14.6 | 稽核員可刪歷史紀錄、可輪替掉資料庫備份；管理員反而被擋 | `test_auditor_readonly.py` |
| v1.14.6 | 「法務資安」角色實際等於「一般使用者」 | `test_roles_rbac.py` |
| v1.14.6 | API token 強制驗證開啟時管理區全壞（判斷用「路徑含 /api/」） | `test_api_gate_and_csrf_edges.py` |
| v1.14.6 | 另外三個工具的預覽端點切出空 id 就跳過檢查（doc-deident / pdf-editor / pdf-to-image） | `test_preview_acl_failopen.py`（prefix 那組） |
| v1.14.6 | `/workspace/save` 有自己一份歸屬判斷 → 無主作業可被任何登入者存走 | `test_job_id_acl.py::test_workspace_save_denies_ownerless_job` |
| v1.14.6 | 送件檢核用 403 / 404 區分「不是你的」與「不存在」（id 查詢介面） | `test_submission_check_acl.py::test_non_owner_response_is_indistinguishable_from_not_found` |
| v1.14.6 | 匯出的 CSV / xlsx 可被試算表當公式執行（註解作者來自對方的 PDF） | `test_csv_injection.py` |
| v1.14.6 | 四個工具的網頁介面對非 PDF 回 500（對外 API 有驗、網頁介面沒有） | `test_upload_validation_parity.py` |
| v1.14.6 | 十處把伺服器回應直接塞進 innerHTML | `test_upload_validation_parity.py::test_no_template_injects_raw_server_text_into_innerhtml` |
| v1.11.81 | Host 標頭污染 `request.url.path` 可繞過工具權限閘 | 滲透測試 §3 |
| v1.12.52 | 群組成員數用 `innerHTML` 塞 DOM 文字（CodeQL High） | 源碼掃描 |
| v1.12.33-34 | `csrf.js` 沒包相對 URL fetch 與 XMLHttpRequest → 所有上傳在正式環境 403 | **headless 實測上傳**（conftest 關閉 CSRF，測不到） |
| v1.4.83 | 任一登入者拿到別人的 `upload_id` 即可下載對方 PDF | `test_upload_owner_acl.py` |
