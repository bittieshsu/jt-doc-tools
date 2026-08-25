# Jason Tools 文件工具箱 —— 容器映像檔
#
# 主要用途是**封閉網路部署**（GitHub issue #45）：在有網路的機器建好映像檔，
# `docker save` 成 tar 帶進內網，`docker load` 後直接跑，不需要目標機器連外。
#
# 兩個刻意的取捨：
#
# 1. **預設不裝 EasyOCR / PyTorch**（`WITH_EASYOCR=0`）。理由有三：
#    * Linux 的 torch wheel 會拉進 2 GB 以上的 **NVIDIA CUDA 執行期**，而那些是
#      **專有授權**元件 —— 和本程式（AGPL-3.0）打包成同一個散布物有疑慮。
#      **且 Docker 分層不可變：先裝再移除，檔案仍留在前一層**，映像檔不會變小、
#      元件也還在。所以只能「根本不裝」。
#    * 容器裡多半沒有 GPU，裝了也只是 CPU 推論。
#    * **OCR 仍然可用** —— tesseract 已內建，程式偵測不到 easyocr 會自動退回它
#      （`ocr_engine.is_easyocr_available()` 用 find_spec，不會 import 失敗）。
#      要快就接「外接 GPU OCR 伺服器」，那才是這個專案建議的加速路徑。
#    真的要在容器內跑 EasyOCR：`--build-arg WITH_EASYOCR=1`（映像檔會大 2 GB+，
#    並且會含 NVIDIA 專有元件，散布前請自行確認授權）。
# 2. **Office 引擎預設裝**（`WITH_OFFICE=1`，約 +500 MB）。少了它，格式轉換那
#    一整類工具會在執行時才失敗 —— 這正是本專案一再犯的「宣稱支援卻沒有裝」
#    那個形狀。真的不需要再用 `--build-arg WITH_OFFICE=0` 關掉。

FROM python:3.12-slim-bookworm AS base

ARG WITH_OFFICE=1
ARG WITH_EASYOCR=0
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    JTDT_DATA_DIR=/data \
    JTDT_HOST=0.0.0.0 \
    JTDT_PORT=8765

# --- 系統相依 ---
# fonts-noto-cjk 是**必要**的：少了它，寫進 PDF 的中文會變成空白方框
# （本程式在字型缺席時會提示，但那是給沒辦法的環境用的退路，不是預設狀態）。
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl fonts-noto-cjk fonts-noto-color-emoji \
        libgl1 libglib2.0-0 zbar-tools \
    && apt-get install -y --no-install-recommends \
         tesseract-ocr tesseract-ocr-chi-tra tesseract-ocr-eng \
    && if [ "$WITH_OFFICE" = "1" ]; then \
         apt-get install -y --no-install-recommends libreoffice-writer libreoffice-calc \
             libreoffice-impress libreoffice-draw default-jre-headless; \
       fi \
    && rm -rf /var/lib/apt/lists/*

# --- Python 相依 ---
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
# 先只複製相依宣告 —— 程式碼改動不會讓這一層失效（重建快很多）
COPY pyproject.toml uv.lock README.md docker-gpu-excl.py ./
# **兩道 sync 都要帶排除清單。** 一開始只有第二道帶，結果 CUDA 在這個「相依
# 快取層」就被裝進去了 —— 而 Docker 分層不可變，後面再排除也沒用（映像檔照樣
# 大 2 GB，專有元件照樣在裡面）。
RUN mkdir -p app && touch app/__init__.py \
    && GPU_EXCL=$(python docker-gpu-excl.py) \
    && if [ "$WITH_EASYOCR" = "1" ]; then \
         uv sync --no-dev --no-install-project; \
       else \
         uv sync --no-dev --no-install-project \
           --no-install-package torch --no-install-package torchvision \
           --no-install-package easyocr $GPU_EXCL; \
       fi \
    && uv cache clean

COPY . /app
# 預設把 torch / torchvision / easyocr 整組排除 —— 連帶 nvidia-* 與 triton 都
# 不會被裝（`uv sync --dry-run` 實測：排除這三個之後，輸出裡 nvidia/cuda/triton
# 的行數是 0）。**這是「根本不裝」而不是「裝了再刪」** —— 後者在 Docker 分層
# 下沒有意義。
# 排除清單**從 uv.lock 現算**，不寫死 —— 寫死的話 lock 更新換了套件名（例如
# `nvidia-cudnn-cu13` 這種帶版本後綴的），排除就會悄悄失效，而症狀是「映像檔
# 突然大了 2 GB」，沒有人會注意到。
# 只排除 torch 是不夠的：那些 nvidia-* 在 lock 裡是**獨立的套件節點**，
# 照樣會被裝進來（第一次就是這樣，被下面的守門擋下）。
RUN GPU_EXCL=$(python docker-gpu-excl.py) \
    && echo "排除的 GPU / 專有套件：$GPU_EXCL" \
    && if [ "$WITH_EASYOCR" = "1" ]; then \
        uv sync --no-dev; \
    else \
        uv sync --no-dev \
            --no-install-package torch \
            --no-install-package torchvision \
            --no-install-package easyocr $GPU_EXCL; \
    fi \
    && uv cache clean \
    # 守門：預設組態下不可以出現任何 NVIDIA 專有元件。驗不過就讓建置失敗，
    # 不可以讓它「靜靜地」被裝進來。
    && if [ "$WITH_EASYOCR" != "1" ] \
       && ls /app/.venv/lib/python3.12/site-packages/nvidia >/dev/null 2>&1; then \
         echo "❌ 映像檔裡出現 NVIDIA CUDA 套件（專有授權）—— 見 Dockerfile 說明"; \
         exit 1; \
       fi \
    # 啟動所需的相依必須齊全（缺了要在**建置時**就知道，不是使用者按下按鈕才發現）
    && /app/.venv/bin/python -c "import fastapi, fitz, PIL, pillow_heif, pdfplumber, docx, odf, openpyxl, pyzipper, httpx, psutil, pyotp, qrcode, pdf2docx, rapidfuzz, fontTools, numpy, lxml, pymupdf4llm, markdown_it, jwt, onelogin.saml2.auth, xmlsec, truststore, ldap3; print('deps OK')"

# --- 執行身分與資料目錄 ---
# 不以 root 執行；資料目錄掛成 volume（升級換映像檔時資料留著）
RUN useradd --system --uid 10001 --create-home --home-dir /home/jtdt jtdt \
    && mkdir -p /data && chown -R jtdt:jtdt /data /app
USER jtdt
VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["/app/.venv/bin/python", "-c", \
         "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=3).status==200 else 1)"]

CMD ["/app/.venv/bin/python", "-m", "uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8765"]
