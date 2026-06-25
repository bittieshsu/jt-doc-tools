"""pdf-attachments「產生無附件副本」測試。

重點回歸：PDF/A-3 / Factur-X（發票型）把附件同時掛在 catalog 的 /AF
(Associated Files)，舊版只 embfile_del 名稱樹、沒清 /AF，副本在 PDF 檢視器
中仍顯示附件（v1.12.12 修）。
"""
import fitz


def _pdf_with_embedded_and_af() -> bytes:
    """造一份 3 個嵌入附件、且同時掛在 catalog /AF 的 PDF。"""
    doc = fitz.open()
    doc.new_page()
    for nm, data in [("invoice.csv", b"a,b\n1,2"),
                     ("report.xml", b"<r/>"),
                     ("config.json", b"{}")]:
        doc.embfile_add(nm, data)
    # 把 filespec xref 掛到 catalog /AF
    af = [x for x in range(1, doc.xref_length())
          if doc.xref_get_key(x, "Type")[1] == "/Filespec"]
    cat = doc.pdf_catalog()
    doc.xref_set_key(cat, "AF", "[" + " ".join(f"{x} 0 R" for x in af) + "]")
    out = doc.tobytes(garbage=4, deflate=True)
    doc.close()
    return out


def _catalog_has_live_af(pdf_bytes: bytes) -> bool:
    d = fitz.open("pdf", pdf_bytes)
    try:
        cat_obj = d.xref_object(d.pdf_catalog())
        if "/AF" not in cat_obj:
            return False
        # /AF 後第一個 token 是 null 視為已移除
        after = cat_obj.split("/AF", 1)[1].lstrip()
        return not after.startswith("null")
    finally:
        d.close()


def test_strip_removes_embedded_and_af(client, auth_off):
    pdf = _pdf_with_embedded_and_af()
    r = client.post(
        "/tools/pdf-attachments/scan",
        files={"file": ("att.pdf", pdf, "application/pdf")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    uid = body["upload_id"]
    assert len(body["attachments"]) == 3

    r2 = client.post("/tools/pdf-attachments/strip", json={"upload_id": uid})
    assert r2.status_code == 200, r2.text
    assert r2.json()["removed"] == 3

    r3 = client.get(f"/tools/pdf-attachments/stripped/{uid}")
    assert r3.status_code == 200
    stripped = r3.content
    assert stripped[:4] == b"%PDF"

    # 副本：名稱樹清空 + catalog 無有效 /AF
    d = fitz.open("pdf", stripped)
    try:
        assert d.embfile_names() == []
        fs = sum(1 for x in range(1, d.xref_length())
                 if d.xref_get_key(x, "Type")[1] == "/Filespec")
        assert fs == 0, f"還有 {fs} 個 Filespec 物件殘留"
    finally:
        d.close()
    assert not _catalog_has_live_af(stripped), "catalog 仍有有效 /AF"


def test_strip_plain_embedded_still_works(client, auth_off):
    """沒有 /AF、只有一般 embfile_add 的 PDF 也要正常清掉。"""
    doc = fitz.open()
    doc.new_page()
    doc.embfile_add("a.txt", b"x")
    pdf = doc.tobytes(garbage=4, deflate=True)
    doc.close()

    uid = client.post(
        "/tools/pdf-attachments/scan",
        files={"file": ("a.pdf", pdf, "application/pdf")},
    ).json()["upload_id"]
    assert client.post("/tools/pdf-attachments/strip",
                       json={"upload_id": uid}).json()["removed"] == 1
    out = client.get(f"/tools/pdf-attachments/stripped/{uid}").content
    d = fitz.open("pdf", out)
    try:
        assert d.embfile_names() == []
    finally:
        d.close()
