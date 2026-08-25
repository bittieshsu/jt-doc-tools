# 封閉網路 / 離線安裝

給「機器連不到 GitHub 與 PyPI」的環境（GitHub issue #45）。兩條路，**先看你的
網路實際長什麼樣**再選：

| 你的情況 | 選哪條 |
|---|---|
| 目標機器完全不能連外，但你有另一台可以連外的機器 | **A. Docker 映像檔搬運** |
| 公司有 PyPI 代理 / 內部鏡像，GitHub 可間接取得 | **B. 走代理直接安裝**（不必做映像檔） |

兩條都不需要改程式，也不需要我們提供特製版本。

---

## A. Docker 映像檔搬運

### A-1. 在**有網路**的機器建映像檔

```bash
git clone https://github.com/jasoncheng7115/jt-doc-tools.git
cd jt-doc-tools
docker build -t jt-doc-tools:1.14.52 .
```

建置約 10–20 分鐘（視網速），映像檔約 **3.7 GB**。

### A-2. 匯出成單一檔案

```bash
docker save jt-doc-tools:1.14.52 | gzip -1 > jtdt-image.tgz    # 約 940 MB
```

用隨身碟 / 內部檔案交換區把 `jtdt-image.tgz` 帶進內網。

### A-3. 在**內網**機器載入並啟動

```bash
docker load -i jtdt-image.tgz
docker volume create jtdt-data          # 資料目錄，換映像檔時資料留著
docker run -d --name jt-doc-tools \
    -p 8765:8765 \
    -v jtdt-data:/data \
    --restart unless-stopped \
    jt-doc-tools:1.14.52
```

開瀏覽器到 `http://<內網機器>:8765/`。

> **對外服務請放在反向代理 + HTTPS 後面**，不要把 8765 直接暴露 —— 見
> [OPS.md](OPS.md)。

### A-4. 之後怎麼升級

在有網路的機器重新 build → save → 帶進內網 → load → 換容器：

```bash
docker stop jt-doc-tools && docker rm jt-doc-tools
docker run -d --name jt-doc-tools -p 8765:8765 -v jtdt-data:/data \
    --restart unless-stopped jt-doc-tools:<新版本>
```

**資料在 volume 裡，不會因為換映像檔而消失**（設定、帳號、稽核、歷史都在
`/data`）。升級前仍建議先 `docker run --rm -v jtdt-data:/data -v $PWD:/backup
alpine tar czf /backup/jtdt-data.tgz -C /data .` 備一份。

### 這個映像檔裡有什麼、沒有什麼

| 項目 | 狀態 |
|---|---|
| 全部 46 個工具 | ✅ |
| LibreOffice（格式轉換那一整類） | ✅ 內建 |
| 中文字型（Noto CJK） | ✅ 內建 —— 少了它，寫進 PDF 的中文會是空白方框 |
| HEIC / HEIF 照片 | ✅ 內建 |
| OCR（tesseract，含繁中） | ✅ 內建 |
| **OCR（EasyOCR / PyTorch）** | ❌ **刻意不裝**，見下 |

**為什麼不裝 EasyOCR**：Linux 的 PyTorch wheel 會拉進 2 GB 以上的 **NVIDIA CUDA
執行期**，那是專有授權元件，而本程式是 AGPL-3.0，打包成同一個散布物有疑慮；
容器裡多半也沒有 GPU。**OCR 仍然可用** —— 程式偵測不到 EasyOCR 會自動退回
tesseract。要更快的 OCR 請用「外接 GPU OCR 伺服器」（管理區有一鍵部署腳本）。

真的要在映像檔內含 EasyOCR：`docker build --build-arg WITH_EASYOCR=1 .`
（映像檔會大 2 GB 以上，並含 NVIDIA 專有元件，散布前請自行確認授權）。

---

## B. 走公司代理直接安裝

目標機器連得到**公司的 PyPI 代理**、GitHub 可間接取得時，用這條 —— 不必做
映像檔，之後也能照常 `jtdt update`。

### B-1. 取得原始碼

```bash
# 情況一：內部有 git 鏡像
git clone https://<內部git>/jt-doc-tools.git /opt/jt-doc-tools

# 情況二：從能連外的機器下載壓縮檔帶進來
#   curl -L https://github.com/jasoncheng7115/jt-doc-tools/archive/refs/heads/main.tar.gz -o jtdt.tgz
tar xzf jtdt.tgz && mv jt-doc-tools-main /opt/jt-doc-tools
```

### B-2. 指向公司的套件代理再安裝

```bash
export UV_INDEX_URL="https://<公司代理>/simple"     # uv 讀這個環境變數
export PIP_INDEX_URL="https://<公司代理>/simple"    # 少數 fallback 路徑會用到

# 封閉網路一定會被安裝腳本的網路預檢擋下（它會探 github.com / jsdelivr /
# astral.sh）—— 明確略過：
export JTDT_SKIP_NET_CHECK=1

# uv 也抓不到（它預設從 astral.sh 下載）。系統上已經有 uv 就會自動沿用；
# 放在非標準路徑時用 JTDT_UV_PATH 指給它：
#   apt install uv   /   pip install uv --index-url $PIP_INDEX_URL   /   直接複製 binary
export JTDT_UV_PATH=/usr/bin/uv        # 選填，PATH 上找得到就不用設

cd /opt/jt-doc-tools
sudo -E bash install.sh                # -E 保留上面這些環境變數
```

> 企業 TLS 檢查（代理換憑證）不必特別處理 —— 安裝腳本預設就已經
> `UV_NATIVE_TLS=true`（改用作業系統信任庫），程式本身的 `net_ssl` 也是同一套。

### B-3. 之後的更新

```bash
export JTDT_REPO_URL="https://<內部git>/jt-doc-tools.git"   # 指向內部鏡像
export UV_INDEX_URL="https://<公司代理>/simple"
sudo -E jtdt update                                        # -E 保留環境變數
```

---

## 常見狀況

**Q：內網沒有 Docker，也沒有 PyPI 代理，能離線裝嗎？**
可以，但要自己把整個 `.venv` 一起搬：在**同樣的作業系統與 CPU 架構**上裝好
（`bash install.sh`），整個 `/opt/jt-doc-tools` 打包帶過去，解到相同路徑即可。
不同發行版 / 架構之間**不要**這樣搬 —— 內含編譯過的原生模組。

**Q：OCR 的模型檔要另外準備嗎？**
tesseract 的繁中訓練檔已經在映像檔裡。EasyOCR 首次辨識會下載約 150 MB 模型 ——
封閉網路裡請改用 tesseract 或外接 GPU OCR 伺服器。

**Q：LLM 加值功能在離線環境還能用嗎？**
可以，但要有**地端 LLM 伺服器**（Ollama / vLLM 等）並在管理區指向它。
沒有的話，那些功能維持關閉，其餘工具完全不受影響。
