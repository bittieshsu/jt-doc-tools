"""把「分段轉出的多個 .odt / .docx」合併回單一檔案（純 Python，不呼叫 soffice）。

為什麼需要分段再合併
--------------------
soffice 的匯出耗時隨文件內物件數 **超線性（約 O(n²)）** 成長。實測某 152 頁年報用
版面重現引擎整份轉 .docx：18,000+ 個物件 → 超過 15 分鐘仍未完成；同一份切成小段後
每段只要 10~90 秒。大型 PDF 甚至連 soffice 的 PDF→Draw 匯入都會逾時。

但**使用者要的是一份文件，不是一包分段檔**，所以分段只能是內部實作細節：轉完之後
一定要合併回單一 .odt / .docx。合併在 Python 端做（純 XML 拼接），不再經過 soffice，
因此不會踩到那條 O(n²) 曲線。

合併時要處理的衝突
------------------
* **樣式名稱**：各段是獨立文件，自動樣式名（gr1 / P1 / JtPg1 …）會重複但定義不同
  → 逐段加前綴後再合併，並同步改所有引用處。
* **分節**：OOXML 的「最後一節」屬性放在 ``w:body`` 尾端、其餘節放在該節最後一個
  段落的 ``w:pPr/w:sectPr``。串接時要把前面各段的 body 層 sectPr 降級成段落層，
  否則後段內容會併進前段的節、頁面尺寸跟著錯亂。
* **圖片**：檔名與關聯 ID（rId）逐段重複 → 一併改名。
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

from lxml import etree

log = logging.getLogger(__name__)

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT = "http://schemas.openxmlformats.org/package/2006/content-types"

_ODF_NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "xlink": "http://www.w3.org/1999/xlink",
    "manifest": "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0",
}


def _parser() -> etree.XMLParser:
    """安全解析器（關外部實體 / 網路，防 XXE）。lxml parser 非執行緒安全 → 每次新建。"""
    return etree.XMLParser(resolve_entities=False, no_network=True,
                           huge_tree=False, load_dtd=False)


def _q(ns: str, local: str) -> str:
    return "{%s}%s" % (ns, local)


def _odf(prefix: str, local: str) -> str:
    return "{%s}%s" % (_ODF_NS[prefix], local)


def _atomic_write_zip(out_path: Path, entries: list[tuple[str, bytes]],
                      stored_first: str | None = None) -> None:
    """原子寫出 zip（中途失敗不留半截檔）。stored_first 指定要以 STORED 存放的首檔
    （ODF 規定 mimetype 必須是第一個且不壓縮，否則部分程式認不得檔案類型）。"""
    fd, tmp = tempfile.mkstemp(suffix=out_path.suffix, dir=str(out_path.parent))
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            if stored_first is not None:
                for name, data in entries:
                    if name == stored_first:
                        zf.writestr(name, data, zipfile.ZIP_STORED)
                        break
            for name, data in entries:
                if name == stored_first:
                    continue
                zf.writestr(name, data)
        shutil.move(tmp, out_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── .docx ───────────────────────────────────────────────────────────────────
def merge_docx(parts: list[Path], out_path: Path) -> int:
    """把多個 .docx 依序合併成一個。回合併的段落 / 區塊數。

    做法：以第一段為基底，其餘各段的 ``w:body`` 內容接在後面。前面各段 body 層的
    ``w:sectPr`` 降級成該段最後一個段落的段落層 sectPr（見模組說明），只保留最後
    一段的 body 層 sectPr。樣式 / 字型表 / 圖片依名稱聯集，重名者逐段加前綴。
    """
    parts = [Path(p) for p in parts]
    if not parts:
        raise ValueError("沒有要合併的檔案")
    if len(parts) == 1:
        shutil.copyfile(parts[0], out_path)
        return 0

    base_zip = zipfile.ZipFile(parts[0])
    files: dict[str, bytes] = {n: base_zip.read(n) for n in base_zip.namelist()}
    doc = etree.fromstring(files["word/document.xml"], _parser())
    body = doc.find(_q(_W, "body"))
    if body is None:
        raise ValueError("word/document.xml 缺少 w:body")

    rels = etree.fromstring(files["word/_rels/document.xml.rels"], _parser())
    styles = etree.fromstring(files.get("word/styles.xml", b"<x/>"), _parser())
    known_styles = {s.get(_q(_W, "styleId"))
                    for s in styles.iter(_q(_W, "style"))}
    fonts_xml = files.get("word/fontTable.xml")
    fonts = etree.fromstring(fonts_xml, _parser()) if fonts_xml else None
    known_fonts = ({f.get(_q(_W, "name")) for f in fonts.iter(_q(_W, "font"))}
                   if fonts is not None else set())
    ct = etree.fromstring(files["[Content_Types].xml"], _parser())
    known_ext = {d.get("Extension", "").lower() for d in ct.iter(_q(_CT, "Default"))}

    n_blocks = len(body)
    for idx, part in enumerate(parts[1:], start=1):
        with zipfile.ZipFile(part) as z:
            pdoc = etree.fromstring(z.read("word/document.xml"), _parser())
            pbody = pdoc.find(_q(_W, "body"))
            if pbody is None:
                continue
            prels = etree.fromstring(z.read("word/_rels/document.xml.rels"),
                                     _parser())
            # 1) 圖片與關聯 ID 改名（各段的 rId1 / image1.png 會互撞）
            rid_map: dict[str, str] = {}
            for rel in prels:
                old_id = rel.get("Id")
                new_id = "jtdt%d%s" % (idx, old_id)
                rid_map[old_id] = new_id
                target = rel.get("Target") or ""
                if target.startswith("media/"):
                    new_target = "media/jtdt%d_%s" % (idx, target[len("media/"):])
                    src = "word/" + target
                    if src in z.namelist():
                        files["word/" + new_target] = z.read(src)
                        ext = new_target.rsplit(".", 1)[-1].lower()
                        if ext and ext not in known_ext:
                            d = etree.SubElement(ct, _q(_CT, "Default"))
                            d.set("Extension", ext)
                            d.set("ContentType", "image/" + ("jpeg" if ext in
                                                             ("jpg", "jpeg") else ext))
                            known_ext.add(ext)
                    rel.set("Target", new_target)
                rel.set("Id", new_id)
                rels.append(rel)
            if rid_map:
                _remap_rids(pbody, rid_map)

            # 2) 樣式 / 字型：只補基底沒有的（各段樣式定義相同，實測就是
            #    Normal / FrameContents 兩個，故用「同名視為同義」）
            try:
                pstyles = etree.fromstring(z.read("word/styles.xml"), _parser())
                for s in list(pstyles.iter(_q(_W, "style"))):
                    sid = s.get(_q(_W, "styleId"))
                    if sid and sid not in known_styles:
                        styles.append(s)
                        known_styles.add(sid)
            except KeyError:
                pass
            if fonts is not None:
                try:
                    pfonts = etree.fromstring(z.read("word/fontTable.xml"), _parser())
                    for f in list(pfonts.iter(_q(_W, "font"))):
                        nm = f.get(_q(_W, "name"))
                        if nm and nm not in known_fonts:
                            fonts.append(f)
                            known_fonts.add(nm)
                except KeyError:
                    pass

            # 3) 前一段 body 層的 sectPr 降級到「前一段最後一個段落」
            _demote_trailing_sectpr(body)
            for child in list(pbody):
                body.append(child)
                n_blocks += 1

    files["word/document.xml"] = etree.tostring(
        doc, xml_declaration=True, encoding="UTF-8", standalone=True)
    files["word/_rels/document.xml.rels"] = etree.tostring(
        rels, xml_declaration=True, encoding="UTF-8", standalone=True)
    if "word/styles.xml" in files:
        files["word/styles.xml"] = etree.tostring(
            styles, xml_declaration=True, encoding="UTF-8", standalone=True)
    if fonts is not None:
        files["word/fontTable.xml"] = etree.tostring(
            fonts, xml_declaration=True, encoding="UTF-8", standalone=True)
    files["[Content_Types].xml"] = etree.tostring(
        ct, xml_declaration=True, encoding="UTF-8", standalone=True)
    base_zip.close()
    _atomic_write_zip(out_path, list(files.items()))
    return n_blocks


def _remap_rids(tree, rid_map: dict[str, str]) -> None:
    """把子樹裡所有指向關聯 ID 的屬性換成新 ID（r:id / r:embed / r:link）。"""
    keys = (_q(_R, "id"), _q(_R, "embed"), _q(_R, "link"))
    for el in tree.iter():
        if not isinstance(el.tag, str):
            continue
        for k in keys:
            v = el.get(k)
            if v and v in rid_map:
                el.set(k, rid_map[v])


def _demote_trailing_sectpr(body) -> None:
    """把 body 尾端的 w:sectPr 移進「前一個段落」的 w:pPr（成為該節的終止符）。

    OOXML 的分節規則：最後一節的屬性放 body 尾端，其餘節放各節最後一個段落的
    pPr 內。串接文件時若不做這個降級，後面接上的內容會被併進前一節 → 頁面尺寸 /
    分頁位置全錯。
    """
    if len(body) == 0:
        return
    last = body[-1]
    if not isinstance(last.tag, str) or etree.QName(last).localname != "sectPr":
        return
    body.remove(last)
    # 往前找最後一個段落來掛
    for el in reversed(body):
        if isinstance(el.tag, str) and etree.QName(el).localname == "p":
            ppr = el.find(_q(_W, "pPr"))
            if ppr is None:
                ppr = etree.Element(_q(_W, "pPr"))
                el.insert(0, ppr)
            if ppr.find(_q(_W, "sectPr")) is None:
                ppr.append(last)
            return
    # 沒有段落可掛（理論上不會發生）→ 補一個空段落承載
    p = etree.SubElement(body, _q(_W, "p"))
    ppr = etree.SubElement(p, _q(_W, "pPr"))
    ppr.append(last)


# ── .odt ────────────────────────────────────────────────────────────────────
_ODT_STYLE_REFS = (
    ("draw", "style-name"), ("draw", "text-style-name"),
    ("text", "style-name"), ("text", "class-names"),
    ("style", "master-page-name"), ("style", "list-style-name"),
    ("style", "parent-style-name"),
)


def merge_odt(parts: list[Path], out_path: Path) -> int:
    """把多個 .odt 依序合併成一個。回合併的頂層區塊數。

    各段是獨立文件，自動樣式（JtPg1 / gr1 / P1 …）與頁面樣式（JtMP_p1 …）名稱會
    重複但定義不同 → 逐段加前綴後再合併，並同步改所有引用處；圖片路徑同理。
    """
    parts = [Path(p) for p in parts]
    if not parts:
        raise ValueError("沒有要合併的檔案")
    if len(parts) == 1:
        shutil.copyfile(parts[0], out_path)
        return 0

    files: dict[str, bytes] = {}
    with zipfile.ZipFile(parts[0]) as z0:
        for n in z0.namelist():
            files[n] = z0.read(n)
    content = etree.fromstring(files["content.xml"], _parser())
    styles = etree.fromstring(files["styles.xml"], _parser())
    manifest = etree.fromstring(files["META-INF/manifest.xml"], _parser())

    c_auto = content.find(_odf("office", "automatic-styles"))
    c_body = content.find(_odf("office", "body"))
    c_text = c_body.find(_odf("office", "text")) if c_body is not None else None
    s_auto = styles.find(_odf("office", "automatic-styles"))
    s_master = styles.find(_odf("office", "master-styles"))
    c_fonts = content.find(_odf("office", "font-face-decls"))
    known_fonts = ({f.get(_odf("style", "name"))
                    for f in c_fonts.findall(_odf("style", "font-face"))}
                   if c_fonts is not None else set())
    if c_text is None:
        raise ValueError("content.xml 缺少 office:text")

    n_blocks = len(c_text)
    for idx, part in enumerate(parts[1:], start=1):
        pre = "jt%d" % idx
        with zipfile.ZipFile(part) as z:
            pc = etree.fromstring(z.read("content.xml"), _parser())
            ps = etree.fromstring(z.read("styles.xml"), _parser())
            # 1) 圖片改名（各段都是 Pictures/xxx，會互撞）
            pic_map: dict[str, str] = {}
            for n in z.namelist():
                if n.startswith("Pictures/"):
                    new = "Pictures/%s_%s" % (pre, n[len("Pictures/"):])
                    pic_map[n] = new
                    files[new] = z.read(n)
            # 2) 樣式改名 + 引用同步
            _prefix_odf_styles(pc, pre)
            _prefix_odf_styles(ps, pre)
            _remap_odf_hrefs(pc, pic_map)
            # 3) 併進基底
            p_auto = pc.find(_odf("office", "automatic-styles"))
            if p_auto is not None and c_auto is not None:
                for st in list(p_auto):
                    c_auto.append(st)
            p_sauto = ps.find(_odf("office", "automatic-styles"))
            if p_sauto is not None and s_auto is not None:
                for st in list(p_sauto):
                    s_auto.append(st)
            p_master = ps.find(_odf("office", "master-styles"))
            if p_master is not None and s_master is not None:
                for m in list(p_master):
                    s_master.append(m)
            p_fonts = pc.find(_odf("office", "font-face-decls"))
            if p_fonts is not None and c_fonts is not None:
                for f in list(p_fonts.findall(_odf("style", "font-face"))):
                    nm = f.get(_odf("style", "name"))
                    if nm and nm not in known_fonts:
                        c_fonts.append(f)
                        known_fonts.add(nm)
            p_body = pc.find(_odf("office", "body"))
            p_text = p_body.find(_odf("office", "text")) if p_body is not None else None
            if p_text is not None:
                for child in list(p_text):
                    c_text.append(child)
                    n_blocks += 1
            # 4) manifest 補圖片項目
            for new in pic_map.values():
                e = etree.SubElement(manifest, _odf("manifest", "file-entry"))
                e.set(_odf("manifest", "full-path"), new)
                e.set(_odf("manifest", "media-type"),
                      "image/png" if new.lower().endswith(".png") else "image/jpeg")

    files["content.xml"] = etree.tostring(content, xml_declaration=True,
                                          encoding="UTF-8")
    files["styles.xml"] = etree.tostring(styles, xml_declaration=True,
                                         encoding="UTF-8")
    files["META-INF/manifest.xml"] = etree.tostring(manifest, xml_declaration=True,
                                                    encoding="UTF-8")
    _atomic_write_zip(out_path, list(files.items()), stored_first="mimetype")
    return n_blocks


def _prefix_odf_styles(root, prefix: str) -> None:
    """把樹內所有「樣式定義的名稱」與「引用該名稱之處」一起加前綴。"""
    renamed: set[str] = set()
    name_attr = _odf("style", "name")
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        ln = etree.QName(el).localname
        if ln in ("style", "page-layout", "master-page", "list-style",
                  "font-face"):
            if ln == "font-face":
                continue                       # 字型名是真實字型，不可改
            nm = el.get(name_attr)
            if nm:
                el.set(name_attr, prefix + nm)
                renamed.add(nm)
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        for ns, attr in _ODT_STYLE_REFS:
            k = _odf(ns, attr)
            v = el.get(k)
            if v and v in renamed:
                el.set(k, prefix + v)
        pl = el.get(_odf("style", "page-layout-name"))
        if pl and pl in renamed:
            el.set(_odf("style", "page-layout-name"), prefix + pl)


def _remap_odf_hrefs(root, pic_map: dict[str, str]) -> None:
    href = _odf("xlink", "href")
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        v = el.get(href)
        if not v:
            continue
        key = v[2:] if v.startswith("./") else v
        if key in pic_map:
            el.set(href, pic_map[key])
