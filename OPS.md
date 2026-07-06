# 日常運維指南

## `jtdt` 指令

安裝完成後會在系統 PATH 加入 `jtdt`：

| 指令 | 說明 |
|------|------|
| `jtdt status`        | 顯示版本、服務狀態、安裝路徑 |
| `jtdt start`         | 啟動服務 |
| `jtdt stop`          | 停止服務 |
| `jtdt restart`       | 重啟服務 |
| `jtdt logs -f`       | 即時看 log |
| `jtdt open`          | 用瀏覽器開啟介面 |
| `sudo jtdt update`<sup>†</sup>   | 升級到最新版（會自動備份資料） |
| `sudo jtdt uninstall`<sup>†</sup> | 移除（資料保留），加 `--purge` 連同資料一起刪 |
| `sudo jtdt bind <addr>:<port>`<sup>†</sup> | 改 listen 位置（詳見「監聽位置」） |
| `sudo jtdt auth show`<sup>†</sup> | 顯示認證後端設定 |
| `sudo jtdt auth disable`<sup>†</sup> | 緊急關掉認證 |
| `sudo jtdt auth set-local`<sup>†</sup> | 切回本機帳號模式 |
| `sudo jtdt reset-password <user>`<sup>†</sup> | 重設使用者密碼 |
| `sudo jtdt audit-user create <name>`<sup>†</sup> | 建立稽核員帳號 |

<sup>†</sup> Linux / macOS 用 `sudo`；Windows 沒有 `sudo`，請改成「以系統管理員身分執行 PowerShell」後跑 `jtdt update` / `jtdt uninstall`。

## 升級流程

`jtdt update` 自動：
1. 停服務
2. 備份 `data/` (最近 3 份保留)
3. `git pull` 從 GitHub
4. `uv sync` 同步依賴
5. 重啟
6. 健康檢查

降版會被拒（避免毀資料）。失敗會自動 rollback。

## 企業 TLS 攔截環境（更新 / 下載出現 CERTIFICATE_VERIFY_FAILED）

公司若有 **TLS 檢查代理 / 防火牆**會把外部 HTTPS 憑證換成自家 CA。徵狀：
`jtdt update`、下載 OCR 語言檔（tessdata）、或 LLM / 遠端 OCR / SSO 連線時出現

```
SSL: CERTIFICATE_VERIFY_FAILED ... Missing Authority Key Identifier
```

原因：企業 CA 裝在 **OS 系統信任庫**，但 Python（程式內建的獨立 Python）用的是
自帶的 certifi 憑證庫，**不認**那個企業 CA。

**v1.12.43 起自動處理 — 不需任何設定。** 只要照原本方式安裝 / 更新即可：

```bash
# 既有安裝：直接更新（建議重跑網站一行安裝指令，確保拿到最新 install.sh）
curl -fsSL https://raw.githubusercontent.com/jasoncheng7115/jt-doc-tools/main/install.sh | sudo bash
# 或
sudo jtdt update
```

install.sh / `jtdt update` / 程式啟動會**自動**：① 讓 uv 用 OS 信任庫
（`UV_NATIVE_TLS` / `UV_SYSTEM_CERTS`）；② 把 `SSL_CERT_FILE` 指到 OS 系統 CA
bundle（`/etc/ssl/certs/ca-certificates.crt` 等）；③ 用 **truststore** 把程式執行時的
Python ssl 接到 OS 原生信任庫。企業 CA 既然已在 OS 信任庫（`apt` / `curl` / `git` 能動
即代表有），上述三層就會自動認得，HTTPS 下載全部成功 —— **客戶端零設定**。

> 註：既有客戶若卡在舊版,**重跑上面的網站一行安裝指令**最保險（會拿到含此修正的新
> install.sh，第一次就成功）；若用 `jtdt update`,因更新當下跑的是舊版邏輯,可能需再跑一次。

