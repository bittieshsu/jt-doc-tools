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

### 1.6 使用者工作區 (`tests/test_workspace.py` + `tests/test_workspace_api.py`)
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

### 1.10 乘車證明整理（`tests/test_transit_proof_parser.py` + `tests/test_transit_proof_api.py`）🆕

- 解析器：高鐵電子車票證明（label：value）+ 台鐵購票證明（打散版面用特徵正則）；日期正規化 ISO、乘車日排除印製日期、乘車區間抽起訖時間 / 站名、車種不被「乘車區間」誤匹配、高鐵站名去「高鐵 / 車站」；非乘車證明 / 空欄位 → ParseError。
- 端點：頁面渲染、上傳解析 + 票號去重、非乘車證明 PDF 進 failed、7 種格式匯出（csv/xlsx/ods/json/xml/txt/md）+ 非法格式 400 + 空清單 400、CSV 預設 4 欄（日期/交通工具/來源-目的/費用）、設定 roundtrip（勾選 / 順序 / 格式 / 匯出標題）套用到匯出、刪除單筆、對外 API 不寫 buffer。
- **手動驗收**：拉多張台鐵 + 高鐵 PDF → 表格出現 4 欄 + 底部加總；「設定」加欄位 / 改格式 / 排序 → 表格與匯出同步；各格式下載可開。合成 PDF 測試須用 CJK 字型（`fontname="china-t"`）否則抽文字變 notdef。

### 1.9 目錄瀏覽 filter（`tests/test_dir_filter.py` + `tests/test_directory_filter_api.py`）🆕

- 純函式：規則 → LDAP filter（類型→objectClass、名稱關鍵字 escape_filter_chars 轉義、多欄位）；符合物件 → 剪枝樹（祖先鏈、共用祖先合併去重、matched 旗標、parent 排在 child 前、cycle-safe、無 root 停在 DC 層）。
- 設定 roundtrip / 清洗（空規則丟棄、無效類型過濾、無效 default_mode 忽略）。
- 端點：`/directory/filter` GET/POST roundtrip（backend-agnostic）；`/directory/selected` 非目錄後端回 400；目錄頁可渲染。
- **手動驗收（需 LDAP / AD）**：進 /admin/directory → 預設「已選定」模式；設定 filter 加規則（名稱關鍵字 + 類型 + OU 子樹）→ 儲存 → 樹只留符合分支；切「全部」看完整目錄樹；點 OU 指派角色仍正常。

### 1.8 每頁畫面 + 關鍵元素可見性回歸（`scripts/page_visual_check.py`）🆕

**目的**：抓「元素 / 功能靜默消失」這一類 regression（例：v1.12.30 CSP 樣式重構
讓「下載」按鈕、臨時資產縮圖、個資限用章預覽在存檔 / 選圖後一直不顯示，
v1.12.71 修）。純像素比對對字型 / 時間戳 / 動態內容太吵，所以主檢查是
「可見互動元素清單」比對 + 關鍵狀態斷言，截圖僅供人工對照。

**需要**：headless chromium（dev1：`chromium-browser`）+ 一個 auth-off 本機實例。

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

## 2. 手動驗收清單（每個版本）

### 2.1 填單用印

#### PDF 表單填寫 (pdf-fill)
- [ ] 上傳廠商 PDF（華儲 / Macpower / momo / Tigerair）
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
- [ ] **2×3 位置選擇格**點擊直接換位置
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

### 2.4 格式轉換

#### 文書轉 PDF (office-to-pdf)
- [ ] .docx / .xlsx / .pptx / .odt 各轉一份
- [ ] OxOffice 優先（`find_soffice` 命中 OxOffice）

#### 文書轉圖片 (pdf-to-image) 🆕 擴充 Office
- [ ] PDF 每頁 → PNG
- [ ] **Office 檔案（docx/xlsx/pptx/odt）先自動轉 PDF 再轉圖**
- [ ] 單頁直接下 PNG、多頁自動 ZIP

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
- [ ] 視窗縮窄到 ≤ 900px：側欄收起、漢堡按鈕展開、項目正確點擊

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

## 4. API 覆蓋檢查 🆕（v1.8.55 起完整列出，現 37 個工具）

每個工具至少 1 個 `/api/<tool-id>` endpoint（路徑：`/tools/<tool-id>/api/<tool-id>` 或 `/tools/<tool-id>/convert`）。發版前 curl 抽測：

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
- [ ] `/tools/pdf-to-image/convert` — POST file → ZIP/PNG
- [ ] `/tools/pdf-to-office/convert` — POST file → docx/odt（async job）
- [ ] `/tools/image-to-pdf/api/image-to-pdf` — POST files[] → PDF
- [ ] `/tools/scan-merge/api/scan-merge` — POST files[] → 單張 A4 白底 PDF

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

### 共通驗證項
每個 endpoint 至少要：
- [ ] 拒絕非 PDF / 空檔（400）
- [ ] 啟用認證時 token 驗證 + ACL（`upload_owner.require()` 防跨 user 取檔）
- [ ] 大檔（> 限額）回 413 而不是 OOM
- [ ] 回應 `Content-Disposition` 中文檔名 RFC 5987（走 `http_utils.content_disposition`）

### 自動化覆蓋（理想）
新加 endpoint 都應該在 `tests/test_api_endpoints.py`（待補）跑單元測試 — POST 假 PDF + assert 200 + assert 回傳格式。發版前 `uv run pytest tests/test_api_endpoints.py -v` 必綠。

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
