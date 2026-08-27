# jt-doc-tools 測試計畫

每次發版前都跑 `pytest`。覆蓋以下面向：

> **資安項目已拆到獨立計畫：`TEST_PLAN_SECURITY.md`**（越權 / RBAC / 滲透測試 /
> 源碼掃描 / ZAP）。拆開的理由是執行方式不同 —— 那些項目需要「啟用認證 + 兩個以上
> 帳號 + 攻擊者視角」，判定標準是「拿不到」而不是「功能正常」，混在功能清單裡會被
> 當成一般項目快速帶過。**發版前兩份都要跑完。**

## 1. 自動化測試（pytest）

執行：
```bash
.venv/bin/python -m pytest -q
```

### 1.1 路由 smoke (`tests/test_smoke_routes.py`)
- 所有公開路由（首頁 / healthz / admin 頁 / 每個工具頁）都應回 200
- 回歸：`/tools/pdf-fill/?cid=…` 不能 500（pydantic forward-ref 問題）
- 停用的工具（例：`aes-zip`, `enabled=False`）**不**應註冊路由

### 1.2 PDF 工具端到端 (`tests/test_pdf_tools.py`)
- `pdf-merge` 合併 1+2 頁 → 結果 3 頁
- `pdf-merge` 拒絕單檔
- `pdf-split` mode=each 切 10 頁 → ZIP 內 10 個 PDF
- `pdf-split` mode=ranges `1-3,5,7-` → ZIP 內 3 個 PDF
- `pdf-rotate` 整份 90 度 → 每頁 rotation==90
- `pdf-rotate` 指定頁面 (`3,5`, 180) → 只有 p3/p5 旋轉，其他 0
- `pdf-rotate` **水平鏡射** (mode=flip-h) → 內容翻轉但頁數不變
- `pdf-rotate` **垂直鏡射** (mode=flip-v)
- `pdf-pages` mode=drop `2-4` → 剩 7 頁
- `pdf-pages` mode=reorder `5,4,3,2,1` → 5 頁
- `pdf-pageno` 印頁碼 → 抽取文字確認 `1/2`、`2/2` 出現
- 通用 `/api/jobs/{id}/download-png` → 兩頁 PDF 回 ZIP，內含 2 個 PNG

### 1.3 欄位偵測單元測試 (`tests/test_pdf_form_detect.py`)
- `_normalize` 處理 `**` / `1.` 前綴與 `:`／`：` 後綴
- NFKC 折疊：U+F9F7（compat 立）≡ U+7ACB（canonical 立）
- 簡繁折疊：傳真號碼 ≡ 传真号码
- `_split_multi_colon_span("銀行名稱：     銀行代號：")` 切成兩段
- 同義字索引找得到 `公司名稱` / `duns / 鄧白氏`
- 用 PyMuPDF 動態建 PDF，驗證偵測到 `company_name`
- 印章區排除：`公司章` 同列的 `負責人` 必須被排除

### 1.4 Admin API (`tests/test_admin_apis.py`)
- 轉檔設定：可儲存自訂路徑與 builtin 順序，回讀含新 path
- 公司 profile：建立 → 啟用 → 用 `?cid=` 讀 pdf-fill 200 → 刪除
- 同義詞：POST/save 後 GET 回 200
- **字型管理**：GET `/admin/fonts` 200、`/api/fonts` 列出字型清單
- **LLM 設定**：GET `/admin/llm-settings` 200，預設 `enabled=False`
- **API Token**：可建立/列表/刪除 token；`/api/*` 需帶 bearer

### 1.5 資產與圖像 (`tests/test_assets_and_image_utils.py`)
- 上傳 200x100 PNG → match-aspect 後 width/height ratio ≈ 2:1
- 裁剪右半 (`x=0.5,w=0.5`) → 結果 preset 比例 ≈ 1:1
- `remove_white_background` 對 400x400 白底中間黑方塊 → 自動裁掉空白邊界，輸出尺寸落在 90~130

### 1.6 資產縮圖載入 (`tests/test_asset_thumbnails_resolve.py`)
- 每個已登錄資產的 `/assets/{id}/thumb` 與 `/file` 都回 200（印章/簽名 picker 不破圖）
- 匯出 → 合併匯入（會重新分配 id）後縮圖仍載入得到（防 import 沒同步 file_key/thumb_key → 縮圖 404 破圖,2026-06-27 客戶回報）
- file_key/thumb_key 指向不存在的檔時退回 `{id}.png`