**例外 — 萬一企業 CA 還沒進 OS 信任庫**（少數情況，連 `apt`/`curl` 也失敗）：先把 CA
裝進系統信任庫即可（Debian：`sudo cp 企業CA.crt /usr/local/share/ca-certificates/ &&
sudo update-ca-certificates`；Windows 匯入「受信任的根憑證授權單位」；macOS 加入鑰匙圈並信任）。
真的不便處理時，最後手段 `sudo JTDT_TLS_INSECURE=1 jtdt update`（停用驗證，有 MITM 風險，僅信任內網用）。

## ⚠ 不建議直接對「公開網際網路」開放

> **首選部署方式：只在內網 / VPN 使用，不要把服務放到公開網際網路上。**
>
> **最主要的原因是機密性**：同仁多半會用這套工具處理公司**內部 / 機密文件**（合約、報價、
> 個資、統編資料等）。一旦直接對外開放，等於把這些文件與管理後台一併暴露出去，只要被人
> 猜到網址或攻破認證，內部文件就會外洩。
>
> 除了外洩風險，攻擊面本身也高：本工具的本質是「解析使用者上傳的 PDF / Office / 圖片」，底層用 MuPDF（PyMuPDF）、
> LibreOffice（soffice）、Pillow 等**記憶體不安全的原生程式**，屬於高風險攻擊面；
> 一旦有人上傳特製檔案打中解析器漏洞，可能造成服務當機甚至更嚴重後果。加上系統可能
> 含**統編資料庫等大量資料**與管理後台，**放到公開網路上等於把這些攻擊面暴露給全世界**。
>
> **建議**：限縮在公司內網或 VPN 後、只給需要的人用。若因業務**必須**對外，這是「風險
> 自負」的選擇，至少要全部做到：
> - 反向代理 + HTTPS（見下）、只綁 `127.0.0.1`、防火牆只放代理
> - 啟用認證 + 強密碼 + **強制 2FA**、限縮角色權限
> - 前面加 **WAF / 速率限制**（登入端點 + 上傳端點）
> - 設 `client_max_body_size` 限縮上傳大小、持續更新相依套件（盯 Dependabot / CodeQL）
> - 考慮把 soffice 轉檔放進沙箱 / 獨立低權限帳號、封鎖對外連線

## 反向代理（HTTPS）— 非本機存取的**強制**安全要求

> ⚠️ **只要不是「本機單人」使用（任何網路 / 多人 / 內網其他電腦 / 對外）→ 一律放在
> nginx（或 Caddy）反向代理 + HTTPS 後面,絕不要把 `:8765` 直接對網路開放。**
>
> 應用程式預設只綁 `127.0.0.1:8765`（純 HTTP、無 TLS）。直接 `jtdt bind 0.0.0.0`
> 對網路開放等於明文傳輸帳號密碼 / 文件內容,且少了 TLS 終結、HSTS、憑證、速率
> 限制等防護。**正確做法**:`:8765` 維持只聽 `127.0.0.1`,由同機 / 內網的 nginx
> 反向代理對外提供 HTTPS。下面是含資安設定的完整範例。

### 不管用哪一套反向代理，都必須做到這 7 點

無論 nginx / Caddy / Apache / HAProxy / Traefik / IIS / F5，共通要求一致：

1. **掛根路徑 `/`**（不能 `/jtdt/`）— 所有頁面用絕對路徑。
2. **body 上限 ≥ 100 MB** — 上傳大檔需要。
3. **傳 `X-Forwarded-Proto`** — 後端據此設 Secure cookie + HSTS（SSL 在代理卸載時尤其必要）。
4. **傳 `X-Forwarded-For`（真實用戶端 IP）** — 稽核 / 歷史顯示來源 IP 靠它。**並且要覆寫進站的 XFF**（見下方防偽造）。
5. **read / send 逾時 ≥ 900s** — LLM 工具（翻譯 / OCR 校驗 / pdf-fill 視覺校驗）單筆推理常 5–15 分鐘，預設 60s 會 504。
6. **安全標頭由後端 app 統一設定**（CSP / HSTS / X-Frame-Options…）— 代理**不要**再加一次，否則重複標頭（ZAP「Multiple Header Entries」）。
7. **後端只聽 `127.0.0.1:8765`** — 對外只由代理提供 HTTPS，並隱藏代理版本號。

