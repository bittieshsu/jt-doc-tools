"""MCP 開放的工具登記表 —— **哪些工具、參數、怎麼呼叫原本的 API、結果怎麼處理，只寫在這裡**。

* `name` 是 MCP 上的名稱（英文小寫加底線，三家客戶端都吃得下）；`tool_id` 是本站的工具代號（權限照它）。
* 說明一律英文、短：模型讀英文最一致，也省上下文；畫面上的中文名稱另外有。
* `params` 是 JSON Schema 的子集（type / enum / minimum / maximum / pattern / maxLength / default），
  伺服器端照這份檢查（`validate`），錯了回清楚的錯誤讓模型自己修。
* 沒有列在這裡的工具，MCP 一律叫不到；測試檢查每一支載入的工具都有歸類（`CLASSIFIED`）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

#: 輸入檔案的種類 → 允許的副檔名（工作區記錄的 ext）
PDF = (".pdf",)
OFFICE = (".docx", ".doc", ".odt", ".rtf", ".xlsx", ".xls", ".ods", ".pptx", ".ppt", ".odp", ".odg")
TEXT = (".txt", ".md", ".csv", ".json")
KINDS = {"pdf": PDF, "office": OFFICE, "pdf_or_office": PDF + OFFICE, "doc_or_text": PDF + OFFICE + TEXT}
KIND_TEXT = {"pdf": "PDF", "office": "Office / ODF document", "pdf_or_office": "PDF or Office / ODF document",
             "doc_or_text": "PDF, Office / ODF document or plain text"}


@dataclass(frozen=True)
class McpTool:
    name: str
    tool_id: str
    category: str
    title: str                      # 中文名稱（畫面、稽核用）
    summary: str                    # 一行英文說明（目錄用）
    path: str                       # 原本的 API 路徑
    inputs: Optional[str]           # 輸入檔種類（KINDS 的鍵）；None ＝ 不吃檔案
    min_files: int = 0
    max_files: int = 0
    file_field: str = "file"
    params: dict = field(default_factory=dict)
    required: tuple = ()
    body: str = "form"              # form | json
    output: str = "file"            # file | files | json | text | image_two_step
    output_desc: str = ""
    example: dict = field(default_factory=dict)
    default_on: bool = True
    #: B 類：原本的工具有 LLM 選項 —— 這裡送出的表單一律把它關掉（欄位 → 值）
    force: dict = field(default_factory=dict)


_PAGES = {"type": "string", "maxLength": 200, "pattern": r"^(all|\d+(-\d+)?(,\d+(-\d+)?)*)$",
          "description": "'all' or 1-based pages/ranges, e.g. '1-3,5'"}

TOOLS: tuple[McpTool, ...] = (
    McpTool(
        "pdf_merge", "pdf-merge", "pdf_edit", "檔案合併",
        "Merge 2-20 PDFs into one PDF, in the given order.",
        "/tools/pdf-merge/api/pdf-merge", "pdf", 2, 20, file_field="files",
        output="file", output_desc="One merged PDF saved to the workspace.",
        example={"tool": "pdf_merge", "files": ["<file_id_1>", "<file_id_2>"]}),
    McpTool(
        "pdf_split", "pdf-split", "pdf_edit", "頁面分拆",
        "Split a PDF into one file per page, or by page ranges.",
        "/tools/pdf-split/api/pdf-split", "pdf", 1, 1,
        params={"mode": {"type": "string", "enum": ["each", "ranges"], "default": "each",
                         "description": "'each' = one PDF per page; 'ranges' = one PDF per range"},
                "ranges": {"type": "string", "maxLength": 200, "pattern": r"^(\d+(-\d+)?(,\d+(-\d+)?)*)?$",
                           "default": "", "description": "With mode=ranges, e.g. '1-3,4-6,7'"}},
        output="files", output_desc="One PDF per part saved to the workspace.",
        example={"tool": "pdf_split", "files": ["<file_id>"], "params": {"mode": "ranges", "ranges": "1-2,3-5"}}),
    McpTool(
        "pdf_compress", "pdf-compress", "pdf_edit", "PDF 壓縮",
        "Reduce PDF file size by recompressing images.",
        "/tools/pdf-compress/api/pdf-compress", "pdf", 1, 1,
        params={"preset": {"type": "string", "enum": ["gentle", "balanced", "aggressive"], "default": "balanced",
                           "description": "gentle = best quality, aggressive = smallest"}},
        output="file", output_desc="The compressed PDF saved to the workspace.",
        example={"tool": "pdf_compress", "files": ["<file_id>"], "params": {"preset": "balanced"}}),
    McpTool(
        "pdf_rotate", "pdf-rotate", "pdf_edit", "頁面轉向",
        "Rotate all or selected pages of a PDF clockwise.",
        "/tools/pdf-rotate/api/pdf-rotate", "pdf", 1, 1,
        params={"angle": {"type": "integer", "enum": [90, 180, 270], "default": 90,
                          "description": "Clockwise degrees"},
                "pages": dict(_PAGES, default="all")},
        output="file", output_desc="The rotated PDF saved to the workspace.",
        example={"tool": "pdf_rotate", "files": ["<file_id>"], "params": {"angle": 90, "pages": "2,4"}}),
    McpTool(
        "pdf_page_numbers", "pdf-pageno", "pdf_edit", "插入頁碼",
        "Add page numbers to every page of a PDF.",
        "/tools/pdf-pageno/api/pdf-pageno", "pdf", 1, 1,
        params={"position": {"type": "string", "enum": ["tl", "tc", "tr", "bl", "bc", "br"], "default": "br",
                             "description": "t/b = top/bottom, l/c/r = left/center/right"},
                "fmt": {"type": "string", "maxLength": 40, "default": "{n} / {N}",
                        "description": "{n} = page number, {N} = total pages"},
                "start": {"type": "integer", "minimum": 1, "maximum": 99999, "default": 1},
                "font_size": {"type": "number", "minimum": 6, "maximum": 48, "default": 11},
                "margin_mm": {"type": "number", "minimum": 0, "maximum": 50, "default": 10},
                "color": {"type": "string", "pattern": r"^#[0-9a-fA-F]{6}$", "default": "#000000"}},
        output="file", output_desc="The numbered PDF saved to the workspace.",
        example={"tool": "pdf_page_numbers", "files": ["<file_id>"], "params": {"position": "bc", "fmt": "- {n} -"}}),
    McpTool(
        "office_to_pdf", "office-to-pdf", "convert", "辦公文件轉 PDF",
        "Convert an Office / ODF document (docx, xlsx, pptx, odt, ods, odp...) to PDF.",
        "/api/convert-to-pdf", "office", 1, 1,
        output="file", output_desc="The PDF saved to the workspace.",
        example={"tool": "office_to_pdf", "files": ["<file_id>"]}),
    McpTool(
        "document_to_images", "pdf-to-image", "convert", "辦公文件轉圖片",
        "Render each page of a PDF or Office document as a PNG or JPEG image.",
        "/tools/pdf-to-image/convert", "pdf_or_office", 1, 1,
        params={"format": {"type": "string", "enum": ["png", "jpeg"], "default": "png"},
                "dpi": {"type": "integer", "minimum": 72, "maximum": 600, "default": 200,
                        "description": "Ignored when width is given"},
                "width": {"type": "integer", "minimum": 16, "maximum": 10000,
                          "description": "Optional fixed width in pixels for every page"},
                "quality": {"type": "integer", "minimum": 1, "maximum": 100, "default": 80,
                            "description": "JPEG quality"}},
        output="image_two_step", output_desc="One image per page saved to the workspace.",
        example={"tool": "document_to_images", "files": ["<file_id>"], "params": {"format": "png", "dpi": 150}}),
    McpTool(
        "pdf_extract_text", "pdf-extract-text", "extract", "擷取文字",
        "Extract the text of a PDF page by page (no AI).",
        "/tools/pdf-extract-text/api/pdf-extract-text", "pdf", 1, 1,
        output="text",
        output_desc="The text is saved to the workspace as .txt; the first part is returned, "
                    "read the rest with workspace_read_text.",
        example={"tool": "pdf_extract_text", "files": ["<file_id>"]}),
    McpTool(
        "document_word_count", "pdf-wordcount", "extract", "字數統計",
        "Count pages, characters and words of a PDF, Office document or text file (no AI).",
        "/tools/pdf-wordcount/api/pdf-wordcount", "doc_or_text", 1, 1,
        output="json", output_desc="Statistics as JSON.",
        example={"tool": "document_word_count", "files": ["<file_id>"]}),
    McpTool(
        "vat_lookup", "vat-lookup", "lookup", "統編查詢",
        "Look up a Taiwan company / organisation by its 8-digit unified business number (UBN).",
        "/tools/vat-lookup/api/vat-lookup", None, 0, 0, body="json",
        params={"vat": {"type": "string", "pattern": r"^\d{8}$", "description": "8 digits"}},
        required=("vat",), output="json", output_desc="Company name, address, status, industries...",
        example={"tool": "vat_lookup", "params": {"vat": "12345678"}}),
)

NAMES = tuple(t.name for t in TOOLS)
BY_NAME = {t.name: t for t in TOOLS}
CATEGORIES = {"convert": "Format conversion", "pdf_edit": "PDF editing", "extract": "Text and statistics",
              "lookup": "Lookups"}

#: 每一支本站工具在 MCP 的歸類。新工具一定要在這裡決定（測試會檢查），不可以預設開放。
#: open＝已在 TOOLS；later＝不用 LLM、之後開放；llm_optional＝有 LLM 選項，開放時要強制關掉；
#: excluded＝不開放（LLM 是核心、操作設備、互動式、管理功能）；sensitive＝有法律效果，另有規則（用印）。
CLASSIFIED: dict[str, str] = {
    **{t.tool_id: "open" for t in TOOLS},
    "office-convert": "later", "image-to-pdf": "later", "pdf-to-office": "later", "pdf-to-markdown": "later",
    "markdown-to-doc": "later", "pdf-to-slides": "later", "pdf-pages": "later", "pdf-border": "later",
    "pdf-bookmark": "later", "pdf-nup": "later", "pdf-page-size": "later", "pdf-watermark": "later",
    "doc-straighten": "later", "scan-merge": "later", "pdf-extract-images": "later", "pdf-attachments": "later",
    "pdf-metadata": "later", "pdf-hidden-scan": "later", "pdf-annotations-strip": "later",
    "pdf-annotations-flatten": "later", "pdf-encrypt": "later", "pdf-decrypt": "later", "text-diff": "later",
    "text-list": "later", "transit-proof": "later",
    "pdf-ocr": "llm_optional", "doc-diff": "llm_optional", "text-deident": "llm_optional",
    "doc-deident": "llm_optional", "pdf-annotations": "llm_optional", "submission-check": "llm_optional",
    "einvoice-scan": "llm_optional",
    "official-doc": "excluded", "translate-doc": "excluded", "doc-translate": "excluded",
    "meeting-summary": "excluded", "meeting-transcribe": "excluded", "mfp-fetch": "excluded",
    "pdf-editor": "excluded", "pdf-fill": "excluded",
    "pdf-stamp": "sensitive", "pdf-seam-stamp": "sensitive",
}


# --------------------------------------------------------------------------- 參數檢查

class ParamError(ValueError):
    pass


def validate(tool: McpTool, params: Any) -> dict:
    """照登記表檢查參數，補上預設值；不認得的參數一律拒絕。回乾淨的參數（只有登記過的欄位）。"""
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ParamError("params must be an object")
    unknown = sorted(set(params) - set(tool.params))
    if unknown:
        allowed = ", ".join(sorted(tool.params)) or "(none)"
        raise ParamError(f"unknown parameter(s): {', '.join(unknown)}; allowed: {allowed}")
    out: dict = {}
    for key, spec in tool.params.items():
        if key not in params or params[key] is None:
            if key in tool.required:
                raise ParamError(f"missing required parameter: {key}")
            if "default" in spec:
                out[key] = spec["default"]
            continue
        out[key] = _check(key, spec, params[key])
    return out


def _check(key: str, spec: dict, v: Any) -> Any:
    t = spec.get("type")
    if t == "integer":
        if isinstance(v, bool) or not isinstance(v, int):
            if isinstance(v, str) and re.fullmatch(r"-?\d{1,9}", v.strip()):
                v = int(v)
            elif isinstance(v, float) and v.is_integer():
                v = int(v)
            else:
                raise ParamError(f"{key} must be an integer")
    elif t == "number":
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            try:
                v = float(str(v))
            except ValueError:
                raise ParamError(f"{key} must be a number") from None
    elif t == "string":
        if not isinstance(v, str):
            raise ParamError(f"{key} must be a string")
        if len(v) > spec.get("maxLength", 1000):
            raise ParamError(f"{key} is longer than {spec.get('maxLength', 1000)} characters")
        if "\x00" in v:
            raise ParamError(f"{key} contains a NUL character")
        if spec.get("pattern") and not re.fullmatch(spec["pattern"], v):
            raise ParamError(f"{key} has an invalid format ({spec.get('description', spec['pattern'])})")
    if "enum" in spec and v not in spec["enum"]:
        raise ParamError(f"{key} must be one of: {', '.join(map(str, spec['enum']))}")
    if "minimum" in spec and v < spec["minimum"]:
        raise ParamError(f"{key} must be >= {spec['minimum']}")
    if "maximum" in spec and v > spec["maximum"]:
        raise ParamError(f"{key} must be <= {spec['maximum']}")
    return v


def describe(tool: McpTool) -> dict:
    """`jtdt_describe` 回的完整說明（只在模型需要時才給，不放進 tools/list）。"""
    files = None
    if tool.inputs:
        n = (f"exactly {tool.min_files}" if tool.min_files == tool.max_files
             else f"{tool.min_files} to {tool.max_files}")
        files = {"count": n, "kind": KIND_TEXT[tool.inputs], "extensions": list(KINDS[tool.inputs]),
                 "how": "Workspace file ids (from workspace_list, workspace_upload_url or a previous result)."}
    return {
        "tool": tool.name, "title": tool.title, "category": tool.category, "summary": tool.summary,
        "files": files,
        "params": {"type": "object", "properties": tool.params, "required": list(tool.required),
                   "additionalProperties": False},
        "output": tool.output_desc, "example": tool.example,
    }