### 1.7 授權邊界 (`tests/test_authz_boundaries.py`)
- **垂直越權**:已登入的非 admin 一般使用者 → 所有 /admin/* 頁 + admin 寫入（改站名/關認證/列使用者/建 token）一律非 200（401/403/302）
- **工具權限**:default-user 沒有的工具（pdf-fill/pdf-stamp）UI 與後端動作端點都擋；有的（pdf-merge）可用
- **水平越權**:B 使用者不可下載 A 的工作區檔（/workspace/file/{id}）與 A 的上傳檔（/tools/pdf-editor/file/{upload_id}）

### 1.8 使用者工作區 (`tests/test_workspace.py` + `tests/test_workspace_api.py`)
核心（`workspace.py`）：
- 存 PDF / PNG → meta 正確（ext / mime / 顯示名）；list 回該使用者的檔
- PNG 以 magic bytes 偵測（檔名沒 .png 也自動補副檔名）
- 非 PDF/PNG（zip 等）→ `UnsupportedType`
- get / rename / delete CRUD 正常；刪除後 get 回 `NotFound`
- **跨使用者隔離**：bob 拿 alice 的 file_id → `NotFound`；list 互不可見
- 每人容量額度超過 → `QuotaExceeded`；單檔上限超過 → `QuotaExceeded`
- **停用** → save 回 `WorkspaceDisabled`、list 回空（功能完全隱藏）
- 認證 OFF → 單一共用工作區 key `__single__`，仍可存取
- 保留掃描 `sweep_older_than`：backdate 後掃掉過期項
- 設定 save/get roundtrip（enabled 為布林）

端點（`workspace_routes.py`，auth OFF / 單機）：
- `GET /workspace` 頁面 200、含「我的工作區」
- save → list → file(serve) → delete 一輪；serve 回 `application/pdf`
- save 非 PDF/PNG → 400
- `?accept=png` 過濾掉 PDF
- **停用時** `/workspace`、`/workspace/save`、`/workspace/api/list` 全回 404

### 1.9 乘車證明整理（`tests/test_transit_proof_parser.py` + `tests/test_transit_proof_api.py`）

- 解析器：高鐵電子車票證明（label：value）+ 台鐵購票證明（打散版面用特徵正則）；日期正規化 ISO、乘車日排除印製日期、乘車區間抽起訖時間 / 站名、車種不被「乘車區間」誤匹配、高鐵站名去「高鐵 / 車站」；非乘車證明 / 空欄位 → ParseError。
- 端點：頁面渲染、上傳解析 + 票號去重、非乘車證明 PDF 進 failed、7 種格式匯出（csv/xlsx/ods/json/xml/txt/md）+ 非法格式 400 + 空清單 400、CSV 預設 4 欄（日期/交通工具/來源-目的/費用）、設定 roundtrip（勾選 / 順序 / 格式 / 匯出標題）套用到匯出、刪除單筆、對外 API 不寫 buffer。
- **手動驗收**：拉多張台鐵 + 高鐵 PDF → 表格出現 4 欄 + 底部加總；「設定」加欄位 / 改格式 / 排序 → 表格與匯出同步；各格式下載可開。合成 PDF 測試須用 CJK 字型（`fontname="china-t"`）否則抽文字變 notdef。

### 1.10 目錄瀏覽 filter（`tests/test_dir_filter.py` + `tests/test_directory_filter_api.py`）

- 純函式：規則 → LDAP filter（類型→objectClass、名稱關鍵字 escape_filter_chars 轉義、多欄位）；符合物件 → 剪枝樹（祖先鏈、共用祖先合併去重、matched 旗標、parent 排在 child 前、cycle-safe、無 root 停在 DC 層）。
- 設定 roundtrip / 清洗（空規則丟棄、無效類型過濾、無效 default_mode 忽略）。
- 端點：`/directory/filter` GET/POST roundtrip（backend-agnostic）；`/directory/selected` 非目錄後端回 400；目錄頁可渲染。
- **手動驗收（需 LDAP / AD）**：進 /admin/directory → 預設「已選定」模式；設定 filter 加規則（名稱關鍵字 + 類型 + OU 子樹）→ 儲存 → 樹只留符合分支；切「全部」看完整目錄樹；點 OU 指派角色仍正常。

### 1.11 每頁畫面 + 關鍵元素可見性回歸（`scripts/page_visual_check.py`）

**目的**：抓「元素 / 功能靜默消失」這一類 regression（例：v1.12.30 CSP 樣式重構
讓「下載」按鈕、臨時資產縮圖、個資限用章預覽在存檔 / 選圖後一直不顯示，
v1.12.71 修）。純像素比對對字型 / 時間戳 / 動態內容太吵，所以主檢查是
「可見互動元素清單」比對 + 關鍵狀態斷言，截圖僅供人工對照。

**需要**：headless chromium（開發機上是 `chromium-browser`）+ 一個 auth-off 本機實例。

**跑法（發版前）**：
```bash
# 1) 起 auth-off 實例（臨時 data dir）
JTDT_DATA_DIR=$(mktemp -d) JTDT_CSRF_DISABLE=1 \
  .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8799 &
# 2) 比對（有元素消失就 exit 1）
.venv/bin/python scripts/page_visual_check.py --base http://127.0.0.1:8799
# 3) UI 有意改動後才更新 baseline
.venv/bin/python scripts/page_visual_check.py --base http://127.0.0.1:8799 --update
```

**檢查內容**：
- 逐一載入 39 個工具落地頁 + 首頁 + 工作區，擷取「可見互動元素清單」
  （可見按鈕文字 / 輸入 / 上傳區；用 `offsetParent` + computed `display` 判定
  「真的看得到」，能抓 CSS 規則造成的隱藏）。
- 與 baseline（`tests/visual/baseline_inventory.json`，進版控）比對：baseline 有、
  現在不見的可見按鈕 → **FAIL（功能消失）**；控制項數量下降 → warn。
- 每個工具落地頁至少要有一個可見的互動控制項，否則 FAIL。
- **關鍵狀態斷言**：pdf-editor 從工作區載入 → 儲存並預覽 → `#btnDownload` 必須
  可見（直接守住 download-after-save 這類「動作後才出現」的元素）。
- 截圖 + 清單存 `temp/visual/<run>/`（gitignore，供人工前後對照）。

**已驗證能抓到**：① 在 baseline 塞假按鈕 → 比對報「消失的可見按鈕」；
② 還原 v1.12.71 下載鈕修法（重現 bug）→ 報「存檔後下載按鈕仍不可見」。

### 1.12 缺中文字型提示 (`tests/test_cjk_font_notice.py`)

- 偵測層：挑得到黑體 / 只挑得到明體 → ok；兩種都挑不到 → 帶回 `sys_deps`
  那一份的安裝指令（`font_health.py` 內不可自己寫死 apt 指令）
- 偵測炸掉時 Jinja global 回 ok=True（**寧可安靜，不可誤報**）
- **自動列舉**會把中文畫進 PDF 的工具（`ast` 看 import，不掃註解），
  模板少 include 提示元件就 FAIL —— 已用「拿掉浮水印的 include」驗過會紅
- 字型齊全 → 頁面上沒有這塊；缺字型 → 管理員看到安裝路徑、一般使用者
  看到「請聯絡管理員」且**不出現任何管理區連結**
- `.cjk-warn` 樣式必須在 `platform.css`（元件內不可有 `<style>`）

### 1.13 四種登入方式的實機驗證（`temp/authtest/verify_logins.py`）

**與 pytest 的差別**：pytest 走 TestClient（ASGI 內部呼叫），LDAP 端是假的；
這支**真的起一個 uvicorn**、**真的對一台 OpenLDAP 做 bind**、**真的送表單帶
CSRF token**，驗的是「使用者按下登入之後會發生什麼」。

**需要**：開發機上的測試目錄（`slapd` + `ldap-utils`，suffix `dc=jtdt,dc=test`，
含 memberof overlay 與 AD 相容屬性的 schema）。

**跑法**：`python temp/authtest/verify_logins.py`（37 項，全部要 [OK]）

| 階段 | 驗到什麼 |
|---|---|
| 本機 | 未登入被擋 / 錯密碼不發 session / 正確密碼發 session / whoami / 登出失效 / 一般使用者只拿到自己角色的工具 |
| LDAP | 真實 bind、JIT 開通、顯示名稱與信箱帶入、memberOf 群組同步、錯密碼與不存在帳號同一訊息、**空密碼被核心擋下**（RFC 4513 未認證 bind） |
| LDAP OU | OU 上指派的角色生效（判準挑**預設角色沒有**的工具）+ 反向對照、搬 OU 後 **DN 換綁**（id 不變、寫稽核 `user_dn_rebind`） |
| AD | 以 sAMAccountName 登入、來源標記 `ad`、大小寫不敏感、`userAccountControl` 三態 |
| SSO (OIDC) | 登入頁列出提供者、導向 IdP、state/nonce、回呼驗簽換 token 發 session、以 `sub` 當識別碼、**偽造 state 被拒** |

SAML 由 `tests/test_sso_saml_e2e.py` 涵蓋（自架 IdP、真 xmlsec 簽章，含竄改 /
換錯金鑰 / 重放三種攻擊路徑）。

**踩過的坑**：①目錄是**持久**的，腳本要自己把搬走的帳號放回去，否則第二次跑
會出現三條假失敗；②OU 的 subject key 是**精確字串比對**，要用目錄實際回傳的
大小寫（管理介面指派時寫的也是目錄回來的那一份）。

## 2. 手動驗收清單（每個版本）

### 2.1 填單用印

#### PDF 表單填寫 (pdf-fill)
- [ ] 上傳廠商 PDF（`temp_pdfs/` 內的真實樣本，四種不同版型）
- [ ] 自動偵測欄位且公司資料正確帶入
- [ ] 切換第二公司不會 500
- [ ] 拖曳藍框微調位置 → 套用新位置
- [ ] 編輯模式 ↔ 合成模式切換
- [ ] 下載 PDF / 下載 PNG 都可用
- [ ] Office 來源（docx/xlsx/odt）自動先轉 PDF 再偵測

#### PDF 用印與簽名 (pdf-stamp)
- [ ] 同時看得到 印章/簽名/Logo 三類資產
- [ ] **所有印章/簽名/Logo 縮圖都實際載入顯示（無破圖）** — 特別是經「匯入（合併/取代）」進來的資產（回歸 2026-06-27 簽名破圖）
- [ ] 上傳檔案後預覽區自動出現，編輯/合成模式可切換
- [ ] 多檔上傳 → ZIP 下載

#### 浮水印 (pdf-watermark)
- [ ] 只列出 type=watermark 的資產（沒有就提示去資產管理上傳）
- [ ] 平鋪填滿 / 指定位置 兩個模式都可用
- [ ] 透明度 / 旋轉 即時預覽
- [ ] 結果 PDF 在閱讀器中無法選取移除浮水印
- [ ] 多檔批次 → ZIP

### 2.2 檔案編輯

#### PDF 編輯器 (pdf-editor) 🆕
- [ ] 上傳 PDF 正確 render（PDF.js 背景 + Fabric overlay）
- [ ] 新增文字框（選字型、字級、顏色、粗體、斜體、底線、旋轉）
- [ ] 字型選單顯示系統 + 內建 CJK + 自訂，不是原生下拉
- [ ] 新增圖片框（從 asset 或直接上傳）
- [ ] 新增形狀 / 白底遮罩 / 螢光筆 / 底線 / 刪除線 / 便箋 / 手繪
- [ ] 點選 canvas 上的既有文字/圖片 → 紅框反白
- [ ] 刪除既有物件（redact 真刪，非浮層蓋）
- [ ] AcroForm widget 刪除（如果 PDF 有表單欄位）
- [ ] vector path / 線條刪除
- [ ] **多選批次改屬性**：Shift+click 多個物件、改字型同時套用
- [ ] **整份換字型**：右側面板按鈕一鍵替換全文字物件字型
- [ ] 復原 / 重做
- [ ] 存檔後重新開啟，物件保留或已 redact（destructive 項目）

#### 合併 (pdf-merge)
- [ ] 2 份以上 PDF 依序合併
- [ ] 單檔拒絕

#### 分拆 (pdf-split)
- [ ] 每頁一份 / 範圍模式都可用

#### 轉向 (pdf-rotate) 🆕 加入鏡射
- [ ] 整份 90/180/270 旋轉
- [ ] 指定頁面旋轉
- [ ] **水平鏡射**（flip-h）內容左右翻轉
- [ ] **垂直鏡射**（flip-v）內容上下翻轉
- [ ] 向量品質保留（非 raster 重繪）

#### 頁面整理 (pdf-pages)
- [ ] 刪除指定頁面
- [ ] 重新排序頁面

#### 插入頁碼 (pdf-pageno) 🆕 視覺選位
- [ ] **2×3 位置選擇格**點選直接換位置
- [ ] 格式 chips（1、1/10、第 1 頁、Page 1）
- [ ] 字級 / 邊距滑桿即時調整
- [ ] 顏色選色器
- [ ] 起始頁碼與跳過頁設定
- [ ] 輸出 PDF 頁碼正確

#### PDF 壓縮 (pdf-compress) 🆕
- [ ] 三個預設（無損 / 平衡 / 極限）都能縮小
- [ ] 進階模式：圖片 DPI / JPEG 品質 / 字型子集化 / 移除註解 分別生效
- [ ] 若系統裝 Ghostscript，進階選項可勾選 GS pass
- [ ] 檔案大小比原檔小；文字內容仍可抽取

### 2.3 內容擷取

#### 擷取文字 (pdf-extract-text) 🆕
- [ ] 擷取 → TXT / Markdown / Word / ODT 四種輸出
- [ ] 段落結構（第二輪合併相鄰 block）正確
- [ ] **LLM 重排** 預設關閉；開啟後 progress NDJSON 事件正常流入
- [ ] LLM 處理時按鈕 disable、顯示進度
- [ ] think mode 被關閉（輸出裡沒殘留 `<think>...</think>`）
- [ ] 取消 / 中斷處理

#### 擷取圖片 (pdf-extract-images)
- [ ] 抽出所有嵌入圖片 → ZIP

#### PDF 附件萃取 (pdf-attachments) 🆕
- [ ] 列出 EmbeddedFiles 清單（含檔名 / 大小）
- [ ] 單檔下載 / 全部打包 ZIP
- [ ] 沒附件時顯示空狀態

#### 內容處理類補列（原本只有 §4 一行 API 的工具）
- [ ] pdf-nup：2/4/8 合 1、頁序正確、邊界不裁字
- [ ] pdf-annotations：清單列出註解（作者 / 類型 / 頁碼）＋ 匯出
- [ ] pdf-annotations-flatten：平面化後註解不可再選取；視覺不位移
- [ ] pdf-annotations-strip：全刪 / 依作者 / 依類型；`/AF` 附件關聯一併清
- [ ] pdf-ocr：中文影像 PDF 辨識後文字可選取、highlight 寬度與字對齊；
      停止辨識鈕即時中止
- [ ] pdf-wordcount：中英混排字數與 Word 統計一致（±1%）；CSV 匯出
- [ ] text-list：排序 / 去重 / 前綴後綴；大清單（10 萬行）不逾時
- [ ] text-diff：長行換行後左右仍對齊（grid row pairing）
- [ ] text-deident：貼文字與上傳 .docx 兩條路都能偵測 / 遮罩
- [ ] translate-doc：背景作業模式、`?job=` 接回、對照表分頁後複製全文不漏頁
- [ ] vat-lookup：統編反查 / 名稱模糊搜尋毫秒回、統計圖下鑽
- [ ] einvoice-scan：QR 雙碼解析、月份彙整表
- [ ] submission-check：規則 / OCR / LLM 三層可分開開關；儀表板僅 admin

### 2.4 格式轉換

#### 辦公文件轉 PDF (office-to-pdf)
- [ ] .docx / .xlsx / .pptx / .odt 各轉一份
- [ ] OxOffice 優先（`find_soffice` 命中 OxOffice）

#### 辦公文件轉圖片 (pdf-to-image) 🆕 擴充 Office
- [ ] PDF 每頁 → PNG
- [ ] **Office 檔案（docx/xlsx/pptx/odt）先自動轉 PDF 再轉圖**
- [ ] 單頁直接下 PNG、多頁自動 ZIP

#### 辦公文件格式互轉 (office-convert) 🆕 v1.14.34
- [ ] 上傳 `.odt` → 只顯示文書檔那一組目標；換上傳 `.pptx` → 切到簡報那一組
- [ ] 一次混上傳兩類（.odt + .ods）→ **前端當場擋下**並講出混到哪兩類
- [ ] `.odt` 轉 Word 97–2003：下載鈕顯示「下載 .doc」（不是「下載 PDF」）
- [ ] **同副檔名互轉**（.pptx 選 pptx 目標）要真的轉（soffice 對同目錄同副檔名
      會無聲跳過 —— 核心已改為獨立輸出目錄，`test_office_convert.py` 守著）
- [ ] `.docx` 兩個版本目標（Word 2007 / Word 2010–365）產出的相容模式
      分別是 12 / 15（`zipfile` 開 `word/settings.xml` 看 `w:val`）
- [ ] 多檔 → ZIP；轉完「存至工作區」有出現且存得進去（.xlsx 也要）
- [ ] 跨類（.ods 配 docx 目標）走 API 直打 → 400，不是產出一份壞檔
- [ ] `GET /tools/office-convert/formats` 三個家族都在；缺 Impress 的機器
      簡報家族整組消失（不是留一組永遠轉不出來的）

#### 書籤與目錄 (pdf-bookmark) 🆕 v1.14.20
- [ ] 多檔上傳自動串接，檔名成為第一層書籤；子文件原書籤降一層、頁碼加偏移
- [ ] 貼上目錄文字解析（含頁碼在行尾）；層級不合法時自動 normalize 並逐條回報
- [ ] 頁碼超出總頁數 → 明確擋下（PyMuPDF 預設無聲夾到最後一頁）
- [ ] 插目錄頁：書籤頁碼 / 目錄上印的頁碼 / 目錄連結三者一起位移；
      插入點之前的書籤**不可平移**（封面那筆不能指到目錄自己）

#### 頁面尺寸統一 (pdf-page-size) 🆕 v1.14.20
- [ ] 混合尺寸 PDF 統一成 A4：內容仍是向量、文字仍選得到（不是轉成圖）
- [ ] 原本就是目標尺寸的頁**不重放**（不多包一層 XObject）
- [ ] 帶 /Rotate 的頁面尺寸判斷正確（`page.rect` 已是視覺尺寸，不可再算一次）

#### 騎縫章 (pdf-seam-stamp) 🆕 v1.14.20
- [ ] 印章切片蓋在連續頁上，預覽「拼回去」看接縫是否對得起來
- [ ] 旋轉在切片**之前**（先切再各自轉會對不起來）；切片寬度累進取整無殘條
- [ ] 同一組內位置與角度完全一致；亂數種子有回報可重現
- [ ] 一般使用者權限與「用印與簽名」一致（`test_roles_rbac.py` 守著）

#### 頁面加框 (pdf-border) 🆕 v1.14.16
- [ ] 單線 / 雙線 / 圓角 / 陰影各出一份，框不壓到內容
- [ ] 自訂邊距與線寬生效；多頁整份都有框

#### 乘車證明整理 (transit-proof) 🆕 v1.14.17
- [ ] 上傳台鐵 / 高鐵乘車證明 PDF → 日期、交通工具、起訖、費用成表
- [ ] 多份批次 → 單一彙整表；CSV 匯出欄位齊全（公式注入已由
      `test_csv_injection.py` 守）

#### 掃描拼合 (scan-merge) 🆕 v1.11.0
- [ ] 拉入多張掃描（PDF / PNG / JPG）各含一塊內容 → 自動偵測出區塊
- [ ] **保留原彩色**：合成結果不轉黑白 / 不去彩（彩色內容飽和度不掉）
- [ ] **依原位置**：每塊擺到它在原掃描中的相對位置；重疊以紅框警示、不自動重排
- [ ] A4 預覽可拖曳移動、拖右下控點等比縮放
- [ ] **背景淨白**（預設開）把淡灰 / 微黃掃描底色提亮成純白，彩色內容不受影響；可關閉
- [ ] 產生單張 A4 白底 PDF（595×842 pt）
- [ ] 空白頁回 422（找不到內容）
- [ ] crop 取圖 ACL：非法 id 400、不存在 404、跨 user 擋
- [ ] **公開 API** `POST /tools/scan-merge/api/scan-merge`（form-data 多檔）回 PDF

#### PDF 轉文書檔 / 轉簡報 / 轉 Markdown / Markdown 轉辦公文件（補列）
- [ ] pdf-to-office：三引擎各轉一份、前後對照預覽出現、內容遺失 >50% 有紅字警示
- [ ] pdf-to-slides：直向 PDF 尺寸照原樣還原、產出載得進 Impress
- [ ] pdf-to-markdown：標題 / 表格 / 粗體保留；`include_images=true` 改回 ZIP
- [ ] markdown-to-doc：三輸出（PDF / docx / odt）＋ 頁面預覽 lightbox；
      odt 的 mimetype 是 text 不是 text-web（infilter 雷）
- [ ] image-to-pdf：多圖排序 / 旋轉 / 刪除、頁面大小選項生效

### 2.5 資安處理 🆕 全新分類

#### 文件去識別化 (doc-deident) 🆕
- [ ] 上傳 PDF 或 Office（先轉 PDF）
- [ ] 偵測 12 類：身分證 / 手機 / Email / 統編 / 信用卡 / 住址 / 銀行帳號 / ...
- [ ] 台灣身分證末碼校驗、統編加權檢查、信用卡 Luhn 都正確
- [ ] **遮蔽模式**：真 redact（`apply_redactions`），下載後原文無法復原
- [ ] **脫敏模式**：透明 redact + 蓋上 mask 文字（不是白底方塊）
- [ ] 處理完顯示頁面預覽縮圖 + lightbox 放大

#### PDF 密碼保護 (pdf-encrypt) 🆕
- [ ] 設開啟密碼 + 擁有者密碼 + 權限（禁列印/複製/編輯/擷取）
- [ ] AES-256 加密
- [ ] 下載後用 reader 開啟需要密碼

#### PDF 密碼解除 (pdf-decrypt) 🆕
- [ ] 已知密碼解除 → 輸出無密碼副本
- [ ] 多檔批次套用同一密碼
- [ ] 無開啟密碼但有權限限制：留空密碼也能解除權限

#### Metadata 清除 (pdf-metadata) 🆕
- [ ] 分析頁顯示 Info dict / XMP / 修訂歷史 / 標記
- [ ] 選擇性清除（個別勾選）
- [ ] 全部清除 → 輸出無痕副本
- [ ] 再次分析確認欄位為空

#### 隱藏內容掃描 (pdf-hidden-scan) 🆕
- [ ] 掃出 7 類：JS / 嵌入檔 / URI / launch action / 白字/頁面外 / 3D / 多媒體
- [ ] 風險清單顯示類型 + 位置
- [ ] 一鍵清除後再掃確認乾淨

#### 文件差異比對 (doc-diff) 🆕
- [ ] 上傳舊 / 新兩份 PDF
- [ ] 並排顯示 opcodes（紅=刪 / 綠=增 / 黃=改）
- [ ] Metadata 差異區塊
- [ ] 跨頁也能比對

### 2.6 設定 (admin)

#### 資產管理
- [ ] 上傳 + 去背 + 裁剪 + match-aspect
- [ ] 三類資產（stamp / signature / watermark / logo）分開列示

#### 公司資料
- [ ] 新增第二公司、欄位編輯、匯入匯出

#### 同義詞
- [ ] 新增條目並儲存

#### 表單範本
- [ ] 列表顯示已記住版型

#### 轉檔設定
- [ ] 拖曳排序、新增自訂路徑、儲存後重讀正確
- [ ] OxOffice / LibreOffice 優先序

#### 字型管理 🆕
- [ ] 內建 CJK 字型清單（Noto Sans TC / Noto Serif TC）
- [ ] 系統字型掃描 + 重掃按鈕
- [ ] 自訂字型上傳（.ttf / .otf）
- [ ] 刪除自訂字型
- [ ] pdf-editor 的字型 picker 能看到所有來源

#### LLM 設定 🆕
- [ ] 預設 enabled=False
- [ ] 填 endpoint / model 後測試連線
- [ ] 關閉時核心工具仍能正常運作

#### API Token 🆕
- [ ] 建立 / 列表 / 刪除 token
- [ ] 用 bearer 呼叫 `/api/*` 成功；無 token 回 401

#### 工作區設定 (admin/workspace) 🆕
- [ ] 啟用 / 停用切換；停用後重新整理任一工具頁，「存至工作區」「從工作區載入」按鈕與側欄「我的工作區」全部消失
- [ ] 設定每人容量額度 / 單檔上限 / 保留時數並儲存
- [ ] 「目前佔用」表列出各使用者佔用與總量

#### 記錄轉發（log forward）（2026-08-16 稽核補列 —— 原本整頁零驗收）
- [ ] 新增 syslog / CEF / GELF 目的地各一，測試送出有到（tcpdump 或收端確認）
- [ ] 收端不通時：retry 3 次後放棄，本機稽核出現 `audit_forward_failed`
- [ ] 停用的目的地不送

#### OCR 語言包管理（原本零驗收）
- [ ] 列出已裝 / 可裝語言；補裝一種後 pdf-ocr 立即可選
- [ ] 遠端 OCR 伺服器部署腳本可下載（install.sh / uninstall.sh）

#### 統編資料庫管理（原本零驗收）
- [ ] 財政部檔上傳走背景（頁面立即回 started，不卡住）
- [ ] 進度列會動；完成後筆數正確、vat-lookup 查得到新資料
- [ ] 排程自動更新設定存讀一致

#### 稽核記錄頁（原本只驗權限，沒驗頁面本身）
- [ ] 分頁列表、依 user / 事件類型 / 時間篩選有效
- [ ] CSV 匯出欄位齊全（公式注入由 `test_csv_injection.py` 守）
- [ ] 使用者篩選的模糊比對（LIKE）與 datalist 建議正常

### 2.6b 使用者工作區 (我的工作區) 🆕
- [ ] 任一 job 型工具（如蓋章 / 合併 / OCR）完成後出現「存至工作區」，按下後存入成功
- [ ] 任一上傳區出現「從工作區載入」，挑檔後該檔灌入工具流程（PDF/PNG，依工具 accept 過濾；非 PDF/PNG 工具不顯示此鈕）
- [ ] 「我的工作區」頁：容量條、檔案清單、下載 / 重新命名 / 刪除
- [ ] 額度已滿時再存 → 友善錯誤「容量已滿」
- [ ] 啟用認證時：A 帳號看不到 B 帳號的檔（清單與直連 file_id 皆不可）
- [ ] 保留時數到期後（或手動 retention sweep）過期檔被清除

### 2.7 介面

- [ ] 側欄品牌顯示 logo（深底）
- [ ] 首頁 hero 顯示淺底 logo + 三個特色 pill
- [ ] favicon 顯示
- [ ] 工具卡片依分類分組
- [ ] **每個工具有獨一無二的 icon 與顏色**（首頁與側欄一致）
- [ ] **側欄 active tile 白底延伸到右邊內容區**（無紫色縫隙）
- [ ] **側欄捲軸浮動**（只在 hover / 滾動時顯示）
- [ ] **搜尋支援中英文**（輸入 `form` 或 `填寫` 都能找到 pdf-fill）
- [ ] 視窗縮窄到 ≤ 900px：側欄收起、漢堡按鈕展開、項目正確點選
- [ ] **缺中文字型提示**：把系統中文字型移開（或在字型管理把它們隱藏）後開
      浮水印 / 插入頁碼 → 頁面最上方出現黃色提示；管理員看得到安裝指令，
      一般使用者看到「請聯絡管理員」。裝回字型後提示消失

### 2.8 術語檢查

- [ ] UI 使用台灣繁體用詞：圖片 / 軟體 / 字型 / 列印 / 檔案 / 訊息 / 影片 / 網路 / 伺服器 / 選單 / 螢幕 / 儲存 / 預設 / 設定
- [ ] 避免中國大陸用詞：圖像 / 軟件 / 字體 / 打印 / 文檔 / 信息 / 視頻 / 網絡 / 服務器 / 菜單 / 屏幕 / 保存 / 默認 / 設置

## 3. 跨平台檢查

### macOS
- [ ] OxOffice 已安裝時 `find_soffice` 命中 `/Applications/OxOffice.app/...`
- [ ] 原生 overlay 捲軸在 hover 時顯示

### Linux
- [ ] `apt install libreoffice` 後命中 `/usr/bin/soffice` 或 `/usr/bin/libreoffice`
- [ ] Ghostscript 若裝了 (`/usr/bin/gs`) 壓縮進階模式可用

### Windows
- [ ] LibreOffice 安裝後命中 `C:\Program Files\LibreOffice\program\soffice.exe`
- [ ] `shutil.which("soffice.exe")` 回 fallback 路徑
- [ ] `/admin/conversion` 顯示 Windows builtin 路徑且可使用
- [ ] Ghostscript `gswin64c.exe` 偵測

## 4. API 覆蓋檢查 🆕（v1.8.55 起完整列出，現 46 個工具）

每個工具至少 1 個 `/api/<tool-id>` endpoint（路徑：`/tools/<tool-id>/api/<tool-id>` 或 `/tools/<tool-id>/convert`）。發版前 curl 抽測：

> **這份清單靠人維護一定會漂**（歷史教訓：v1.14.20 核對時曾發現 7 支工具沒列、`API.md` 少 3 支 —— 已補，留此句是講「為什麼要有自動比對」）。
> `tests/test_api_doc_coverage.py` 會用**實際路由表**反向比對這份清單與 `github/API.md`，
> 漏列直接紅燈。手動抽測仍要做 —— 那支測試只保證「有寫」，不保證「寫的是對的」。

### 結構操作（PDF in / PDF out）
- [ ] `/tools/pdf-compress/api/pdf-compress` — POST file + preset → PDF
- [ ] `/tools/pdf-split/api/pdf-split` — POST file + pages → PDF or ZIP
- [ ] `/tools/pdf-rotate/api/pdf-rotate` — POST file + angle → PDF
- [ ] `/tools/pdf-pages/api/pdf-pages` — POST file + keep_pages → PDF
- [ ] `/tools/pdf-pageno/api/pdf-pageno` — POST file + style → PDF
- [ ] `/tools/pdf-nup/api/pdf-nup` — POST file + n → PDF
- [ ] `/tools/pdf-merge/api/pdf-merge` — POST files[] → PDF
- [ ] `/tools/pdf-encrypt/api/pdf-encrypt` — POST file + password → PDF
- [ ] `/tools/pdf-decrypt/api/pdf-decrypt` — POST file + password → PDF
- [ ] `/tools/pdf-border/api/pdf-border` — POST file + 框線設定 → PDF
- [ ] `/tools/pdf-bookmark/api/pdf-bookmark` — POST files[] + 書籤設定 → PDF（書籤 / 目錄頁）
- [ ] `/tools/pdf-seam-stamp/api/pdf-seam-stamp` — POST file + 章來源 → PDF（切片蓋在連續頁）
- [ ] `/tools/pdf-page-size/api/pdf-page-size` — POST file + paper → PDF（統一尺寸）

### 內容擷取
- [ ] `/tools/pdf-extract-text/api/pdf-extract-text` — POST file → JSON `{pages:[...]}`
- [ ] `/tools/pdf-extract-images/api/pdf-extract-images` — POST file → ZIP
- [ ] `/tools/pdf-attachments/api/pdf-attachments` — POST file → ZIP
- [ ] `/tools/pdf-wordcount/api/pdf-wordcount` — POST file → JSON `{words, chars, ...}`
- [ ] `/tools/pdf-hidden-scan/api/pdf-hidden-scan` — POST file → JSON `{findings, totals}`
- [ ] `/tools/pdf-metadata/api/pdf-metadata` — POST file + clear_* flags → cleaned PDF

### 用印 / 簽名 / 浮水印 / 表單
- [ ] `/tools/pdf-stamp/api/pdf-stamp` — POST file + stamp_image → PDF
- [ ] `/tools/pdf-watermark/api/pdf-watermark` — POST file + text → PDF
- [ ] `/tools/pdf-fill/api/pdf-fill` — POST file + company_id → PDF

### 註解
- [ ] `/tools/pdf-annotations/api/pdf-annotations` — POST file → JSON
- [ ] `/tools/pdf-annotations-strip/api/pdf-annotations-strip` — POST file → PDF
- [ ] `/tools/pdf-annotations-flatten/api/pdf-annotations-flatten` — POST file → PDF

### 格式轉換
- [ ] `/api/convert-to-pdf` (in main.py) — POST file → PDF (office-to-pdf)
- [ ] `/tools/office-convert/formats` — GET → 可用家族與目標格式（target id 因安裝而異）
- [ ] `/tools/office-convert/convert` — POST files[] + target → 原格式或 ZIP（async job）；
      跨類（試算表配文書檔的 target）與不存在的 target 都應是 400 不是 500
- [ ] `/tools/pdf-to-image/convert` — POST file → ZIP/PNG
- [ ] `/tools/pdf-to-office/convert` — POST file → docx/odt（async job）
- [ ] `/tools/image-to-pdf/api/image-to-pdf` — POST files[] → PDF
- [ ] `/tools/scan-merge/api/scan-merge` — POST files[] → 單張 A4 白底 PDF
- [ ] `/tools/pdf-to-slides/convert` — POST file → pptx/odp（async job）
- [ ] `/tools/pdf-to-markdown/api/pdf-to-markdown` — POST file → `text/markdown`；
      `include_images=true` 改回 ZIP（**回應型別會變**）
- [ ] `/tools/markdown-to-doc/api/markdown-to-doc` — POST file 或 text + format → pdf/docx/odt；
      非法 format 應是 400 不是 500

### 文字工具
- [ ] `/tools/text-list/api/text-list` — POST text → JSON
- [ ] `/tools/text-diff/api/text-diff` — POST text → JSON / HTML
- [ ] `/tools/text-deident/api/text-deident` — POST text → JSON
- [ ] `/tools/translate-doc/api/translate-doc` — POST file → translated file

### 文件處理
- [ ] `/tools/doc-deident/api/doc-deident` — POST file → de-identified
- [ ] `/tools/doc-diff/api/doc-diff` — POST file_a + file_b → JSON
- [ ] `/tools/pdf-editor/api/pdf-editor` — POST file + edits json → PDF
- [ ] `/tools/pdf-ocr/api/pdf-ocr` — POST file + langs → `{job_id}` (async)

### 查詢 / 分析 / 檢核
- [ ] `/tools/vat-lookup/api/vat-lookup` + `/api/vat-lookup/batch`
- [ ] `/api/vat-lookup/{vat}` (path-style GET in main.py)
- [ ] `/tools/einvoice-scan/api/einvoice-scan` + `/api/backend-status`
- [ ] `/tools/submission-check/api/self-entities` (CRUD)
- [ ] `/tools/transit-proof/api/transit-proof` — POST files[] → JSON `{ok, count, entries, failed}`；
      **認不出的檔不會讓整批失敗**（HTTP 仍 200），要看 `failed` 是不是空的

### 共通驗證項
每個 endpoint 至少要：
- [ ] 拒絕非 PDF / 空檔（400）
- [ ] 啟用認證時 token 驗證 + ACL（`upload_owner.require()` 防跨 user 取檔）
- [ ] 大檔（> 限額）回 413 而不是 OOM
- [ ] 回應 `Content-Disposition` 中文檔名 RFC 5987（走 `http_utils.content_disposition`）

### 自動化覆蓋（理想）
新加 endpoint 由兩支既有測試守：`tests/test_api_doc_coverage.py`（路由表 ↔ 文件雙向比對）與 `tests/test_broken_input_no_500.py`（**全部**工具 POST 端點 × 壞輸入不可 500，從路由表自動列舉，新工具自動被涵蓋）。發版前 `uv run pytest tests/test_api_doc_coverage.py tests/test_broken_input_no_500.py -q` 必綠。（原本這裡寫「tests/test_api_endpoints.py（待補）必綠」—— 一個不存在的檔案當發版門檻，指令必然失敗，2026-08-16 稽核改掉。）

## 4.6 非工具 API（管理 / 作業 / 通知）🆕 v1.14.56

§4 只涵蓋「每個工具至少一支 API」。**管理區、作業佇列、通知這些 API 之前
一條驗收都沒有** —— 它們同樣是對外的攻擊面，而且改壞了整個管理功能會死掉
（2026-08-26 稽核補上）。清單由 `tests/test_test_plan_coverage.py` **從路由表
自動比對**，新增端點沒列進來就紅燈，不靠人記得。

> 判準都一樣：①未登入一律拒絕 ②一般使用者碰管理端點一律拒絕
> ③壞輸入回 4xx 不可 500 ④寫入端點不帶 CSRF token 要被擋。
> 這四條由 `tests/test_authz_boundaries.py`、`tests/test_broken_input_no_500.py`、
> `temp/sec-audit/pentest.py` 自動涵蓋；下面列的是**功能**驗收。

### 管理區設定 API
- [ ] `POST /admin/api/check-latest-version` — 回目前版本與最新版本；連不到網路時要回錯誤訊息，不可讓頁面一直轉
- [ ] `GET|POST /admin/api/llm/settings` — 存檔後重新整理值要留著；數值欄位（逾時、並行數、句數上限）超範圍要被 clamp
- [ ] `GET /admin/api/llm/models` — 列出遠端模型；伺服器連不上時回錯誤訊息不可拋例外
- [ ] `POST /admin/api/llm/test-connection` — 成功 / 失敗都要有明確訊息（失敗訊息不可洩漏內部路徑或憑證）
- [ ] `POST /admin/api/ocr-langs/set-engine` — 切換 easyocr / tesseract 後，OCR 工具實際用的引擎要跟著改
- [ ] `POST /admin/api/ocr-langs/set-quality`、`POST /admin/api/ocr-langs/switch-active` — 設定有寫進去且重啟後仍在
- [ ] `GET /admin/api/ocr-langs/external/status`、`POST /admin/api/ocr-langs/external/save`、
      `POST /admin/api/ocr-langs/external/test` — 遠端 GPU OCR 設定；**test 要真的打對方**，不可只回 200
- [ ] `GET /admin/api/settings-export/categories` — 類別清單要跟實際可匯出的項目一致
- [ ] `POST /admin/api/tokens/create`、`POST /admin/api/tokens/revoke`、`POST /admin/api/tokens/enforce`
      — 建立的 token 立即可用、撤銷後立即失效、enforce 開關會改變未帶 token 的行為

### 作業佇列 API
- [ ] `GET /api/jobs/{job_id}` — 進度 / 狀態；**別人的作業要拿不到**
- [ ] `POST /api/jobs/{job_id}/cancel` — 取消後狀態要變、正在跑的要真的停
- [ ] `GET /api/jobs/{job_id}/download`、`GET /api/jobs/{job_id}/download/{_filename}`、
      `GET /api/jobs/{job_id}/download-png` — 歸屬驗證；作業過期回 410 不可 500
- [ ] `POST /admin/jobs/api/cancel/{job_id}` — 管理員可取消任何人的作業
- [ ] `POST /admin/jobs/api/pause` — 暫停後新作業排隊不派送，恢復後會繼續
- [ ] `GET|POST /admin/jobs/api/priority-users` — **順序就是優先序**，讀回來不可以被重新排序（v1.14.7 踩過：讀取時 `sorted()` 把拖好的順序洗掉）
- [ ] `GET /admin/jobs/api/user-search` — 模糊比對；非管理員不可用

### 通知 / 其他
- [ ] `GET /api/my/inbox` — 只回自己的通知
- [ ] `POST /api/my/inbox/seen` — 標記已讀；別人的通知 id 標不動
- [ ] `POST /api/llm-review` — LLM 逐欄校驗；LLM 關閉時要回明確訊息不可 500
- [ ] `PUT|DELETE /tools/submission-check/api/self-entities/{entity_id}` — 只能改 / 刪自己的；別人的 id 要被拒

### 管理頁（每一頁至少開得起來且功能可用）

清單同樣由 `tests/test_test_plan_coverage.py` 從路由表比對，新增管理頁沒列進來會紅燈。

- [ ] `/admin/api-tokens` — 建立 / 撤銷 token，開關 enforce
- [ ] `/admin/log-forward` — 新增目的地、三種格式（syslog / cef / gelf）、送測試訊息
- [ ] `/admin/synonyms` — 同義詞新增 / 刪除，會影響表單填寫的欄位對應
- [ ] `/admin/templates` — 範本列表與刪除
- [ ] `/admin/vat-db`、`/admin/vat-db/info`、`/admin/vat-db/schedule` — 統編資料庫下載 / 上傳匯入（背景執行，頁面不可卡住）、排程設定、狀態顯示
- [ ] `/admin/directory/tree`、`/admin/directory/user-roles`、`/admin/directory/group-roles` — 目錄瀏覽的樹狀展開與角色指派（含 OU / 群組 / 個人三種對象）
- [ ] `/admin/system-status/databases` — 各資料庫大小與最舊一筆時間
- [ ] `/admin/audit/export.csv` — 稽核記錄匯出；**公式注入防護**（`=` 開頭的欄位要被前綴處理，見 TEST_PLAN_SECURITY）

## 4.5 壓力測試 🆕（v1.7.50+）

詳細跑法 / 驗收門檻 / 歷史紀錄見獨立文件 **[STRESS_TEST.md](STRESS_TEST.md)**（涵蓋 1 / 5 / 10 / 30 / 50 並行使用者場景，輕重型工具混合）。

- [ ] 1 user 跑過：p95 < 500 ms 100% 成功
- [ ] 5 users 跑過：吞吐有上升、成功率 100%
- [ ] 10 users 跑過：p95 < 1500 ms、成功率 ≥ 99%
- [ ] 30 users 跑過：成功率 ≥ 98%
- [ ] 50 users 跑過：成功率 ≥ 95%
- [ ] 任一階段成功率突降 → 看 server log 找 root cause

## 5. 發版前最終檢查

1. `git status` 沒有未追蹤的暫存檔
2. `pytest` 全數綠燈
3. **文件 / 設定備份涵蓋度檢查**（v1.14.6 起列為發版必跑）：
   ```bash
   python tools/check_docs_tool_coverage.py        # 工具是否都寫進 README / 介紹站
   python tools/check_settings_export_coverage.py  # 新設定檔是否都納入「設定備份 / 匯入」
   python tools/check_version_consistency.py       # 五處版本號一致
   ```
   後者是 v1.14.6 補上的：`settings_export.CATEGORIES` 是**人工維護**的清單，加新設定
   檔漏加不會有任何錯誤訊息，只有客戶搬機還原後才會發現設定不見了（該版一次補了
   16 項，其中 `sso_settings.json` 從 v1.12.0 起就沒被備份過，而「認證設定」分類的
   說明卻寫著含 OIDC / SAML）。
4. **認證開 / 關兩種模式的全功能矩陣**（v1.14.6 起列為發版必跑）：
   ```bash
   uv run pytest tests/test_auth_modes_matrix.py -v
   ```
   這個專案幾乎每條路徑都有兩種行為（工作區儲存鍵、作業歸屬、通知偏好、權限閘、
   admin 頁可見性…），而**很容易只顧到一邊** —— 例如新的 admin 頁忘了掛權限
   dependency，在單機模式下完全看不出來（那時本來就全員放行）。人工把 41 個工具
   在兩種模式各點一遍不現實，所以用同一組斷言自動跑兩遍。
5. **資安測試計畫全數通過**（v1.14.6 起）：見 `TEST_PLAN_SECURITY.md` ——
   自動化 18 支測試檔 + 滲透測試腳本（7 類 + 反向對照）+ bandit + ZAP 兩目標
   （High / Medium / Low 全 0）。
6. **OWASP regression 全數綠燈**（v1.5.3 起列為發版必跑）：
   ```bash
   uv run pytest -v \
       tests/test_owasp_top10.py \
       tests/test_llm_url_ssrf.py \
       tests/test_path_traversal_audit.py \
       tests/test_version_consistency.py \
       tests/test_redos_ad_dn.py
   ```
   `test_version_consistency` 確保 `app/main.py:VERSION` / `pyproject.toml` / `uv.lock` / `README` / `CHANGELOG` 五處版本號完全一致（v1.5.3 慘案訓練）。
7. 重啟 server，所有路由 200（以 curl 跑 1.1 列表）
8. 手動跑一輪 2.x 清單
9. 跑完 §6 「歷史回歸案例」清單
10. 更新 `app/main.py` `VERSION` + `pyproject.toml` `version` + `github/CHANGELOG.md` 加一筆 + `github/README.md` 標題版號
11. 重啟，確認 footer 顯示新版本號
12. 確認停用的工具（`aes-zip`）仍保留程式碼但未顯示於側欄／首頁
13. **推 GitHub 後 5–15 分鐘**檢查 GitHub native scan：
    - <https://github.com/jasoncheng7115/jt-doc-tools/security/dependabot> — Open alert 數應持平或下降
    - <https://github.com/jasoncheng7115/jt-doc-tools/security/code-scanning> — CodeQL 新警告當天處理或記入「已知議題」

## 6. 歷史回歸案例（每次發版必過）

每條附「修在哪個版本」+「測試方法」+「預期行為」。任一條 fail 視為 regression 必須修復才能發版。

### 6.1 pdf-editor

- [ ] **OCR 中文亂碼擷取** (v1.2.4 / v1.2.5)
  - 上傳 `~/Nextcloud/文件檔/Proxmox VE 手冊/1 Proxmox VE 準備與安裝.pdf`
  - 點選原 PDF 上「網路基本設定」→ 應顯示「網路基本設定」（非「翕⊕ㄱ」之類）
  - 點選「登入系統」→ 應顯示「登入系統」
  - 預期：自動 OCR 重建、訊息「已用 OCR 自動辨識…」

- [ ] **OCR 西文字型用 eng-only** (v1.3.1)
  - 同上 PDF，點選「Proxmox VE」(OpenSans-Bold 字型) → 應顯示「Proxmox VE」(非「ProXimoxX VE」)

- [ ] **OCR 短標題 padding 不抓鄰近 span** (v1.2.5)
  - 「網路基本設定」OCR 結果不應含前後鄰近文字（不是「VE 網路基本設定一」）

- [ ] **OCR 等待時提示** (v1.3.4)
  - 點選需 OCR 的文字 → 500ms 後狀態列應顯示「辨識中…（原文字字型無 Unicode 對應表，正在 OCR 重建文字）」

- [ ] **既有透明 PNG 擷取保留 alpha** (v1.3.3)
  - PDF 內含透明背景 + 陰影圖片時，點選 → 擷取出來的圖**不可變黑底**

- [ ] **undo 到最早不會 redact 既有物件** (v1.1.99)
  - 載入 PDF → 點擷取一段文字 → undo 回到最早
  - 預期：BG 重新渲染後，原 PDF 文字仍完整顯示（不該變空白）

- [ ] **存檔後既有物件不重影** (v1.1.97)
  - 點擷取既有文字後存檔 → 預覽 BG 已含新文字，且 Fabric 上的同位置物件 fade 到 opacity 0.01
  - 預期：不該看到「BG 文字 + Fabric 文字」雙層重影

- [ ] **下載按鈕** (v1.1.96)
  - 純 anchor + download attribute；按下要觸發瀏覽器下載 dialog
  - 若特定瀏覽器不下載，先請使用者開無痕視窗排除擴充功能

### 6.34 v1.14.54 — 壞掉的文字對應表要從字形反查，不可以用 OCR（每次發版必過）

客戶回報：PDF 編輯器點文件上原本的中文，文字框裡整排變成 `••••••`。

- [ ] **圓點型的擷取失敗要被抓到**
  - `tests/test_placeholder_extraction.py` 全綠
  - 舊的 `_looks_garbled` 對 `•`（U+2022，一般標點區）是無感的 ——
    `●`(U+25CF) / `□`(U+25A1) 落在 Geometric Shapes 所以抓得到，圓點抓不到
- [ ] **判斷靠寬度不靠字元**
  - 真的點引導符（`目錄………12`）每點只有 0.2～0.35 字寬 → 不可被判為壞掉
  - 擷取壞掉時每個「點」佔滿一個中文字寬
- [ ] **還原走字形反查，不是 OCR**
  - `tests/test_glyph_text_recovery.py` 全綠；`recovered_from_font=true`、
    `ocr_used=false`
  - 反查**不可以多吃隔壁的字**（水平範圍必須是半開區間 —— 下一個字的原點
    正好落在這個框的右緣）
  - 反查到控制字元一律當作查不到（實測在真實表單上踩到 NUL）
  - 查不齊時**整段放棄**，不可以吐半段正確半段問號
- [ ] **不可以把本來正確的文字改壞**（這條比原本的 bug 更嚴重）
  - 拿 `temp_pdfs/` 全部樣本掃一遍被判不可靠的 span，比對「反查結果 vs 原擷取」
  - 判準：**不同的必須是 0**（v1.14.54 實測 相同 54｜不同 0｜查不到 100）
- [ ] **旗標語意**：字形反查成功時 `extracted_text_unreliable` 要是 false
  - 前端是先看這個旗標就直接放棄，忘了清會變成「已經還原出文字，使用者
    卻還是拿不到」（真實瀏覽器測試抓到的）
- [ ] **內部自動 OCR 不可以打掛服務**
  - 缺 AVX2 的機器上本機 EasyOCR 會 SIGILL；`tests/test_ocr_avx2_guard.py` 全綠
  - 注意「選 tesseract 也會反向掉回 EasyOCR」那條路徑
  - OCR 工具的手動引擎切換行為**不變**（沿用「不自動退」的決定）
- [ ] **真實瀏覽器**：`temp/editor-dots/cdp_dots_test.py` 9/9

### 6.35 v1.14.55 — 端點不可以把整站鎖住（每次發版必過）

2026-08-26 使用者實測：跑「文件去識別化」時**全站兩分鐘完全不回應**。
日誌是「事件迴圈被卡住 116.4 秒 / 慢請求 116.9 秒 POST /tools/doc-deident/process」，
而當下作業佇列是空的 —— 同步的重活直接跑在事件迴圈裡。

- [ ] `tests/test_no_blocking_endpoints.py` 全綠
- [ ] 同形狀的端點**只准變少不准變多**（`KNOWN_REMAINING`），新工具不可以再犯
- [ ] 判讀陷阱：watchdog 警告寫「調低最大同時作業數可緩解」，兇手是同步的
      請求處理函式時**照那句去調完全沒用**

### 6.36 v1.14.55 — 文件去識別化的替換模式（每次發版必過）

- [ ] **原值一定要真的消失**：處理完把 PDF 的文字抽出來，原本的身分證 / 電話
      **一個都不可以還找得到**（看起來處理過了但抽得出來，是這類工具最要命的失敗）
- [ ] **同一個原值固定對應同一個假值** —— 否則一份報表裡同一個客戶會變三個人
- [ ] **預設的假值不可以通過檢查碼**（不會撞到真人資料）；打開「可通過驗證」
      之後身分證 / 統編 / 信用卡要真的通過（否則「適合拿去測試」是空話）
- [ ] Email 用 `example.com`、IP 用 `192.0.2.x`、MAC 用 `00:00:5E`（保留範圍）
- [ ] **格式要保住**：身分證 10 碼、信用卡 16 碼、銀行帳號的分隔符號位置不變
- [ ] **太長要自動縮字**塞回原框（不縮會壓到隔壁欄位，而且是無聲的）
- [ ] 手動改過的替換值，切換「可通過驗證」開關時**不可以被洗掉**
- [ ] 自訂字詞（`/find`）：找得到位置、有建議的替換值、**別人的 upload_id 拿不到東西**
- [ ] 公開 API 同步支援（`mode=replace` / `replacements` / `valid_checksum`）
- [ ] 真實瀏覽器：`temp/deident-replace/cdp_replace_test.py` 8/8

### 6.37 v1.14.56 — 端點一律不可以鎖住事件迴圈（每次發版必過）

- [ ] `tests/test_no_blocking_endpoints.py` 全綠，且 `KNOWN_REMAINING` **是 0**
- [ ] 新端點要算縮圖 → `await pdf_preview.render_page_png_async(...)`
- [ ] 新端點要轉檔 → `await office_convert.convert_to_pdf_async(...)`（docx / odt 同）
- [ ] 其他重活 → 包成同步閉包再 `await asyncio.to_thread(_work)`
- [ ] **判定的兩個陷阱**（都踩過）：
      ①「巢狀函式裡的重活不算」是錯的 —— 包成閉包正是修法本身，只看巢不巢狀的話，
      有人把 `await to_thread(_work)` 改回 `_work()` 反而抓不到。要看**有沒有真的
      派到別的執行緒**（`to_thread` / 背景作業）。
      ②交給 `job_manager` 的閉包**不算阻塞**，把它們算進來會讓數字虛胖，
      虛胖的指標沒人會認真看。
- [ ] 管理區的端點定義在 `build_router()` **裡面**，縮排比模組層級多一層 ——
      自動化改寫時縮排要從 AST 的 `col_offset` 取，寫死會產生
      `await outside async function`

### 6.38 v1.14.56 — 測試計畫本身要有守門（每次發版必過）

發版門檻是照這份計畫跑的，**計畫漏了什麼那塊就等於沒驗過**，而且報告看起來
仍然全綠。

- [ ] `tests/test_test_plan_coverage.py` 全綠
- [ ] 新工具 / 新 API / 新管理頁沒寫進計畫要紅燈（從路由表與註冊表實算，
      不寫死期望值 —— 寫死的數字自己就是下一個會漂的東西）
- [ ] 計畫裡**指令引用的檔案**必須存在（照抄會 file not found 的那種，
      2026-08-16 稽核踩過：一個不存在的測試檔被當成發版門檻）
- [ ] 掃描只掃**指令行**不掃說明文字 —— 說明裡會引用「當初寫錯的檔名」當反例

### 6.39 v1.14.57 — 壞掉的文字對應表：抽取類工具也要還原（每次發版必過）

v1.14.54 只修了 PDF 編輯器；擷取文字、字數統計、逐句翻譯走同一條路徑、
同一個盲點（同一份檔案抽出來是 `••••••`，字數算成 0 個中文字）。

- [ ] `tests/test_extract_text_glyph_repair.py` 全綠
- [ ] **正常的 PDF 一個位元都不可以變** —— `page_text_repaired()` 對正常頁面
      要回 `None`（代表「照原本的路徑走」）。這是整個修法的安全閥：為了救
      1% 的壞檔把 99% 的好檔弄出細微差異（斷行、空白）是更糟的結果
- [ ] 拿 `temp_pdfs/` 真實樣本掃過，**被判成「壞掉」的頁面數必須是 0**
- [ ] 三種回傳值語意不可混：`None`（本來就好）/ 還原後的字 / `""`（確定壞掉
      但救不回，呼叫端該丟掉那段）
- [ ] 字數統計檔案裡有**三處**各自抽取文字 —— 只改一處會出現「API 對了、
      網頁還是錯的」

### 6.40 v1.14.57 — 真實樣本要被拿來測（每次發版必過）

`temp_pdfs/` 有 29 份真實廠商表單 + 6 份 Office 檔，但 2026-08-27 盤點時發現
**只有「表單自動填寫回歸」在用**，其餘工具的測試全部跑合成 PDF。而真正的意外
都在真實檔案裡：壞掉的文字對應表、Wingdings 核取方塊、直書、掃描件、奇怪的
表格版型、旋轉頁、缺字型 —— 這些合成檔造不出來（兩天內連續踩到三種）。

- [ ] `tests/test_real_samples_smoke.py` 全綠（22 支工具 × 29 份樣本，約 2 分鐘）
- [ ] 判準是「**不可以炸掉**」：不回 5xx、不拋例外
- [ ] 另外驗**抽文字類工具真的抽得到字** —— 只驗狀態碼不夠，客戶回報的那次
      就是一路回 200 但內容是一整片圓點
- [ ] 樣本沒了要**看得見地 skip**，不可以安靜跳過（少跑一項比跑出紅字危險，
      因為報告看起來仍然是綠的）
- [ ] **樣本含客戶資料**：測試只看狀態碼與結構，不印內容、不寫出任何檔案

### 6.41 v1.14.58 — 刪使用者不可以卡住整站（每次發版必過）

客戶回報：「刪 user 會卡住，多刪幾個系統就像掛掉」「超久才回應」。三個原因疊在一起。

- [ ] `tests/test_db_query_plans.py` 全綠 —— **熱路徑的 SQL 不可以出現 `SCAN`**
      （`group_members` 的主鍵是 `(group_id, user_id)`，用 user_id 單獨查
      **用不到**那個索引，而刪 users 會觸發它的 CASCADE）
- [ ] `tests/test_no_blocking_endpoints.py` 的 `MUST_OFFLOAD` 全綠 ——
      重活**藏在被呼叫的函式裡**時掃描抓不到，只能逐支列管
- [ ] 前端每筆操作後**不可以 `location.reload()`** —— 使用者管理頁要統計整個
      目錄（客戶 18,611 位），刪十筆等於重算十次
- [ ] 這類缺陷**從功能測試看不出來**（功能完全正確，只是慢，而且要資料量夠大
      才看得出來）→ 一律用查詢計畫驗，不要用計時（計時在小資料上永遠是綠的）

### 6.42 v1.14.58 — 批次刪除使用者（每次發版必過）

- [ ] `tests/test_users_bulk_ops.py` 的批次刪除項全綠
- [ ] 內建管理員 / 內建稽核員 / 自己 → 跳過但**其他照刪**（一顆地雷不該讓整批停擺）
- [ ] **先整批試算再動手**：刪完一個管理員都不剩就整批中止（刪到一半才發現
      就救不回來了）
- [ ] 寫稽核（不可逆的操作一定要留紀錄）
- [ ] 前端要求**打字確認數量**
- [ ] **測試的兩個陷阱**（都踩過）：①`admin_session` 登入的就是 seed 管理員本人，
      拿他去刪自己會被「不可刪除自己」擋掉 —— seed 那條防線等於沒驗到，要用
      **另一個管理員**登入才測得到；②端點與 `user_manager` 兩層防線**各自都夠**，
      只拔一層變異不會紅，要同時拔掉才驗得出測試有沒有牙齒

### 6.43 v1.14.58 — 作業的三個時間點（每次發版必過）

- [ ] `tests/test_job_timestamps.py` 全綠
- [ ] 送出 / 開始 / 結束都要**存進資料庫**（開始時間原本只活在記憶體裡）
- [ ] **從資料庫還原作業時要帶回 `started_at`** —— 少了這步，之後任何一次
      upsert 都會把值寫成 NULL，資料靜靜地不見
- [ ] 舊資料沒有這個值 → 顯示「—」，**不可以拿送出時間硬湊**

### 6.44 v1.14.58 — 自訂字型的顯示名稱（每次發版必過）

- [ ] `tests/test_font_display_names.py` 全綠
- [ ] 上傳後顯示的是**字型檔內建的名稱**，不是檔名
- [ ] 管理員可以自訂；**留空會退回內建名稱**，不是退回檔名
- [ ] 名稱會反映到 **PDF 編輯器的字型下拉**（`label` 欄位）—— 後端有值但下拉
      沒變等於沒做
- [ ] 壞掉的字型檔要安靜回空字串，不可以讓整份清單掛掉
- [ ] 系統字型不可改名（掃出來的，改了下次掃描就沒了）；`custom:` 之外的
      id 一律拒絕，路徑要限制在自訂字型資料夾內

### 6.45 v1.14.59 — 可上傳的檔案大小（每次發版必過）

- [ ] `tests/test_upload_limits.py` 全綠
- [ ] 反向代理的上限用 `Expect: 100-continue` 問，**不可以真的傳檔案去測**
      （伺服器會在讀完 body 前就回應，測出來的數字不可信）
- [ ] **問不到要說問不到** —— 回一個看起來像答案的數字比沒有這個功能更糟
- [ ] 清單裡「可調 / 寫死」的標示要誠實（說可調卻寫死 → 管理員會去找一個
      不存在的設定欄位）
- [ ] 探測端點**不可以接受呼叫端指定目標** —— 那就是 SSRF
- [ ] `POST /admin/api/upload-limit/probe` —— 只有管理員可用；目標綁死在當前
      連線的 Host；會寫稽核（`upload_limit_probe`）

### 6.46 v1.14.59 — 以文件為單位的快取不可以放模組層級（每次發版必過）

- [ ] `tests/test_glyph_text_recovery.py` 的 `test_page_cache_lives_on_the_document` 全綠
- [ ] **不可以用 `id(doc)` 當鍵**：文件每個請求開一份、用完就關，`id()` 會被
      重複使用 → 下一份文件讀到上一份的資料 → **反查出別份文件的字**，無聲
- [ ] 判斷指紋：**單跑全綠、合跑失敗，而且每次失敗的項目還不一樣**
- [ ] 行為測試（跨文件不污染）對這個變異**沒有牙齒**（id 重用不保證發生），
      要靠結構測試釘住「快取掛在文件物件上」

### 6.47 v1.14.60 — 面板收折與標題圖示（每次發版必過）

- [ ] `temp/ui-1458/cdp_sysstatus.py` 7/7（真實瀏覽器）
- [ ] **收折要驗「點了之後 class 真的變」**，不是只看標題存在 —— 這個 bug 的
      本質就是「看起來一樣、但點了沒反應」
- [ ] 全站收折機制的條件是 **`<h2>` 必須是 `.panel` 的第一個子元素**
      （`static/js/toast.js`）。包在 `<div>` 裡就接不到，而且完全無聲
- [ ] **HTML 樣板裡的字串不可以寫 markdown** —— `**粗體**` 會原樣印出星號。
      判準：頁面文字裡不可以出現 `**`
- [ ] 新增區塊標題時挑**語意相符**的圖示；圖示名稱要真的存在
      （`components/icons.html`，用不存在的名字不會報錯、只是沒圖）

### 6.2 圖片轉 PDF (image-to-pdf, v1.3.0+)

- [ ] **拖曳多張圖片** → 縮圖網格出現
- [ ] **再加圖片** → 已存在的縮圖不被覆蓋，新的加在後面
- [ ] **拖曳重新排序** → 順序變更後產生的 PDF 對應新順序
- [ ] **逐頁旋轉** (↺ / ↻) → 縮圖視覺旋轉、PDF 對應頁旋轉
- [ ] **逐頁刪除** (×) → 縮圖移除，產出 PDF 不含該頁
- [ ] **頁面大小：原始** → 每頁尺寸等於圖片尺寸
- [ ] **頁面大小：A4** → 全部頁面 A4，圖片置中、依比例自動轉向
- [ ] **邊距 10mm** → 圖片離邊 10mm
- [ ] **背景色** → 非「原始」時 letterbox 區用此色
- [ ] **EXIF 自動正向** → 手機照片不應躺著
- [ ] **HEIC / WebP / TIFF** 格式接受
- [ ] **公開 API** `POST /tools/image-to-pdf/api/image-to-pdf`（form-data 多檔）回 PDF 檔
- [ ] 縮圖右上紅色 × **一直顯示**（不靠 hover）
- [ ] 設定面板 4 列 label 對齊整齊、說明文字看得出歸屬哪一列

### 6.3 jtdt CLI

- [ ] **`jtdt`（無參數）印分組指令清單** (v1.3.6)
  - 不應只印一行 `usage: jtdt [-h] {start,stop,...}`
  - 應分「服務控制 / 升級與維護 / 緊急復原」三組

- [ ] **`jtdt update` 拒絕降版** (v1.3.5)
  - 在 origin 改成過期 file:// 的測試環境上跑 `jtdt update`
  - 預期：偵測新版 < 舊版 → abort + 還原 + 印 git remote 修復指令

- [ ] **`jtdt update` 處理 force-pushed remote** (v1.2.3)
  - 用 `git reset --hard origin/main` 而非 `git pull --ff-only`
  - 預期：force-pushed 的 origin 也能順利升級，不會「Not possible to fast-forward」

- [ ] **`jtdt update` 自動補裝系統相依** (v1.2.2+)
  - 缺 tesseract 時自動 `apt/brew/winget install`
  - 失敗只 warn 不 abort 升級
  - 結尾印「相依套件狀態」表

- [ ] **`jtdt auth show / disable / set-local`** 不需 service running 也能跑（緊急復原）
- [ ] **`jtdt reset-password <user>`** 同上

### 6.4 相依套件檢查 (admin/sys-deps, v1.2.3+)

- [ ] 設定區第一個項目顯示「**相依套件檢查**」
- [ ] 頁面顯示 stat cards（就緒 / 必要相依缺 / 選用相依缺）
- [ ] tesseract / Office / CJK 字型 / pytesseract / Pillow 各一列
- [ ] 缺漏項目顯示對應平台的安裝指令（Linux: apt / macOS: brew / Windows: winget）
- [ ] `GET /admin/api/sys-deps` 回 JSON

### 6.5 認證設定 lockout 防呆 (v1.3.14)

- [ ] **未啟用認證時，/admin/auth-settings 下方 backend 設定整段鎖定**：
  - 黃底 banner「請先啟用認證才能設定 backend」顯示
  - LDAP 表單灰階、不可輸入、tab 跳過 (`inert` 屬性)
  - 「驗證測試」按鈕同樣 inert
- [ ] **backend 防線**：未啟用時 `POST /admin/auth-settings/ldap-save` 直接回 HTTP 409，body 含「Cannot configure LDAP/AD backend before authentication is enabled」
- [ ] **完整鎖死情境驗證**：未啟用狀態下 curl POST `backend=ad` → 1) 回 409、2) `auth_settings.json` 內 `backend` 仍是 `off`（未被改）、3) `GET /admin/auth-settings` 仍回 HTTP 200。**任何一步失敗 = 客戶會被鎖在外面，必修。**

### 6.6 升級流程 (含 DB migration)

- [ ] 從 v1.0.x 升到目前版本，所有 migration 跑完不報錯
- [ ] v3 migration: pdf-diff → doc-diff 既有 perms 遷移
- [ ] v4 migration: 既有 pdf-to-image 權限自動授予 image-to-pdf
- [ ] 升級後 default-user / clerk role 含新工具權限
- [ ] 升級後 service user 仍能讀 .venv 內檔案（chown 還原正確）

### 6.7 用詞檢查（push 前 grep）

```bash
# 不應出現的中國用語：
grep -rnE "回滾|軟依賴|硬依賴|系統依賴(?!\s*$)|圖像(?![幾何])|軟件|字體|打印|文檔|信息|視頻|網絡|服務器|菜單|屏幕|保存|默認|設置" \
  app/ static/ github/CHANGELOG.md github/README.md --include='*.py' --include='*.html' --include='*.md'
```

- [ ] grep 結果應為空（除了 memory / to_github.md 的解釋脈絡）
- [ ] 「依賴」→「相依」、「回滾」→「還原」、「硬刷」→「強制重新整理」

### 6.8 landing page (`docs/`)

- [ ] 「線上 PDF 工具的隱憂」/ 「地端自架 + 開源 才能安心」字級 24px / 字重 800
- [ ] 工具總數 / 「N 個工具」與 README hero 一致
- [ ] 截圖無內網 IP / browser chrome
- [ ] hero / 安裝指令 tab 切換正常

### 6.9 v1.4.0 — 11 項使用者建議（每次發版必過）

#### 6.9.1 OxOffice X11 runtime libs（fix #9 #10 #11）

- [ ] Fresh Linux Debian/Ubuntu minimal 上跑 `bash install.sh`：自動 `apt install libxinerama1 libxrandr2 ...`，office-to-pdf 不會炸 `libXinerama.so.1`
- [ ] 既有客戶 `sudo jtdt update`：偵測到缺 X11 lib → 自動 `apt install`，summary 表顯示「OxOffice X11 libs：完整」
- [ ] `/admin/sys-deps` 出現「OxOffice / LibreOffice 執行時依賴 X11 lib」項目，全數綠燈
- [ ] 上傳 .docx 到 office-to-pdf → 成功轉成 PDF（不是 oosplash error）
- [ ] 文件差異比對 PDF vs DOCX → 不會卡在「office 轉 PDF 失敗」

#### 6.9.2 pdf-editor 文字物件不可消失（fix #6）

- [ ] 上傳含中文文字的 PDF → 用 pick tool 選一段既有文字 → 顯示 OCR 還原的文字 IText
- [ ] 點空白處 deselect → IText 視覺保留（opacity = 1，不會 fade 變空白）
- [ ] 等 ~1s（auto-save 觸發）→ 重新點該文字位置 → 仍能編輯，不會看到「物件變空白」
- [ ] 直接用 T 工具新增文字 → 輸入 → 點空白 deselect → 文字仍可見

#### 6.9.3 角色管理全選 / 全不選 / 反選（#2）

- [ ] `/admin/roles` 編輯非 admin 角色，看到工具矩陣上方有 `全選` `全不選` `反選` 按鈕 + 計數「已選 X / Y」
- [ ] 點全選 → 所有 checkbox 勾選 + 計數更新
- [ ] 點全不選 → 全部清空 + 計數變 0 / Y
- [ ] 點反選 → 勾選與未勾選對調
- [ ] 個別點 checkbox → 計數即時更新
- [ ] 「儲存」按鈕送出 → role 套用成功

#### 6.9.4 pdf-rotate 預覽頁個別轉向（#3）

- [ ] 上傳多頁 PDF → 縮圖下方出現 `↺ ↻ 180° ⇆ ⇅ ─` 工具列
- [ ] 點 ↻ → 該頁綠框 + 徽章 `★ ↻ 90°`（綠色背景表示個別覆寫）
- [ ] 再點同一個 ↻ → 取消覆寫，回到全頁設定
- [ ] 點 ─ → 此頁明確不轉，即使全頁設定有套用也不轉
- [ ] 提交 → 結果 PDF 該頁照個別覆寫設定轉
- [ ] 公開 API 也接受 `per_page` JSON：`curl -F per_page='{"3":"rotate-180"}' .../submit`

#### 6.9.5 每頁右上「回首頁」按鈕（#4）

- [ ] 任何工具頁 / admin 頁右上角有圓角「首頁」按鈕（含 home 圖示 + 「首頁」字）
- [ ] 點按鈕跳到 `/`
- [ ] 在 `/` 本身按鈕隱藏（不會出現「回首頁」沒反應）
- [ ] 手機 viewport（< 600 px）只顯示圖示，不顯示文字
- [ ] login 頁不顯示（沒 sidebar 的頁）

#### 6.9.6 企業 Logo / 識別（#1）

- [ ] `/admin/branding` 頁面開啟正常，顯示「目前 Logo」（預設或自訂）
- [ ] 上傳 PNG / JPG / WEBP → 預覽即時顯示 → 上傳成功 → 重整看到自訂 logo 出現在 sidebar / favicon / 首頁 hero / login 頁
- [ ] 上傳 > 5 MB → 拒絕並顯示錯誤
- [ ] 上傳非圖片（如 .pdf 改名 .png）→ 拒絕（PIL verify 抓到）
- [ ] 點「還原預設」→ 確認 → logo 變回內建
- [ ] `GET /branding/logo` 公開 endpoint：未設自訂回 404，有設回 PNG
- [ ] `/branding/` 路徑 prefix 在 `_PUBLIC_PREFIXES`（login 頁能讀到自訂 logo）

#### 6.9.7 用印與簽名臨時資產（#7）

- [ ] `/tools/pdf-stamp` 在資產區下方有「臨時上傳一張（僅本次）」按鈕
- [ ] 上傳圖檔 → 出現綠框臨時資產項目，radio 自動選中
- [ ] PDF 預覽顯示臨時 logo 位置（編輯模式）→ 拖曳 / 縮放正常
- [ ] 提交蓋章 → 成功產出 PDF，圖位置正確
- [ ] 重整頁面後 sessionStorage 還在 → 臨時資產仍可見
- [ ] 開新分頁 → 臨時資產不存在（sessionStorage per tab）
- [ ] 「移除」按鈕 → 清掉
- [ ] 蓋章送出後在 admin 稽核記錄看到 `event_type=temp_asset_used` + 檔名 + sha256 前 16 字
- [ ] data/ 內**不會**有臨時 logo 殘留（temp_dir 內 `stamp_temp_*.png` 由 2hr 排程清掉）

#### 6.9.8 逐句翻譯工具（#5）

- [ ] LLM 未啟用：`/tools/translate-doc` 顯示黃底警告「LLM 服務尚未啟用」+ 連結到 `/admin/llm-settings`，按鈕 disabled
- [ ] LLM 啟用後：貼一段中英混合文字 → 點「開始翻譯」 → 並排對照表出現
- [ ] 每句左原文 / 右譯文，譯文預設繁中
- [ ] 點某句 ↻ → 該句重新翻譯（不影響其他）
- [ ] 上傳 PDF → 解析出文字並切句 → 翻譯
- [ ] 上傳 DOCX → 同上
- [ ] 上傳 .txt → 同上
- [ ] 「複製譯文」/「複製對照」按鈕 → 剪貼簿正確
- [ ] 公開 API：`curl -X POST .../api/translate-doc -d '{"text":"hello","target_lang":"zh-TW"}'` → 回 JSON
- [ ] sidebar 搜尋「翻譯」/ `translate` 都找得到
- [ ] 既有客戶升級後：原本有 `text-diff` 權限的角色自動拿到 `translate-doc`（v5 migration）

#### 6.9.9 doc-deident 精準度（#8）

- [ ] 「生日：民國 70 年 3 月 21 日」→ 偵測到 `dob`
- [ ] 「出生日期： 1985-03-21」→ 偵測到 `dob`
- [ ] 「+886-912-345-678」→ 偵測到 `mobile`（含 +886）
- [ ] 「(電話) #123」→ 偵測到 `landline` 含分機
- [ ] 「(地址) 100 號 5 樓之 1」→ 偵測到 `addr` 含「樓之」
- [ ] 「Passport: 123456789」→ 偵測到 `passport`
- [ ] 「駕照號碼：F123456789」→ 偵測到 `driver_license`
- [ ] 純 9 位數字（無 Passport label）→ **不**誤認為 passport（false positive 修正）
- [ ] 「FROM 123」→ **不**誤認為 plate（前後標點要求）

#### 6.9.10 設定備份 / 匯入（#64）

- [ ] `/admin/settings-export` 顯示目前 data/ 內檔案 / 目錄列表 + 大小
- [ ] 點「下載備份壓縮檔」 → 下載 `jtdt-settings-YYYYMMDD-HHMMSS-vX.Y.Z.zip`
- [ ] 解壓 zip 看到 `manifest.json` + `data/` 結構正確
- [ ] 上傳同份 zip 「開始匯入」 → 確認對話框 → 匯入成功
- [ ] data/ 內出現 `*.bak.YYYYMMDD_HHMMSS` 備份檔
- [ ] 上傳壞檔（非 zip / 缺 manifest）→ 拒絕並顯示錯誤
- [ ] 上傳含 path traversal 的 zip（手工構造 `../etc/passwd`）→ 拒絕「unsafe path」
- [ ] 勾選「也覆寫歷史記錄目錄」 + 匯入 → fill_history 等也覆蓋
- [ ] 公開 API：`GET /admin/api/settings-export/summary` 回 JSON

### 6.11 v1.4.x 後續發現的問題（每次發版必過）

#### 6.11.1 Windows install.ps1 NSSM bundled-first（GitHub issue #1，v1.4.2 修）

- [ ] `github/packaging/windows/nssm.exe` 存在且 ~330 KB
- [ ] Fresh Win11 從 GitHub 跑 install.ps1（拔網路或防火牆鎖 nssm.cc 下）→ 仍能裝起來（用 bundled）
- [ ] install.ps1 內 `Install-Nssm` 必須在 `Fetch-Code` 之後（順序顛倒會找不到 bundled）
- [ ] Network fallback 用 `Invoke-WebRequest -TimeoutSec 20`，**禁止** `Net.WebClient.DownloadFile`（沒 timeout 卡好幾分鐘）

#### 6.11.2 客戶升級不准弄壞既有設定（v1.4.2 LDAP 慘案）

- [ ] `_run_auth_helper` 跑完後固定 chown 整個 data dir 回 service user（防止 sudo 寫的檔變 root:root mode 600 service 讀不到）
- [ ] `svc_update` 結尾跑一次 `_chown_data_files_back()`（self-heal 過去被汙染的客戶機）
- [ ] 模擬：在客戶機把 `data/auth_settings.json` chown 成 `root:root mode 600` → 跑 `sudo jtdt update` → 升級完後該檔回 `jtdt:jtdt` → 服務讀得到 → web UI LDAP 設定還在
- [ ] 模擬：客戶設好 LDAP → `sudo jtdt auth disable` → 檢查 `auth_settings.json` 仍 `jtdt:jtdt`、ldap 區段 fields 完整保留
- [ ] 既有 `auth.sqlite` 內 users 在升級後一個都沒少（migrations 全 INSERT OR IGNORE，不 UPDATE / DELETE）
- [ ] 既有 `role_perms` / `subject_perms` 行數升級前後一致（_m4 / _m5 只新增 image-to-pdf / translate-doc 行）

#### 6.11.3 setup-admin 偵測既有 user → 提供「沿用既有 admin 恢復」（v1.4.2）

- [ ] 既有 `auth.sqlite` 內有 user + `auth_settings.json backend=off` → 進 `/setup-admin` 看到藍色 reuse panel + 既有帳號清單
- [ ] 點「恢復本機認證」→ backend 變 local，不建新 user，session 全清，導去 /login
- [ ] /login 顯示提示訊息「已恢復本機認證，沿用 N 個既有帳號」
- [ ] 用既有 admin 帳號 + 密碼登入成功
- [ ] 沒有既有 user → setup-admin 顯示一般 form（建新 admin）
- [ ] reuse 流程結束 `auth_settings.json` ldap 區段未被清掉

#### 6.11.4 友善 403 / 401 / 404 錯誤頁（v1.4.2）

- [ ] 非 admin 在瀏覽器訪問 `/admin/llm-settings` → 友善 403 HTML 頁面（不是 raw JSON）
- [ ] 未登入訪問 `/admin/*` → 友善 401 HTML + 「去登入」按鈕
- [ ] 純 API client (Accept: application/json) 仍然回 JSON，不被改成 HTML

#### 6.11.5 跨用戶 upload_id 資安隔離（v1.4.83 修，重大）

啟用認證後，原本任一已登入 user 拿到別人的 upload_id 即可下載對方的 PDF / preview PNG。新增 `app/core/upload_owner.py` 寫入 sidecar JSON 紀錄 upload_id 屬於哪個 user_id，下載端點用 ACL 比對。

- [ ] **跨 user 拒絕**：兩個 user A、B 各自登入後，A 上傳一份 PDF 到任一工具（例如 pdf-fill /preview）→ 從瀏覽器 DevTools 抄下 `upload_id` → 在 B 的 session 用 curl 帶 cookie 打 `/tools/pdf-fill/download/{A 的 upload_id}` → **必須回 403** access denied
- [ ] **同 user 自己**：A 用自己的 cookie 抓自己的 upload_id → 200 OK 拿到檔案
- [ ] **Admin override**：把 user 設為 admin role → 抓他人 upload_id → 200 OK（為了客服 / 故障排除留的後門）
- [ ] **Anonymous 無法存取**：未登入 curl `/tools/*/download/<任何 id>` → 401 redirect to /login
- [ ] **Auth OFF（單機模式）**：關掉認證 → 任何 upload_id 都能拿（功能維持原樣）
- [ ] **Path traversal 阻擋**：`curl '/tools/pdf-fill/preview/../../etc/passwd'` → 400 invalid filename
- [ ] **UUID 格式檢查**：`curl '/tools/pdf-fill/download/INVALID'` → 400 invalid upload_id
- [ ] **Sidecar 清理**：上傳後 3 小時（temp_hours TTL 預設 2hr）→ `data/temp/.owners/<id>.json` 也應該被 retention sweeper 清掉，不只 PDF
- [ ] **Owner record missing**：手動刪掉 `.owners/<id>.json`（模擬升級前 legacy 檔）→ 該 upload_id 對非 admin 一律 403、對 admin 仍可存取
- [ ] **新單元測試 34 項全數綠燈**：`uv run pytest tests/test_safe_paths_and_owner.py -v`

#### 6.11.6 安全 headers middleware（v1.4.83 加）

- [ ] `curl -I http://localhost:8765/` → 回應含 `X-Content-Type-Options: nosniff` / `X-Frame-Options: SAMEORIGIN` / `Referrer-Policy: strict-origin-when-cross-origin` / `Permissions-Policy: ...interest-cohort=()`
- [ ] HTTPS 連線（reverse proxy 後）→ 額外含 `Strict-Transport-Security: max-age=15552000; includeSubDomains`
- [ ] 純 HTTP 連線**不**發 HSTS（不鎖內網 plain-HTTP 安裝）
- [ ] iframe embed 從 cross-origin 載入頁面 → 被 X-Frame-Options 擋掉