> **防 `X-Forwarded-For` 偽造（稽核 IP 正確性）**：稽核 / 歷史記錄的來源 IP 取 `X-Forwarded-For` 最左側。若代理是用「附加」模式（nginx `$proxy_add_x_forwarded_for`、HAProxy `option forwardfor`…），惡意的內部使用者可先自帶一個假 XFF，就能把稽核記到假 IP。要杜絕，讓代理**先移除進站的 `X-Forwarded-For`，再由代理自己填真實用戶端**（各範例已標註）。這是**選用的硬化**，不影響一般顯示；跟「反向代理下稽核 IP 變 127.0.0.1」的修正（v1.12.65）無關，該問題只要升級即解。

### nginx

`$proxy_add_x_forwarded_for` 會沿用進站 XFF 再附加；要防偽造改用 `proxy_set_header X-Forwarded-For $remote_addr;`（覆寫成真實 peer）。

```nginx
server {
    listen 443 ssl http2;
    server_name docs.example.com;

    ssl_certificate     /etc/letsencrypt/live/docs.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/docs.example.com/privkey.pem;

    # 不要外洩 nginx 版本號（ZAP「Server Leaks Version Information」）
    server_tokens off;

    # HSTS：jt-doc-tools 應用層已自動設 Strict-Transport-Security（會依
    # X-Forwarded-Proto 判斷 https）。**不要在 nginx 再 add_header 一次**，
    # 否則回應會出現兩個 HSTS 標頭（ZAP「Strict-Transport-Security Multiple
    # Header Entries」）。要在 nginx 統一管也行，但只能擇一來源。

    # 必設：上傳大檔需要
    client_max_body_size 100M;

    # 必設：LLM 工具（翻譯 / OCR 校驗 / 視覺校驗）單筆推理可能 5-15 分鐘
    # 預設 60s 會 504；建議 ≥ 900s（並跟 admin → LLM 設定的 timeout 對齊）
    proxy_read_timeout    900s;
    proxy_send_timeout    900s;
    proxy_connect_timeout 60s;

    # 翻譯回應慢慢吐 — 關 buffering 讓 client 即時看到進度
    proxy_buffering       off;

    location / {
        proxy_pass http://127.0.0.1:8765/;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### Caddy

```caddyfile
docs.example.com {
    reverse_proxy 127.0.0.1:8765 {
        flush_interval -1
        transport http {
            read_timeout 900s
            write_timeout 900s
        }
        # Caddy 預設就會帶 X-Forwarded-For / -Proto / -Host（真實用戶端）
        # 防偽造：只信任前一跳的 XFF（Caddy 會覆寫非信任來源的）
        # trusted_proxies static 127.0.0.1/32
    }
    request_body {
        max_size 100MB
    }
}
```

### Apache httpd（mod_proxy）

需啟用模組：`proxy`、`proxy_http`、`ssl`、`headers`。

```apache
<VirtualHost *:443>
    ServerName docs.example.com
    ServerTokens Prod
    ServerSignature Off

    SSLEngine on
    SSLCertificateFile    /etc/letsencrypt/live/docs.example.com/fullchain.pem
    SSLCertificateKeyFile /etc/letsencrypt/live/docs.example.com/privkey.pem

    # 上傳大檔（100 MB，單位 bytes）
    LimitRequestBody 104857600

    ProxyPreserveHost On
    ProxyRequests Off

    # SSL 在此卸載 → 告知後端原始協定（後端據此設 Secure cookie + HSTS）
    RequestHeader set X-Forwarded-Proto "https"
    # 防偽造：清掉進站帶的 XFF，mod_proxy 會自動補上真實用戶端 IP
    RequestHeader unset X-Forwarded-For early

    # LLM 長工作：拉長逾時（秒）
    ProxyPass        / http://127.0.0.1:8765/ timeout=900 connectiontimeout=60
    ProxyPassReverse / http://127.0.0.1:8765/
