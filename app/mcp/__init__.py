"""MCP（Model Context Protocol）伺服器：讓 AI 客戶端（Claude Code、Codex CLI、OpenCode…）呼叫本站不用 LLM 的工具。

v1.17.1 起。規劃在內部文件（MCP 規劃 v1）。重點：

* 自己寫的 JSON-RPC 2.0（Streamable HTTP、只回 JSON、無狀態），不用官方 SDK、不加相依。
* **上下文不爆**：不管開放多少支工具，`tools/list` 只有固定幾個（目錄、查參數、執行、工作區、作業）。
* 執行工具時**在同一個行程裡用同一張 Token 呼叫原本的 `/tools/<工具>/api/...`** ——
  認證、工具權限、檔案檢查、稽核、使用紀錄跟 REST 完全一樣，不另寫一套。
* 檔案內容不經過模型：用一次性傳輸票證讓代理自己 `curl` 上傳 / 下載（`tickets.py`）。
* 預設關閉；Token 要另外打開「允許 MCP」。
"""