#### 6.11.7 Windows Tesseract 不需手動加 PATH（v1.4.88 修，GitHub issue #4）

客戶 Windows 機反映：用 install.ps1 裝完 Tesseract OCR，pdf-editor 仍顯示「OCR 不可用」需手動加 `C:\Program Files\Tesseract-OCR` 進系統 PATH 才行。Winget 安裝 UB-Mannheim 套件有時不會自動加 PATH，使用者也未必有 admin。修法：①程式碼端 `app/core/sys_deps.py:configure_pytesseract()` 探測標準路徑後設 `pytesseract.pytesseract.tesseract_cmd`，不需 PATH；②`install.ps1` 加 `Add-TesseractToPath` 主動補進 system PATH（雙保險）；③`jtdt update` 結尾的 sys-deps summary 也用相同邏輯，不會誤報缺。

- [ ] **故意拔 PATH**：Win11 上把 Tesseract 從 system PATH 移掉但保留 `C:\Program Files\Tesseract-OCR\tesseract.exe`，重啟 service → pdf-editor 仍能跑 OCR（紅框點下去能還原文字）
- [ ] **`jtdt sys-deps` 不誤報**：上述狀態下跑 `jtdt sys-deps` → tesseract 顯示 OK 不是 missing
- [ ] **install.ps1 主動補 PATH**：Fresh Win11 跑 install.ps1 → 觀察 log 應有 `Adding Tesseract to system PATH: C:\Program Files\Tesseract-OCR`；裝完後新開 PowerShell `tesseract --version` 應該抓得到
- [ ] **重複跑 install.ps1 不重複加 PATH**：再跑一次 install.ps1 → 不應重複 append PATH（檢查 system Path 不應有兩個 `Tesseract-OCR`）
- [ ] **macOS / Linux 行為不變**：標準位置 `/usr/local/bin/tesseract` 或 brew 路徑能被探到；`shutil.which` 仍是首選