</VirtualHost>
```

### HAProxy

```haproxy
defaults
    mode http
    timeout connect 60s
    timeout client  900s        # LLM 長工作
    timeout server  900s

frontend fe_docs
    bind :443 ssl crt /etc/haproxy/certs/docs.example.com.pem
    # 防偽造：先刪進站 XFF，再由 option forwardfor 填真實用戶端
    http-request del-header X-Forwarded-For
    option forwardfor
    http-request set-header X-Forwarded-Proto https
    default_backend be_jtdt

backend be_jtdt
    server jtdt 127.0.0.1:8765
```

HAProxy 以串流轉發，body 大小預設不受限；逾時務必拉到 900s。

### Traefik（v2 / v3，file provider）

```yaml
# 動態設定（file provider）
http:
  routers:
    jtdt:
      rule: "Host(`docs.example.com`)"
      entryPoints: ["websecure"]
      tls: { certResolver: "le" }
      service: "jtdt"
  services:
    jtdt:
      loadBalancer:
        servers:
          - url: "http://127.0.0.1:8765/"
        responseForwarding:
          flushInterval: "-1"     # streaming 友善（翻譯逐句吐）
```

```yaml
# 靜態設定（traefik.yml）：逾時 + 防 XFF 偽造
entryPoints:
  websecure:
    address: ":443"
    forwardedHeaders:
      trustedIPs: ["127.0.0.1/32"]   # 只有這些來源的 XFF 才採信；其餘一律覆寫
    transport:
      respondingTimeouts:
        readTimeout: "900s"
        writeTimeout: "900s"
serversTransport:
  forwardingTimeouts:
    dialTimeout: "60s"
    responseHeaderTimeout: "900s"
```

Traefik 會自動帶 `X-Forwarded-For` / `-Proto`；body 以串流轉發不受限。

### IIS（ARR + URL Rewrite，Windows）

先裝 **Application Request Routing (ARR)** + **URL Rewrite**，在 ARR →「Server Proxy Settings」勾 **Enable proxy**。站台放這份 `web.config`：

```xml
<configuration>
  <system.webServer>
    <rewrite>
      <rules>
        <rule name="jtdt-reverse-proxy" stopProcessing="true">
          <match url="(.*)" />
          <action type="Rewrite" url="http://127.0.0.1:8765/{R:1}" />
          <serverVariables>
            <!-- SSL 在 IIS 卸載 → 告知後端原始協定 -->
            <set name="HTTP_X_FORWARDED_PROTO" value="https" />
          </serverVariables>
        </rule>
      </rules>
    </rewrite>
    <security>
      <requestFiltering>
        <!-- 上傳大檔 100 MB（bytes） -->
        <requestLimits maxAllowedContentLength="104857600" />
      </requestFiltering>
    </security>
  </system.webServer>
</configuration>
```

- ARR 啟用 proxy 後會自動帶 `X-Forwarded-For`（真實用戶端）。
- 要設 `HTTP_X_FORWARDED_PROTO`：URL Rewrite →「View Server Variables」→ 把它加進允許清單，上面 `<set>` 才生效。
- **逾時（LLM）**：ARR → Proxy →「Time-out (seconds)」設 `900`（預設 120）。
- **streaming**：ARR →「Response buffer threshold」設 `0`。

### F5 BIG-IP（LTM）

在 Virtual Server（listen `:443`）掛上 Client SSL profile（TLS 終結）、HTTP profile、與這條 iRule；Pool member 指向後端 `127.0.0.1:8765`（或內部伺服器 `IP:8765`）：

```tcl
# iRule：附加到 HTTPS Virtual Server
when HTTP_REQUEST {
    # SSL 已在 F5 卸載 → 告知後端原始協定
    HTTP::header replace X-Forwarded-Proto "https"
    # 防偽造：清掉進站 XFF，交給 HTTP profile 的「Insert X-Forwarded-For」重填
    HTTP::header remove X-Forwarded-For
}
```

GUI / tmsh 對應設定：
- **HTTP profile**：「Insert X-Forwarded-For」= **Enabled**（F5 自動帶真實用戶端 IP）。
- **Client SSL profile**：掛憑證做 TLS 終結。
- **TCP profile**：「Idle Timeout」拉高到 `900`（LLM 長工作，預設 300）。
- F5 預設不限 body 大小、以串流轉發。

### 反向代理避坑

1. **`client_max_body_size 100M`**：上傳大檔必設
2. **必須掛根路徑** `/`（不能 `/jtdt/`）— 所有頁面用絕對路徑
3. **`proxy_read_timeout 900s`** + **`proxy_send_timeout 900s`** 一起設 — LLM 工具（翻譯 / OCR 校驗 / pdf-fill LLM review）單筆 LLM 呼叫常 5-15 分鐘。`300s` 都不夠用。
4. **`proxy_buffering off`** — 翻譯 / 校驗 streaming 友善，不會卡住等整個 response
5. WebSocket 暫時沒用，不需特別 headers

#### 504 Gateway Timeout 排錯流程

如果使用者翻譯 / OCR 校驗看到 504：

```bash
# 1. 是不是 jt-doc-tools 的 nginx 自己 timeout？
sudo grep "upstream timed out" /var/log/nginx/error.log | tail -5

# 2. 看當前設定值（必須 ≥ 900s）
sudo grep -E "proxy_read_timeout|proxy_send_timeout" /etc/nginx/sites-enabled/

# 3. 設不夠 → 加 / 改成 900s，reload
sudo nginx -t && sudo nginx -s reload
```

**多層反向代理情境**（例：你有獨立 LLM proxy 在前，jt-doc-tools 在後）：**每一層 nginx 都要設**（一層用 60s 預設整鏈就斷），且建議從外到內遞增（client → nginx_jtdt 900s → jtdt → nginx_llm 900s → LLM backend）。

**admin → LLM 設定**內也要把「Timeout（秒）」設 ≥ 900（預設 600，舊版 300）。jtdt 自己的 httpx timeout 短於 nginx 反而會先斬。

## 監聽位置

預設 `127.0.0.1:8765`（只本機）。要改：

```bash
# Linux/macOS
sudo jtdt bind 0.0.0.0:8765      # 監聽所有介面（任何 IP 都可連）
sudo jtdt bind 192.168.1.10:8080 # 只監聽特定 IP + 改 port

# Windows (以系統管理員身分執行 PowerShell)
jtdt bind 0.0.0.0:8765
```

`jtdt bind` 自動寫服務設定 + 重啟服務。

## 備份 / 還原

`data/` 目錄含所有設定 + 上傳記錄 + 簽章 / 印章 / 浮水印 asset + audit log。手動備份：

```bash
# Linux
sudo tar -czf jtdt-backup-$(date +%Y%m%d).tgz -C /var/lib jt-doc-tools/data

# 還原
sudo tar -xzf jtdt-backup-20260509.tgz -C /var/lib
sudo chown -R jtdt:jtdt /var/lib/jt-doc-tools
sudo jtdt restart
```

`jtdt update` 升級時自動 snapshot，最近 3 份保留在 `data/.backup-YYYYMMDD-HHMMSS/`。

## 排程清理

啟用認證後可在 `/admin/retention` 設定每類資料保留天數：

| 項目 | 預設 | 路徑 |
|---|---|---|
| 表單填寫歷史 | 365 天 | `data/fill_history/` |
| 用印簽名歷史 | 365 天 | `data/stamp_history/` |
| 浮水印歷史 | 365 天 | `data/watermark_history/` |
| 暫存上傳 / 工作檔 | 2 小時 | `data/temp/` |
| 稽核記錄 | 90 天 | `data/audit.sqlite` |
| Job 結果 | 24 小時 | `data/jobs/` |

排程：啟動時跑一次 + 每 6 小時跑一次。`-1` = 永久保留。