### 6.13 v1.5.0 — 認證 / 角色 / 稽核員 / 2FA / 鎖定機制（每次發版必過）

#### 6.13.1 全新安裝啟用認證 → jtdt-auditor 自動建（v1.5.0）

- [ ] `jtdt auth set-local` + service restart 後 `auth.sqlite` 出現 username=jtdt-auditor 的本機帳號
- [ ] 該帳號 `password_hash IS NULL`、`totp_required=1`、`is_audit_seed=1`
- [ ] subject_roles 有 `(user, <uid>, auditor)` 對應

#### 6.13.2 升級保留資料（v5 → v7 schema）

- [ ] migration v6（totp_*）+ v7（is_audit_seed）對既有 user 行不影響
- [ ] 既有 default-user 角色的 role_perms 不被 wipe

#### 6.13.3 jtdt-auditor 第一次登入流程

- [ ] NULL pw 狀態 login → form 「帳號或密碼錯誤」（拒絕，不會跳 /2fa-verify）
- [ ] `sudo jtdt reset-password jtdt-auditor` 設密碼 → login 302 to `/2fa-verify`
- [ ] /2fa-verify GET 在 forced_setup 模式顯示 QR + 把 secret 寫進 DB
- [ ] 提交 6 碼正確 → 302 + jtdt_session cookie + totp_enabled=1
- [ ] 提交 6 碼錯誤 → 200 重新顯示

#### 6.13.4 admin 重設使用者 2FA（v1.5.0 新增 #6 BUG 修法）

- [ ] /admin/users 頁每個 user row 多了「重設 2FA」按鈕
- [ ] 點下去 → POST /admin/users/{uid}/reset-totp → 200 ok
- [ ] DB 內該 user totp_secret=NULL, totp_enabled=0；sessions 全清
- [ ] 該 user 下次登入 → 看到 QR（forced setup 重新走一次）
- [ ] 內建 jtdt-admin / jtdt-auditor 也有「重設 2FA」按鈕（不可刪但可重設）

#### 6.13.5 帳號鎖定 / 解鎖（v1.5.0 新增）

- [ ] 連錯密碼 5 次 → form 出現「嘗試次數過多，請於 N 分鐘後再試」
- [ ] /admin/users 頁被鎖的 user 顯示「解鎖」按鈕（黃底）
- [ ] 點「解鎖」→ POST /admin/users/{uid}/unlock → DB lockouts 該 user key 清掉
- [ ] /admin/auth-settings 頁有「清除所有鎖定」按鈕 → 一鍵清光（含 IP-based）

#### 6.13.6 職責分離 / 稽核員權限矩陣

- [ ] **admin 不可看**：/admin/uploads /admin/history/fill /stamp /watermark → 一律 403（v1.5.0 強化）
- [ ] admin 仍可看：/admin/audit /admin/system-status + 其他所有設定區
- [ ] admin sidebar 自動隱藏 uploads + 3 個 history 條目（_nav_settings_visible filter）
- [ ] auditor → /admin/audit /admin/system-status /admin/uploads /admin/history/* 都 200
- [ ] auditor → /admin/users /admin/roles /admin/auth-settings 一律 403
- [ ] auditor → /tools/任何工具/ 一律 403
- [ ] 每次 auditor view 寫一筆 `auditor_view` audit event（admin 看得到，auditor 沒刪除按鈕）
- [ ] auditor 自己 POST /me/2fa/disable → 403「您的角色強制使用 2FA」
- [ ] /admin/roles 頁面稽核員 row 不顯示工具勾選方塊（admin role 也是）
- [ ] admin POST tools=[…] 給 auditor role → 寫不進 role_perms（silently no-op）
- [ ] admin 試刪 jtdt-auditor → 400「不能刪除內建稽核員帳號」
- [ ] enforce_auditor_isolation 啟動時跑：auditor user 不可有其他 role / 直接 tool perm，totp_required 必為 1

#### 6.13.7 LDAP 共存

- [ ] LDAP backend ON 時 jtdt-admin / jtdt-auditor 仍可用 realm=local 登入
- [ ] LDAP user 認證未受 v1.5.0 改動影響
- [ ] `jtdt auth show` 正確顯示 LDAP server URI / search base / bind DN（不是 (unset)）

#### 6.13.8 jtdt update 不弄壞 auth_settings.json

- [ ] update 流程開始前 snapshot auth_settings.json bytes
- [ ] update 結束前若 file 內容變了 → 自動 restore + 警告
- [ ] 升級後 backend / LDAP server URI / TLS 設定全保留
- [ ] 重大原則：客戶升級版本，原有設定必需留存

### 6.12 機密 / 內網檢查（push 前必跑）

```bash
grep -rnE "192\.168\.|10\.[0-9]+\.[0-9]+\.[0-9]+|親測|OSSII 內部" \
  github/ --include='*.md' --include='*.html' --include='*.py' \
  | grep -vE "10\.0\.0\.|192\.168\.1\.10[^0-9]"
```

- [ ] 無真實內網 IP（test fixture 用 `10.0.0.x` / `192.168.1.10` placeholder OK）
- [ ] 無「親測」「內部」之類用語


### 6.14 v1.14.6 — 設定備份補齊 + 工作佇列 / 持久化 / 併行度（每次發版必過）

自動化：`tests/test_settings_export.py`、`tests/test_job_queue.py`、
`tests/test_job_api_acl.py`。以下為需人工確認或跨行程重啟才驗得到的項目。

#### 6.14.1 設定備份 / 匯入涵蓋度

- [ ] `python tools/check_settings_export_coverage.py` 回 0（新設定檔都已納管）
- [ ] 管理區「設定備份 / 匯入」看得到新分類：SSO 單一登入、目錄同步 / 過濾、
      記錄轉送、檔案保留 / 清理、排程備份設定、併行度設定、OCR 設定、
      掃描工具欄位偏好、掃描暫存資料、使用者工作區、送件檢查（自家實體）
- [ ] 「認證設定」分類的說明**不再**宣稱含 OIDC / SAML（那項獨立成 SSO 分類）
- [ ] **SSO 跨機還原**（最重要，是這批的核心 bug）：
      A 機設好 OIDC（含 client secret）→ 匯出 → 在 **B 機**（不同
      `.session_secret`）匯入 → B 機的 SSO 登入**要能成功**。
      舊行為是複製密文過去，B 機解不開 → 設定看起來都在但登入一直失敗。
- [ ] 備份 zip 內**沒有** `.session_secret`（有的話等於把偽造登入的能力送出去）
- [ ] 使用者工作區 / 掃描暫存資料 / 各類歷史 → 預設**不勾選**（量大）

#### 6.14.2 工作持久化（重啟後不遺失）

- [ ] 送出一份大檔轉換 → 等完成 → **重啟服務** → 「我的作業」仍列得出來，
      且「下載」按得到、檔案正確
- [ ] 轉換**進行中**時砍掉服務 → 重啟後該筆顯示「已中斷」+ 說明需重新送出
      （不可繼續顯示「進行中」讓使用者等一個永遠不會完成的工作）
- [ ] 結果檔被保留期限清掉後，該筆顯示「結果已逾期清除」而**不是**一個按了 404 的下載鈕

#### 6.14.3 佇列 / 併行度 / OOM 防線

- [ ] 管理區「背景作業與併行度」：同時送出超過上限的工作 → 多的顯示「排隊中」，
      不是全部一起跑
- [ ] 調高「最大同時工作數」→ 排隊中的**立刻**被派出去（不必等下一次送出）
- [ ] 「暫停派送」→ 新工作停在排隊中；**已經在跑的照樣跑完**（UI 有說明原因）
- [ ] 取消排隊中的工作 → 直接移出佇列；取消執行中的 → 下一個 checkpoint 停止
- [ ] 併行度填一個誇張數字（9999）→ 被夾到硬上限，不可真的生效
- [ ] macOS：「Office 轉檔同時數」欄位**停用**且顯示原因（Aqua bootstrap 競爭）；
      Linux / Windows 可調
- [ ] 記憶體不足時新工作**排隊**而不是硬開（`held_for_ram` 會亮）；
      且沒有任何工作在跑時仍會派一個出去（不可整個服務靜止）

#### 6.14.6 逐句翻譯的背景作業（v1.14.6）

- [ ] 送出後**關掉分頁**，隔一段時間回到「我的作業」→ 那筆作業還在跑 / 已完成
- [ ] 從「我的作業」點「看進度 / 開啟」→ 回到逐句翻譯頁，看得到目前進度與已完成的句子
- [ ] 網址帶 `?job=<id>` 直接開 → 一樣接得回來（重新整理也是）
- [ ] **一送出就看得到全部原文**（右側空白），不是等做完才出現
- [ ] 已花時間顯示的是**伺服器算的**（從別的分頁回來不會變成「已花 0 秒」）
- [ ] 中途按「停止翻譯」→ 狀態變已停止，已完成的句子保留
- [ ] 另一個帳號拿到 job id → 進度查詢回 404（譯文就是文件內容）
- [ ] 服務重新啟動 → 該作業顯示「已中斷，請重新送出」，不是永遠轉圈
- [ ] **外部服務名額**：翻譯進行中，另一個需要 LLM 的工具不會卡死
      （曾經因為名額被重複取得而自我鎖死，症狀是作業永遠停在「準備中」）

#### 6.14.7 帳號信箱與通知收件人（v1.14.6）

- [ ] AD / LDAP 使用者登入後，管理區「使用者管理」看得到從目錄帶入的信箱
- [ ] **不必等登入**：改完信箱屬性後按「立即同步」→ 尚未登入過的鏡射使用者也有信箱
      （UCS 用 `mailPrimaryAddress`，AD 用 `mail`）
- [ ] 目錄那邊沒填信箱的帳號 → 不可以把管理員手動補的值清成空白
- [ ] SSO（OIDC / SAML）登入 → 信箱由 IdP 帶入
- [ ] 通知設定的 Email 那一列**沒有輸入框**，只顯示「會寄到 ○○○」與去哪改
- [ ] 直接送 `{"email_to": "..."}` 給 `/api/my/notify` → 不會生效（擋在伺服器端）
- [ ] 帳號沒有信箱 → Email 管道不啟用（不是錯誤，也不可以噴例外）
- [ ] **從未設定過通知偏好的人**：管理員開好 Email + 帳號有信箱 → 跑一個超過門檻的
      作業就收得到（不必自己去勾任何東西）
- [ ] 使用者把管道全部取消勾選並儲存 → 之後不再收到（不可以又被自動打開）
- [ ] 不會收到任何通知時，通知設定區有明說「目前不會收到任何通知」
- [ ] 通知信是 HTML 版型（標題列 / 狀態徽章 / 欄位表 / 按鈕），且純文字版也在
- [ ] 信裡看得到**站台 logo 與工具圖示**，且**不需要按「顯示圖片」**（內嵌附件，不是外部網址）
- [ ] 管理員換過 logo → 之後寄出的信用新的那張
- [ ] 圖片產不出來時信照樣寄得出去（只是沒有圖）
- [ ] 「站台網址」沒填 → 信裡不放按鈕；填了非 http(s) 的值 → 不被接受
- [ ] 認證設定的「信箱屬性」改成別的名稱後存檔 → 重新整理仍在（不可無聲消失）
- [ ] 側欄「我的帳號」看得到信箱；沒設定時顯示「尚未設定 — 通知會寄不出去」
- [ ] **本機帳號**：卡片上按「修改」→ 存檔 → 重開卡片仍是新值
- [ ] **AD / LDAP / SSO 帳號**：卡片上**沒有**修改鈕，並說明由來源端管理
- [ ] 直接打 `POST /me/email`（目錄帳號）→ 403（擋在伺服器端，不是只藏 UI）
- [ ] 未登入打 `POST /me/email` → 被擋（不可跟著轉址誤判成 200）

#### 6.14.8 工作區的 Office 檔縮圖（v1.14.6）

- [ ] 存一個 .docx / .pptx / .odt 進工作區 → 稍等一下卡片出現第一頁縮圖
      （第一次開頁面可能還是空白，幾秒後自動補上，不必手動重新整理）
- [ ] 同一個檔第二次開頁面 → 立刻有縮圖（走快取，不會再轉一次）
- [ ] 超過 80 MB 的檔 → 不做縮圖，畫面不破圖
- [ ] 毀損的檔 → 失敗一次之後不再重試（不可以每次開頁面都跑一次 Office 引擎）
- [ ] 一頁十幾個 Office 檔 → 頁面**立刻**顯示，不可以卡住等轉檔

#### 6.14.9 「可以關掉這一頁」的標示（v1.14.6）

- [ ] 任一個有背景作業的工具送出後 → 進度列出現這行提示
- [ ] 作業完成 / 失敗 / 取消 → 提示收起
- [ ] 提示只做在共用進度列，個別工具沒有各自再寫一份（文案不會分歧）

#### 6.14.10 文件去識別化：表格裡的欄位（issue #43, v1.14.7）

- [ ] Word 表格「出生日期 | 1998-12-28」要被偵測到，遮蔽框落在**值**那一格
- [ ] 段落四種寫法都要抓到：`1998/12/18`、`1998-12-19`、`1998.12.20`（點分隔）、
      `民國87年12月21日`
- [ ] `DOB: 12/18/1998` 要整個吃掉，不可只抓 `12/18/19`（遮蔽後留著 `98`）
- [ ] 沒有標籤的裸日期不可被當成出生日期
- [ ] 跨格配對不可產生重複，也不可讓身分證 / Email 這類不需標籤的式子跨格湊配
- [ ] 同樣驗一次銀行帳號 / 駕照號碼放在表格裡（同一條程式路徑）

#### 6.14.11 我的工作區：大檔縮圖（v1.14.7）

- [ ] 30 MB 級的 .pptx 存進工作區後，兩分鐘內縮圖會自己出現（不必手動重新整理）
- [ ] 空白佔位圖的回應帶 `Cache-Control: no-store`
- [ ] 縮圖產好之後重新整理頁面，不會因為瀏覽器快取而仍顯示空白

#### 6.14.12 新工具：頁面加框（pdf-border, v1.14.11）

- [ ] 上傳 PDF → 顯示頁數、每頁預覽都出現框線
- [ ] 上傳 .pptx / .odp → 自動轉成 PDF 後加框，狀態列顯示「已由文書檔轉成 PDF」
- [ ] 從工作區載入一份簡報，流程與直接上傳一致
- [ ] 兩種定位：自頁緣內縮（每頁位置一致）/ 貼齊內容（框跟著內容走、不溢出頁面）
- [ ] 線條：粗細 / 顏色 / 實線・虛線・點線 / 圓角 / 不透明度，改動後預覽自動更新
- [ ] 內外雙框、外側陰影各自開關，子選項跟著顯示 / 隱藏
- [ ] 首頁不加框 → 第 1 頁預覽標「不加框」且變淡
- [ ] 指定頁面 `1,3,5-8` → 只有這幾頁有框；**打錯字（例如「第一頁」）要變成全部加框，不可以一頁都不畫**
- [ ] 四個快速套用（投影片外框 / 獎狀雙框 / 細灰線 / 圓角卡片）都會同步所有欄位並重畫預覽
- [ ] 點縮圖開放大檢視，可用 ‹ › 與方向鍵翻頁、ESC 關閉
- [ ] 送出後走背景作業（進度列 + 可關頁面），完成可下載並「存至工作區」
- [ ] 旋轉頁（/Rotate 90）的框線要落在可見頁面內，不可跑出頁外或只畫一半
- [ ] API `POST /tools/pdf-border/api/pdf-border` 依 API.md 範例呼叫可得加框 PDF
- [ ] 線寬 / 邊距給極端值（例如 `width_pt=500`）時伺服器要夾住，不可把整頁塗滿

#### 6.14.13 AD / LDAP 帳號管理一輪（v1.14.14）

**使用者清單**
- [ ] 點來源篩選（local / ldap / ad）清單要正確過濾，**不可以整份消失**
- [ ] 「最後登入」排序要真的按時間，從未登入的排最後
- [ ] 搜尋 / 來源 / 狀態篩選是**伺服器端**：篩出來的總數要是全庫的數字，不是當前頁
- [ ] 超過一頁時有分頁控制，換頁後篩選條件保留

**批次操作**
- [ ] 列選 + 全選（部分選取時全選框呈現 indeterminate）
- [ ] 批次啟用 / 停用 / 加上角色 / 移除角色都會生效並寫稽核
- [ ] **停用自己 → 被擋**；**停用內建管理員 → 被擋**
- [ ] **全選所有管理員後停用 → 被擋**（否則沒有人進得了管理區）
- [ ] 被跳過的帳號要顯示原因，不可以靜靜少做

**目錄已無（離職偵測）**
- [ ] 完整同步後，AD 端刪掉 / 移出範圍的帳號要出現在「目錄已無」
- [ ] 本機帳號與 SSO 帳號**永遠不可以**被標記
- [ ] 還沒做過完整同步時，這個檢視要顯示說明而不是 0 筆
- [ ] 帶名稱過濾的同步**不可以**更新判定基準
- [ ] 該帳號重新登入成功後，標記要消失

**巢狀群組**
- [ ] 權限指派給上層群組 → 子群組成員要拿得到
- [ ] 目錄端設出環狀關係（A→B→A）時，權限查詢不可以卡住

**有效權限**
- [ ] `GET /admin/users/{id}/effective` 列出的工具要與該使用者實際看得到的一致
- [ ] 每個工具都標得出來源；巢狀繼承要標明是繼承來的
- [ ] 稽核員一律 0 個工具（即使同時有 admin 角色）

**故障可觀測性**
- [ ] 關掉 LDAP 伺服器後登入：畫面只顯示通用訊息，**稽核有 `ldap_unavailable`**
- [ ] AD 帳號鎖定後登入：稽核的 `ad_reason` 要顯示「帳號已被鎖定」
- [ ] 同步失敗要記下是哪個群組 / 什麼原因，並保留歷史
- [ ] 同步失敗會發出通知（需先設定通知管道）

#### 6.14.3b 網頁回應與轉檔隔離（「網頁回應永遠優先」）

原始症狀：正式機轉檔期間整站空轉，但 CPU / 記憶體看起來都有餘裕。
**這一節每次發版都要在真的多核機器上跑**，本機開發機（核心數多、沒有其他負載）
重現不出來 —— 2026-07-30 就是在 8 核開發機上測不出、在 6 核正式機上才發生。

- [ ] 送出 2–4 份大型轉檔，同時每 0.5 秒打一次 `/healthz`：
      **不可有任何一次超過 1 秒**（修正前最久 226 秒）
- [ ] 轉檔進行中點側欄任何一頁（尤其「系統狀態」）→ 立即切換，不空轉
- [ ] `ps -o pid,ni` 看 soffice.bin：nice 應為 19（作業執行緒 10 + 子行程 10）
- [ ] `taskset -p <soffice pid>` / `os.sched_getaffinity`：核心數應等於設定值，
      且**至少留一顆**不給轉檔（預設「自動」）
- [ ] 「轉檔 CPU 上限」改 25% / 50% / 100% → 下一個轉檔的核心遮罩跟著變
      （改設定不必重啟服務）
- [ ] 選 100%（不限制）→ 不設遮罩；此時允許網頁變慢，屬管理員明示的選擇
- [ ] macOS：欄位停用並說明「沒有提供限制核心的介面」，但轉檔仍降優先權
- [ ] Windows：核心限制有效（psutil），執行緒優先權不適用 →
      soffice 由 `BELOW_NORMAL_PRIORITY_CLASS` 處理
- [ ] 單核機器（或 cpuset 只有 1 顆）→ 不可算出 0 顆核心而讓轉檔跑不動
- [ ] 已被 cgroup cpuset 限制過的容器 → 只在既有遮罩內挑核心，不可挑到遮罩外
- [ ] 事件迴圈延遲監看：人為卡住主執行緒 > 1 秒 → 記錄出現警告並附當時作業數
- [ ] 單一請求超過 3 秒 → 記錄留下慢請求警告（含路徑與耗時）

#### 6.14.3c 外部服務（LLM / 遠端 GPU OCR）同時呼叫上限

- [ ] 預設為 1：同時送出多個需要 LLM / 遠端 OCR 的作業 →
      對外請求**一次只有一個**，其餘在本機等
- [ ] 上限調高後立即生效（不必重啟）
- [ ] 外部服務逾時 / 斷線 → 名額要**確實釋放**（不可卡死後續所有作業）
- [ ] 這個上限與「最大同時作業數」互不影響（本機估算擋不到遠端負載）

#### 6.14.4 權限邊界（水平越權）

- [ ] 認證開啟：A 使用者的「我的作業」**看不到** B 的工作
- [ ] 認證開啟：A 不可取消 B 的工作（回 404，不確認其存在）
- [ ] 認證開啟：未登入呼叫 `/api/jobs` → 401
- [ ] 認證關閉（且僅此時）：以來源電腦區分，頁面上有說明同一 NAT 出口會混在一起
- [ ] 一般使用者存取 `/admin/jobs` 與其 API → 403

#### 6.14.5 規模（8000 人情境）

- [ ] 管理區「檔案保留 / 清理」有「作業紀錄（我的作業）」一列，預設 30 天
- [ ] 保留期到期後舊紀錄被清掉，**但執行中 / 排隊中的不論多舊都不刪**
- [ ] 28 萬筆時「我的作業」查詢仍在數 ms（實測 1.2 ms / 74 MB）

#### 6.14.6 資料庫毀損防護（v1.14.6）

自動化：`tests/test_db_health.py`（24 項）。以下為需人工或離線環境確認的項目。

- [ ] `jtdt db-check` 在**服務停止**時仍可執行（資料庫壞掉時網頁本來就上不去）
- [ ] `jtdt db-backup` → `jtdt db-backups` 看得到剛建立的備份
- [ ] 人為打壞 `auth.sqlite`（測試機才做）→ `jtdt db-check` 回非 0 並列出影響與復原指令
- [ ] `jtdt db-restore auth.sqlite` → 帳號資料完整回來；毀損的原檔另存為 `.corrupt.<時間>`
- [ ] 毀損狀態下執行備份 → **略過**且既有備份數不變（不可用壞檔覆蓋好備份）
- [ ] 拿一份被打壞的備份去還原 → 被擋下，且正式檔沒有被覆蓋
- [ ] 服務啟動時若資料庫毀損 → 記錄有明確訊息、稽核有 `db_corruption` 事件、
      **服務仍然起得來**（單一資料庫壞掉不該讓整個服務停擺）
- [ ] 管理區「系統狀態 → 資料庫健康狀態」顯示正確，「立即備份」可用
- [ ] CLI 輸出全為英文 ASCII（純文字終端 / 精簡容器 / Windows 主控台皆可讀）

#### 6.14.7 升級路徑（既有客戶）

自動化：`tests/test_upgrade_v1_14_6.py`（10 項）。原則是**客戶升級版本，原有
設定必需留存**，且不需要客戶手動做任何事。

- [ ] **沒有新的第三方相依** → `install.sh` / `setup-python.cmd` / `cli.py`
      三處的 import 煙霧測試都不必改（有新增相依時要走「五處 SOP」）
- [ ] 舊 `retention.json`（缺 `job_records_days`）→ 自動補預設，客戶調過的
      其他天數**不被重設**
- [ ] 舊資料目錄沒有 `jobs.sqlite` / `concurrency.json` / `db_backups/`
      → 啟動或首次使用時自動建立
- [ ] 併行度預設維持**舊行為**（同時 2 個工作、Office 轉檔 1 個）——
      升級不可默默改變併行度而讓客戶機器變慢或變爆
- [ ] 「外部服務同時呼叫數」預設 1、「轉檔 CPU 上限」預設「自動（保留 1 核給網頁）」
      —— 升級後兩者都不需要管理員動手就生效
- [ ] **升級當下正在轉檔的使用者**：`jtdt update` 會重啟服務 → 該工作變成
      「已中斷」，頁面要明確顯示並提示重新送出，**不可讓進度條一直轉**
      （共用進度元件 + pdf-ocr + submission-check 三處都要處理）
- [ ] `sudo jtdt db-backup` 之後，`data/` 內新產生的檔案**不是 root 所有**
      （走 `_run_auth_helper` 會自動 chown 回服務帳號）
- [ ] 升級後管理區的「檔案保留 / 清理」多一列「工作紀錄」，且舊值都在
- [ ] 升級後側欄多出「我的作業」，管理區多出「背景作業與併行度」

#### 6.14.8 作業完成通知（v1.14.6）

自動化：`tests/test_notify.py`（25 項）。以下需真的外部服務或人工確認。

- [ ] 管理區「作業完成通知」→ 各管道「傳送測試」實際收得到；**失敗時顯示實際
      原因**（例如「Connection refused」），不是只說「失敗」
- [ ] 憑證存檔後頁面顯示遮罩；**只改別的欄位再存檔，憑證不會被洗掉**
- [ ] `data/notify_settings.json` 內**看不到明文** token / 密碼 / webhook URL
- [ ] 使用者到「我的作業」選管道 → 個人管道（Email / Telegram / LINE）沒填自己的
      位址時**不會送**；團隊頻道不需填
- [ ] 使用者選了管理員**沒啟用**的管道 → 不會送（不能繞過管理員）
- [ ] 跑超過門檻的作業完成 → 收得到通知；**短作業不通知**
- [ ] 通知內容只有工具名 / 檔名 / 狀態 / 耗時，**沒有檔案內容**
- [ ] 故意把管道設成連不通 → 作業本身仍然成功（通知失敗不可影響作業）
- [ ] 升級後預設是**關閉**的（不可無預警開始往外送訊息）
- [ ] **跨機還原**：A 機設好 → 匯出 → B 機匯入 → 通知直接可用（不必重新輸入憑證）

#### 6.14.9 站內通知 + 自動存入工作區（v1.14.6）

- [ ] 側欄帳號旁有通知按鈕；有新完成的作業時顯示紅點
- [ ] 點開顯示最近完成的作業（工具 / 檔名 / 狀態 / 多久前）；面板**不被側欄裁切**
- [ ] 「全部標示為已讀」後紅點消失；再有新作業完成又會出現
- [ ] **認證關閉時通知按鈕也要在**（單機使用者一樣需要）
- [ ] 只看得到自己的作業（認證開啟依帳號、關閉依來源電腦）
- [ ] **開著頁面等**作業完成 → **不會**自動存入工作區（人就在那裡）
- [ ] 送出後**關掉頁面**，完成後 → 自動存入工作區，清單顯示「已自動存入」
      且**不再顯示「存至工作區」按鈕**
- [ ] 工作區容量調到很小 → 顯示「工作區容量已滿，未自動存入」且下載連結仍在
- [ ] 工作區**停用**時 → 不自動存，改顯示「結果將於 N 小時後清除」
- [ ] `.pptx` / `.odp` 存得進工作區（原本會被拒收）

### 6.15 v1.14.16 — AD / LDAP 管理一輪（每次發版必過）

自動化：`tests/test_ldap_failover.py`（14 項）、`test_ad_primary_group.py`（14）、
`test_ad_account_state.py`（40）、`test_directory_cleanup.py`（31）、
`test_online_sessions.py`（29）、`test_directory_role_assign.py`（19）、
`test_effective_permissions.py`（11）、`test_directory_presence.py`（12）。
以下需要**真的 AD / LDAP 環境**或人工確認。

#### 6.15.1 多台 DC 容錯

- [ ] 伺服器欄位填兩台（逗號分隔）→ **存得下去**（`type="url"` 會讓整個表單送不出）
- [ ] 停掉第一台 → 仍然登得進去（自動換第二台）
- [ ] 第一台修好後**會被重新使用**（不是永久排除 —— `exhaust` 給的是秒數）
- [ ] 兩台都不通 → 幾秒內回「無法連線到認證伺服器」，**不是卡住幾十秒**
- [ ] 稽核記錄有 `ldap_unavailable`，畫面上**沒有**原始例外訊息

#### 6.15.2 AD 主要群組

- [ ] 把某人的 primaryGroupID 改成一個有指派角色的群組（且該群組**不在**他的
      memberOf）→ 他登入後**拿得到**那個群組的權限
- [ ] OpenLDAP 環境登入完全正常（沒有 objectSid，不可以出錯）

#### 6.15.3 帳號狀態（AD 已停用 / 密碼到期）

- [ ] AD 端停用某人 → 同步後使用者清單出現「AD 已停用」徽章與檢視
- [ ] AD 端啟用回來 → 同步後徽章**消失**（狀態要跟著回正常）
- [ ] 密碼快到期的人出現「密碼 N 天後到期」；已過期顯示「密碼已過期」
- [ ] 套了細緻密碼原則（PSO）的人日期**正確**（不是用網域 maxPwdAge 算的）
- [ ] 設了「密碼永久有效」的人**不顯示**到期
- [ ] OpenLDAP / 本機 / SSO 帳號**完全不出現**這兩種徽章

#### 6.15.4 批次停用 / 排程自動停用

- [ ] 「目錄已無」→「全部停用」：確認訊息寫出**實際會動到幾個人**
- [ ] 停用後帳號與角色指派**都還在**，重新啟用即恢復
- [ ] 停用後按鈕**消失**（沒有還啟用中的人）
- [ ] 故意讓待停用人數超過目錄帳號的 20% → **整批中止、一個都沒動**，
      訊息點出可能是服務帳號密碼過期 / 搜尋範圍被改
- [ ] 排程自動停用**預設是關閉**；升級後不可自己開始停用任何人
- [ ] 帶名稱過濾的同步**不會**觸發自動停用
- [ ] 內建管理員永遠不被停用

#### 6.15.5 在線 session

- [ ] 啟用認證 → 使用者清單顯示「N 人在線」；**單機模式不顯示**
- [ ] 同一人開三個瀏覽器 → 算 **1 人**（不是 3）
- [ ] 閒置超過 15 分鐘後從在線人數消失
- [ ] 「登入裝置」看得到瀏覽器 / 作業系統、來源位址、最後活動時間
- [ ] 個別登出 → 那一台下一個動作被導回登入頁，**其他裝置不受影響**
- [ ] 「全部登出」→ 全部被踢；稽核有 `session_revoke`
- [ ] 瀏覽器開發者工具看不到 token 或完整雜湊

#### 6.15.6 目錄瀏覽指派角色

- [ ] 選一個**從沒登入過**的目錄使用者 → 指派角色 → 使用者管理看得到他
      （**未啟用**狀態）
- [ ] 該使用者第一次登入 → 自動啟用，**先前指派的角色還在**（沒被預設角色蓋掉）
- [ ] 「所屬群組」點「角色」→ 設得了群組權限
- [ ] 同名不同 DN → 拒絕並說明衝突對象

#### 6.15.7 有效權限面板

- [ ] 編輯使用者 → 展開「有效權限」→ 列出實際能用的工具與**來源規則**
- [ ] 從上層群組繼承來的標成「巢狀繼承」
- [ ] 管理員顯示「所有工具」；稽核員顯示 0 個工具

### 6.16 v1.14.18 — 「同上」展開 + LLM 逐欄校驗保守規則（每次發版必過）

自動化：`tests/test_same_as_ref.py`（39，含端到端真的產 PDF 抽文字）、
`tests/test_llm_per_field_consensus.py`（13，用腳本化假模型跑真的兩輪流程）。

#### 6.16.1 「同上」展開

- [ ] 公司資料把「發票地址」填成 `同上` → 填出來的表單上是**實際地址**，不是「同上」
- [ ] 填成「同公司地址」「同登記地址」→ 一樣展得開
- [ ] 「電話」填成 `同上` → **保持原字面**（沒有約定俗成的對象，不可以亂猜）
- [ ] 「英文地址」填成 `同上` → **保持原字面**（中文地址不可以填進英文欄）
- [ ] 指到的欄位是空的 → 保持原字面，**不可以變成空白**
- [ ] 結果頁列出「以下的『同上』已展開成實際內容」，且看得到原本填的是什麼
- [ ] 公司名叫「同心圓…」之類「同」開頭的客戶，其他欄位的 `同上` 一樣展得開

#### 6.16.2 LLM 逐欄校驗

> LLM 校驗預設關閉；要測需先在管理區開啟並指定模型。

- [ ] 校驗跑完後結果頁顯示**兩輪**；被採納的列是綠色
- [ ] 同一個問題**不會列兩次**（去重）
- [ ] 只在其中一輪被指出的疑慮**仍然列得出來**，但不標成已採納
- [ ] 管理區把「連續幾輪」設成 1 → 只跑一輪，行為與舊版相同
- [ ] 第二輪只重問可疑欄位（看進度訊息「再確認 N/M」的 M 應**遠小於**總欄位數）

### 6.17 v1.14.19 — 中文字形與字型子集化（每次發版必過）

自動化：`tests/test_ttc_subfont.py`（27 項，含端到端產 PDF 驗字型名稱與檔案大小）。

#### 6.17.1 字形（`.ttc` 子字型）

- [ ] 表單填寫產出的 PDF，內嵌字型名稱含 **CJK TC**（不是 CJK JP）
- [ ] 頁碼、PDF 編輯器產出的中文同樣是 TC
- [ ] 目視確認：**「海」是兩點（每），不是一橫（毎）**；「過」「郎」「船」「直」
      也應為台灣寫法
- [ ] 浮水印打中文字 → 同樣是台灣字形
- [ ] 把系統 CJK 字型移走 / 改名 → **不可以整個印不出來**（退回內建字型即可）

#### 6.17.2 檔案大小

- [ ] 一張乾淨空白表單填幾個中文欄位 → 產出**不超過幾百 KB**（修正前是 13 MB）
- [ ] 產出的 PDF 文字**選得起來、複製得出來、搜尋得到**
- [ ] 100 頁文件加中文頁碼 → **秒級完成**（修正前每頁都重算一次字型子集）
- [ ] 填入罕用字（例如姓名裡的異體字）→ **不可以變成空白方框**；
      真的縮不出來時要退回完整字型（檔案變大是可接受的，缺字不行）

### 6.18 v1.14.20 — 三個新工具（每次發版必過）

自動化：`tests/test_pdf_bookmark.py`（28）、`tests/test_pdf_seam_stamp.py`（40）、
`tests/test_pdf_page_size.py`（22）。三支都用真實瀏覽器（CDP）驗過完整流程。

#### 6.18.1 書籤與目錄

- [ ] 一次選 3 個 PDF → 自動串接，**每個檔名成為第一層書籤**
- [ ] 子文件原有的書籤降一層保留，頁碼有加偏移
- [ ] 手動把第一筆改成第 2 層 → **自動修回第 1 層並說明原因**
- [ ] 頁碼填超過總頁數 → 夾到最後一頁並說明
- [ ] 勾「產生目錄頁」→ 產出多一頁；**書籤頁碼、目錄上的頁碼、目錄連結三者一致**
- [ ] 目錄頁的中文不可以是缺字方框
- [ ] 貼上「標題 + 頁碼」清單（含縮排）→ 層級正確；看不出頁碼的行會被列出來

#### 6.18.2 騎縫章

- [ ] 兩種模式各有**示意圖**（不是只有文字）
- [ ] 三種印章來源都能用：資產庫 / 上傳 / 系統產生
- [ ] 上傳帶白底的章 → 白底變透明，不會蓋住內文
- [ ] 每組 2 頁 / 3 頁 / 整份 → 組數顯示正確且**立刻更新**（不用等預覽圖）
- [ ] 「拼回去」的預覽是**完整的章**（片與片之間的縫是刻意畫的）
- [ ] 加角度之後拼回去**仍然完整**（先轉再切）
- [ ] 開亂數 → 不同組位置 / 角度不同，**同一組內完全一致**
- [ ] 產生後回報亂數種子；填回去重跑得到**一模一樣**的結果
- [ ] 印出來實測：把連續幾頁的邊緣對齊，看得出是同一個章

#### 6.18.3 頁面尺寸統一

- [ ] 上傳混合尺寸的檔 → **先列出有幾種尺寸**並提醒
- [ ] 尺寸一致的檔 → 明說「本來就一致，不一定需要處理」
- [ ] 統一成 A4「跟著原頁方向」→ A3 橫變 A4 橫、A4 直不動
- [ ] 產出的**文字仍然選得到**（不可以被轉成圖片）
- [ ] 原本就是目標尺寸的頁面**沒有被重放**（報告要說有幾頁沒動）
- [ ] 「置中不縮放」遇到比紙張大的頁面 → **警告會裁掉**
- [ ] 有 `/Rotate` 的頁面方向判斷正確

### 6.19 v1.14.21 — 三個新工具的介面回饋（每次發版必過）

> 這一節全部來自使用者實際操作後的回報。共通點是**單元測試都測不到** ——
> 要嘛是版面（要看畫面），要嘛是「模板誰呼叫誰」（要真瀏覽器）。

#### 6.19.1 設定欄位的排版（三支新工具共通）

- [ ] 每個欄位的**說明文字自己一行**，不會擠在輸入框右邊
      （`af-note` 必須是 `display:block`；`<small>` 預設是 inline）
- [ ] 同一區內的**數字框、下拉框、文字框等寬**
      （原本的寬度規則只涵蓋 `text` / `url` / `password`）
- [ ] 勾選框文字長到要折行時，**方框仍對齊第一行**不會被推到中間
- [ ] 單位（mm / % / 度）在欄位裡，不在標籤裡

#### 6.19.2 騎縫章

- [ ] 章面文字打**公司全名**（10 字以上）→ 長方章**變寬**，字級不變小
      （高度不變就是字級沒被動過）
- [ ] 同樣的長字串在圓章 / 方章 → **分行**（直行、右至左），圓章仍是正圓
- [ ] 「印章來源」的卡片與下方欄位之間**有留白**
- [ ] 「從資產庫選」是**縮圖清單**不是下拉；縮圖要真的載入（不是破圖）
- [ ] 換選另一個資產 → 印章預覽跟著更新
- [ ] 一個章跨 N 頁時，預覽把**那一組的每一頁都列出來**（不是只有一頁）
- [ ] 「2. 印章」「3. 怎麼蓋」「4. 預覽」是**三張獨立卡片**

#### 6.19.3 頁面尺寸統一

- [ ] 預覽**一次列六頁**，每張都真的載入
- [ ] 「3. 預覽」是獨立卡片，不在「2. 統一成」裡面

#### 6.19.4 書籤與目錄

- [ ] 有「3. 預覽」卡片；**沒有可看的東西時會說明原因**
      （沒書籤 / 有書籤但沒勾目錄頁，兩種訊息都算通過）
- [ ] 勾「在最前面產生目錄頁」→ 預覽真的顯示目錄頁的圖
- [ ] 產生完的結果訊息**有講書籤在閱讀器側邊欄看**
      （使用者回報過「沒看到目錄」，實際上書籤有做出來）

#### 6.19.5 作業通知的工具圖示

- [ ] 通知清單每一列**都有圖示方塊**（缺一個整排就對不齊）
- [ ] 模擬舊分頁（把某工具從 `#toolIconSprite` 移除）→ 改用**通用圖示**，
      不可以整個方塊消失
- [ ] 三處都要驗：通知下拉、`/my-jobs`、`/admin/jobs`

#### 6.19.6 守門測試（會自動跑，但發版前確認有過）

- [ ] `tests/test_api_doc_coverage.py` —— 以實際路由表反查 `API.md` 與本檔 §4
- [ ] `tests/test_api_page_builder.py` —— `api.html` 不可含 NUL；
      巢狀行內標記（粗體裡包程式碼）要完整還原
- [ ] `tests/test_template_js_syntax.py` / `tests/test_csp_nonce.py`

### 6.20 v1.14.22 — 預覽的載入狀態與頁數（每次發版必過）

- [ ] 縮圖**載入中顯示轉圈**（`.jt-thumb.is-loading`），不是破圖或空白
      —— 每張都是向伺服器要的，往返要時間
- [ ] 縮圖**算不出來時顯示紅字「算不出來」**（`.jt-thumb.is-error`），不留空白
- [ ] 騎縫章 / 頁面尺寸統一的預覽**預設 20 頁**（文件不足 20 頁就全部）
- [ ] 20 張是**有限併行**（4 條），不是逐一等
- [ ] **騎縫章的預覽以「組」為單位包起來**，同一組的頁面**永遠在同一行**
      （驗法：每個 `.sm-group` 內所有 `.sm-page` 的 `getBoundingClientRect().top` 相同）
      —— 這個工具要看的就是相鄰兩頁的接縫，被換行拆開等於預覽沒有用

### 6.21 v1.14.22 — 中文寫進 PDF 必須看得見（每次發版必過）

> v1.14.19 ~ v1.14.21 的正式機故障：字型子集化把字形重新編號，繪製引擎用
> **原始編號**去取 → 什麼都畫不出來。**文字層完全正常**（搜尋、複製、抽取都對），
> 只有畫面空白，所以任何「文字抽得到」的檢查都會誤判成通過。

- [ ] **一律算圖數墨水**，不可以用 `get_text()` 當作通過的依據
- [ ] 表單自動填寫：填入中文 → 下載的 PDF **看得到字**
- [ ] 用印與簽名（含日期、個資限用章）：中文看得到
- [ ] 插入頁碼：中文頁碼格式（第 N 頁）看得到
- [ ] 浮水印：中文浮水印看得到
- [ ] 書籤與目錄的目錄頁：標題看得到
- [ ] 產出檔案**沒有暴增**（子集化仍在生效，約 820 KB 而不是 16 MB）
- [ ] `tests/test_cjk_font_renders.py` 全綠

### 6.22 v1.14.22 — 目錄頁的插入位置（每次發版必過）

- [ ] 「插在第幾頁」填 1 → 目錄在最前面（與舊行為相同）
- [ ] 填 2 → 目錄排在**封面後面**，第 1 頁仍是原本的封面
- [ ] **插入點之前的書籤頁碼不動**（封面那筆仍是第 1 頁，不可以指到目錄自己）
- [ ] 目錄上印的頁碼與**目錄項目的連結**都符合同一個規則
- [ ] 填超過總頁數不會炸掉（會夾到合法範圍）

### 6.23 v1.14.23 — 預覽縮圖不可以讓人誤判邊界（每次發版必過）

> 加框工具的預覽縮圖，卡片自己有一圈灰框線 + 白色內距 → 看起來像「框線離
> 頁緣還有距離」，實際上是貼齊的。使用者要判斷的正是框線位置。

- [ ] 預覽縮圖的容器**沒有自己的框線**（灰底襯白紙加陰影）
- [ ] 頁面加框：邊距設 **0** → 框線正好在白紙邊緣，外面直接是灰底，
      **不可以有白色間隙**
- [ ] 頁面加框：邊距設 5mm → 看得出框線確實內縮
- [ ] 頁面尺寸統一：預覽圖裡的灰框是**目標紙張邊界**，
      容器不可以再畫一條混淆
- [ ] 騎縫章、書籤與目錄的縮圖同樣處理

### 6.24 v1.14.24 — 工具之間的檔案交接（每次發版必過）

> 之後的「工作流程串多個工具」會走同一條路，所以這一節驗的是**通用機制**，
> 不是書籤→頁碼這一對。

- [ ] 書籤與目錄做完 → 結果訊息有「用『插入頁碼』補上」的連結
- [ ] 點下去 → **頁碼工具收到那份檔案**（檔名正確，不是 `document.pdf`）
- [ ] 檔案有存進**我的工作區**（`source_tool` 記著來源工具）
- [ ] 網址上的 `from_ws` / `from_job` / `from_name` **用完就清掉**
      （重新整理不該再抓一次）
- [ ] 一頁有多個上傳框時（如騎縫章），**只有第一個**吃這個參數
- [ ] 工作區被管理員停用 → 退回 `from_job`，功能仍可用
- [ ] 拿**別人的** file_id / job_id → 取不到（伺服器端驗歸屬）

### 6.25 v1.14.24 — 更新後前端要立刻生效（每次發版必過）

- [ ] `curl -I /static/js/file_upload.js` 有 `Cache-Control: no-cache`
- [ ] 沒有這個標頭時瀏覽器會用啟發式快取 → 升級後好幾小時還在跑舊的
      JS / CSS，**重新整理也沒用**；開發時就踩過一次
- [ ] 改過前端之後實測：更新 → 重新整理 → 新功能立刻可用

### 6.26 v1.14.25 — 書籤與目錄的預設值、檔名、預覽連動（每次發版必過）

- [ ] 「產生可以印出來的目錄頁」**預設是勾起來的**
- [ ] API 的 `toc_page` **仍然預設 `false`**（不可以連動改掉，
      會讓既有自動化呼叫突然多一頁）
- [ ] 上傳 `年度報告.pdf` → 產出檔名是 **`年度報告_bookmarked.pdf`**
      （不是寫死的 `bookmarked.pdf`）
- [ ] 接到「插入頁碼」時帶過去的檔名**是產出檔名**（`result_filename`），
      不是輸入檔名
- [ ] **改書籤標題或頁碼 → 目錄預覽跟著重畫**
      （目錄內容就是那張表，不重畫等於顯示的是上一版）

### 6.27 v1.14.26 — 貼上清單的解析效能與用詞（每次發版必過）

- [ ] 「書籤與目錄」貼上**一行兩萬個點、結尾沒有數字**的內容 ×20 行
      → **一秒內**解析完（原本每行要 5.4 秒，是可以拿來癱瘓伺服器的輸入）
- [ ] 正常的目錄清單解析結果不變（層級、引導點、警告訊息）
- [ ] `tests/test_taiwan_terminology.py` 全綠
      —— 只掃**使用者看得到的文字**；程式註解、說明文件、
      以及**刻意收錄大陸用詞的搜尋關鍵字**都要排除

### 6.28 v1.14.27 — cryptography 升版與解析效能（每次發版必過）

- [ ] `cryptography` 已是 50.x（49.0.0 的 PKCS#7 有 Bleichenbacher oracle；
      本專案只用 Fernet 與 PyJWT RS256，**沒有用到 PKCS#7**，屬不可利用，
      但 49.x 無修正版可退）
- [ ] SSO（OIDC / SAML）端對端測試全綠 —— 升 cryptography 最可能撞到的就是這裡
- [ ] Fernet 加解密正常（SSO 設定、通知管道的密鑰都靠它）
- [ ] 「書籤與目錄」貼上清單的解析，**每一版踩過的最壞輸入都要跑**：
      整行都是點 / 一長串數字接非空白 / 數字後一大片空白
      —— **換了寫法就要重新設計最壞輸入**，拿舊的去驗會誤判成修好了

### 6.29 表單自動填寫 — 改動必跑全表單回歸（每次發版必過）

> 定位邏輯（`compute_value_slot` / `pdf_form_detect`）**所有表單都會走**。
> 改壞了使用者不會馬上發現，等表寄出去才知道欄位填錯格。

- [ ] 改動**之前**先存基準：
      `python temp_pdfs/_regress/run_fill_regress.py --save before`
- [ ] 改完比對：`... --compare before`
- [ ] **判準：沒有任何一份變差**
      —— 填入數不可減少、**疊字不可增加**、原本座標不可位移
- [ ] 非填寫類（公文 / 說明書）自動略過，不列入判準
- [ ] 樣本涵蓋五種特殊版型（後置標籤 / 純底線 / 雙欄 / 直書標籤欄 / 逐格分寫）
- [ ] **樣本不可上 git、檔名與客戶名不可寫進 CHANGELOG**
      （`tests/test_no_sample_names_in_public.py` 會擋）

### 6.30 v1.12.95 — .docx 表單底色蓋掉整頁文字（VML z-index）（每次發版必過）

> Word 匯出對**純圖形**走 VML，而 VML 的 z-index 匯出時整個不寫 → 依規範
> 等同疊在文字層之上，底色塊把整頁文字蓋掉（.odt 正常、只有 .docx 壞）。
> 在 ODF 端設 `draw:z-index` 救不了 —— 資訊在匯出當下就掉了。
> 修法是轉出 .docx 後直接改寫（`_fix_docx_vml_zorder`）。
> （2026-08-16 稽核發現這宗一直沒進 §6，補上。）

- [ ] 含底色塊的表單 PDF 轉 .docx，開檔後文字**在色塊之上**（不是被蓋掉）
- [ ] 同一份轉 .odt 對照 —— 兩種輸出都要對

### 6.31 v1.14.34-35 — 作業完成列的按鈕要跟實際產出一致（每次發版必過）

> 兩宗同根因：**同一份清單在兩個地方各寫一份，遲早漂掉。**
> ①「下載 PNG」原本無條件顯示，但那個端點是把結果 PDF 算成圖 ——
> 產出不是 PDF 的工具掛著一顆必然失敗的鈕。
> ②「存至工作區」的副檔名判斷寫死在 JS（pdf|png|docx|odt），伺服器端
> v1.14.6 就多收了 xlsx/ods/pptx/odp —— 伺服器收得下、鈕卻不出現，
> 而且沒有任何錯誤訊息（使用者親自看到才回報）。

- [ ] 轉出 `.xlsx` / `.odp`：主下載鈕顯示「下載 .xlsx」等（不是「下載 PDF」）
- [ ] 產出非 PDF 時「下載 PNG」不出現；產出是 PDF 時要出現且能下載
- [ ] 產出 `.xlsx`「存至工作區」出現、按下真的存進去
- [ ] `tests/test_workspace_save_button.py` 全綠（JS 不可再寫死清單）

### 6.32 v1.14.34 — soffice 的回傳碼不可靠（每次發版必過）

> soffice 會一邊印無關警告（找不到 Java）一邊正常轉完，也可能在收尾才被
> 中止（實測 rc=137 = SIGKILL，多半是記憶體或同時轉太多份）。
> 先看回傳碼會把**已轉好的檔案白白丟掉**。判準一律是「有沒有拿到可用檔案」。

- [ ] `tests/test_office_convert.py` 的
      `test_good_output_wins_over_bad_exit_code` /
      `test_killed_process_says_so_instead_of_blaming_the_format` 綠
- [ ] 同副檔名互轉（pptx→pptx）真的有轉（無聲跳過守門也在同一檔）

### 6.33 v1.14.37 — 毀損檔案一律 400，不可 500（每次發版必過）

> 2026-08-16 全端點壞輸入掃描：毀損 PDF 打全部工具端點，**28 個回 500**。
> 使用者會以為服務掛了而一直重試（其實是檔案壞了），監控端全是假警報。
> 修法是全域 `fitz.FileDataError` 處理器（同 JSONDecodeError 的做法）。

- [ ] `tests/test_broken_input_no_500.py` 全綠
      （從路由表自動列舉全部工具 POST 端點，新工具自動被涵蓋）

