"""KYOCERA / RICOH 送出去的每一種 SOAP 請求，結構要跟研究文件附的請求範本一致（v1.16.95）。

範本是分析者從原廠元件整理出來的（內部文件，不公開；**不是實機擷取**）。這裡把 22 種範本的**結構**
（元素路徑、命名空間、xsi:type、陣列型別）寫成固定資料，再讓 `mfp_soap` 對假設備實際跑一輪，
把它送出去的請求逐種比對：欄位名稱、順序、巢狀、型別錯一個都會紅。值不比（範本裡都是佔位字）。

已知、刻意的差異只有一個：`getDocContentUrl` 的 `schemePreference` 我們依序列兩個協定（POST、GET），
範本只示範一個 —— 它本來就是陣列，比對時相鄰重複的 item 合併成一列。
"""
from __future__ import annotations

from defusedxml.ElementTree import fromstring

from app.core import mfp_soap as K
from tests.test_mfp_soap import FakeKyocera, FakeRicoh, _kyo_session, _ricoh_session, kyo, ricoh  # noqa: F401

XSI = "{http://www.w3.org/2001/XMLSchema-instance}type"
ARR = "{http://schemas.xmlsoap.org/soap/encoding/}arrayType"
ALIAS = {K.ENV12: "env12", K.ENV11: "env11", K.WSA: "wsa", K.WSSE: "wsse", K.KYO_NS["auth"]: "kauth",
         K.KYO_NS["box"]: "kbox", K.KYO_NS["data"]: "kdata", K.RICOH_REPO_NS: "rrepo", K.RICOH_AUTH3_NS: "rauth3", "": ""}

#: 研究文件附的 22 種請求範本（內部文件，不公開）的結構：元素路徑｜命名空間｜xsi:type｜陣列型別。
#: 由範本產生、不是照我們的程式寫的；相鄰重複的列（陣列的 item）合併成一列。
TEMPLATES = {
 "get_authentication_statusRequest": [
  "/Envelope|env12||",
  "/Envelope/Header|env12||",
  "/Envelope/Header/Action|wsa||",
  "/Envelope/Body|env12||",
  "/Envelope/Body/get_authentication_statusRequest|kauth||"
 ],
 "create_box_enumerationRequest": [
  "/Envelope|env12||",
  "/Envelope/Header|env12||",
  "/Envelope/Header/Action|wsa||",
  "/Envelope/Header/Security|wsse||",
  "/Envelope/Header/Security/UsernameToken|wsse||",
  "/Envelope/Header/Security/UsernameToken/Username|wsse||",
  "/Envelope/Body|env12||",
  "/Envelope/Body/create_box_enumerationRequest|kbox||",
  "/Envelope/Body/create_box_enumerationRequest/type|kbox||",
  "/Envelope/Body/create_box_enumerationRequest/range|kbox||"
 ],
 "destroy_box_enumerationRequest": [
  "/Envelope|env12||",
  "/Envelope/Header|env12||",
  "/Envelope/Header/Action|wsa||",
  "/Envelope/Header/Security|wsse||",
  "/Envelope/Header/Security/UsernameToken|wsse||",
  "/Envelope/Header/Security/UsernameToken/Username|wsse||",
  "/Envelope/Body|env12||",
  "/Envelope/Body/destroy_box_enumerationRequest|kbox||",
  "/Envelope/Body/destroy_box_enumerationRequest/enumeration|kbox||"
 ],
 "destroy_document_enumerationRequest": [
  "/Envelope|env12||",
  "/Envelope/Header|env12||",
  "/Envelope/Header/Action|wsa||",
  "/Envelope/Header/Security|wsse||",
  "/Envelope/Header/Security/UsernameToken|wsse||",
  "/Envelope/Header/Security/UsernameToken/Username|wsse||",
  "/Envelope/Body|env12||",
  "/Envelope/Body/destroy_document_enumerationRequest|kbox||",
  "/Envelope/Body/destroy_document_enumerationRequest/enumeration|kbox||"
 ],
 "get_box_informationRequest": [
  "/Envelope|env12||",
  "/Envelope/Header|env12||",
  "/Envelope/Header/Action|wsa||",
  "/Envelope/Header/Security|wsse||",
  "/Envelope/Header/Security/UsernameToken|wsse||",
  "/Envelope/Header/Security/UsernameToken/Username|wsse||",
  "/Envelope/Body|env12||",
  "/Envelope/Body/get_box_informationRequest|kbox||",
  "/Envelope/Body/get_box_informationRequest/enumeration|kbox||"
 ],
 "get_document_informationRequest": [
  "/Envelope|env12||",
  "/Envelope/Header|env12||",
  "/Envelope/Header/Action|wsa||",
  "/Envelope/Header/Security|wsse||",
  "/Envelope/Header/Security/UsernameToken|wsse||",
  "/Envelope/Header/Security/UsernameToken/Username|wsse||",
  "/Envelope/Body|env12||",
  "/Envelope/Body/get_document_informationRequest|kbox||",
  "/Envelope/Body/get_document_informationRequest/enumeration|kbox||"
 ],
 "create_document_enumerationRequest": [
  "/Envelope|env12||",
  "/Envelope/Header|env12||",
  "/Envelope/Header/Action|wsa||",
  "/Envelope/Header/Security|wsse||",
  "/Envelope/Header/Security/UsernameToken|wsse||",
  "/Envelope/Header/Security/UsernameToken/Username|wsse||",
  "/Envelope/Body|env12||",
  "/Envelope/Body/create_document_enumerationRequest|kbox||",
  "/Envelope/Body/create_document_enumerationRequest/identification_information|kbox||",
  "/Envelope/Body/create_document_enumerationRequest/identification_information/type|kbox||",
  "/Envelope/Body/create_document_enumerationRequest/identification_information/number|kbox||",
  "/Envelope/Body/create_document_enumerationRequest/identification_information/password|kbox||",
  "/Envelope/Body/create_document_enumerationRequest/range|kbox||"
 ],
 "login_systemRequest": [
  "/Envelope|env12||",
  "/Envelope/Header|env12||",
  "/Envelope/Header/Action|wsa||",
  "/Envelope/Body|env12||",
  "/Envelope/Body/login_systemRequest|kauth||",
  "/Envelope/Body/login_systemRequest/userid|kauth||",
  "/Envelope/Body/login_systemRequest/password|kauth||",
  "/Envelope/Body/login_systemRequest/authentication_method|kauth||"
 ],
 "logout_systemRequest": [
  "/Envelope|env12||",
  "/Envelope/Header|env12||",
  "/Envelope/Header/Action|wsa||",
  "/Envelope/Header/Security|wsse||",
  "/Envelope/Header/Security/UsernameToken|wsse||",
  "/Envelope/Header/Security/UsernameToken/Username|wsse||",
  "/Envelope/Body|env12||",
  "/Envelope/Body/logout_systemRequest|kauth||",
  "/Envelope/Body/logout_systemRequest/usertoken|kauth||",
  "/Envelope/Body/logout_systemRequest/usertoken/encode|kauth||",
  "/Envelope/Body/logout_systemRequest/usertoken/token|kauth||"
 ],
 "prepare_downloading_dataRequest": [
  "/Envelope|env12||",
  "/Envelope/Header|env12||",
  "/Envelope/Header/Action|wsa||",
  "/Envelope/Header/Security|wsse||",
  "/Envelope/Header/Security/UsernameToken|wsse||",
  "/Envelope/Header/Security/UsernameToken/Username|wsse||",
  "/Envelope/Body|env12||",
  "/Envelope/Body/prepare_downloading_dataRequest|kdata||",
  "/Envelope/Body/prepare_downloading_dataRequest/identification_information|kdata||",
  "/Envelope/Body/prepare_downloading_dataRequest/identification_information/type|kdata||",
  "/Envelope/Body/prepare_downloading_dataRequest/identification_information/indicator|kdata||",
  "/Envelope/Body/prepare_downloading_dataRequest/identification_information/indicator/box_number|kdata||",
  "/Envelope/Body/prepare_downloading_dataRequest/identification_information/password|kdata||",
  "/Envelope/Body/prepare_downloading_dataRequest/file_name|kdata||",
  "/Envelope/Body/prepare_downloading_dataRequest/type|kdata||",
  "/Envelope/Body/prepare_downloading_dataRequest/color_selection|kdata||",
  "/Envelope/Body/prepare_downloading_dataRequest/file_separation|kdata||"
 ],
 "start_downloading_dataRequest": [
  "/Envelope|env12||",
  "/Envelope/Header|env12||",
  "/Envelope/Header/Action|wsa||",
  "/Envelope/Header/Security|wsse||",
  "/Envelope/Header/Security/UsernameToken|wsse||",
  "/Envelope/Header/Security/UsernameToken/Username|wsse||",
  "/Envelope/Body|env12||",
  "/Envelope/Body/start_downloading_dataRequest|kdata||",
  "/Envelope/Body/start_downloading_dataRequest/operation_id|kdata||"
 ],
 "authenticateByPassword": [
  "/Envelope|env11||",
  "/Envelope/Body|env11||",
  "/Envelope/Body/authenticateByPassword|rauth3||",
  "/Envelope/Body/authenticateByPassword/providerName||xsd:string|",
  "/Envelope/Body/authenticateByPassword/domainName||xsd:string|",
  "/Envelope/Body/authenticateByPassword/authName||xsd:string|",
  "/Envelope/Body/authenticateByPassword/password||xsd:string|",
  "/Envelope/Body/authenticateByPassword/duration||xsd:int|",
  "/Envelope/Body/authenticateByPassword/options||enc:Array|property[0]"
 ],
 "getServiceCapability": [
  "/Envelope|env11||",
  "/Envelope/Body|env11||",
  "/Envelope/Body/getServiceCapability|rrepo||",
  "/Envelope/Body/getServiceCapability/sessionId||xsd:string|"
 ],
 "checkObjectPassword": [
  "/Envelope|env11||",
  "/Envelope/Body|env11||",
  "/Envelope/Body/checkObjectPassword|rrepo||",
  "/Envelope/Body/checkObjectPassword/sessionId||xsd:string|",
  "/Envelope/Body/checkObjectPassword/objectId||xsd:string|",
  "/Envelope/Body/checkObjectPassword/password||xsd:string|"
 ],
 "getDocContentUrl": [
  "/Envelope|env11||",
  "/Envelope/Body|env11||",
  "/Envelope/Body/getDocContentUrl|rrepo||",
  "/Envelope/Body/getDocContentUrl/sessionId||xsd:string|",
  "/Envelope/Body/getDocContentUrl/docId||xsd:string|",
  "/Envelope/Body/getDocContentUrl/validDurationHint||xsd:unsignedInt|",
  "/Envelope/Body/getDocContentUrl/schemePreference||enc:Array|string[N]",
  "/Envelope/Body/getDocContentUrl/schemePreference/item||xsd:string|"
 ],
 "getDocElementList": [
  "/Envelope|env11||",
  "/Envelope/Body|env11||",
  "/Envelope/Body/getDocElementList|rrepo||",
  "/Envelope/Body/getDocElementList/sessionId||xsd:string|",
  "/Envelope/Body/getDocElementList/docId||xsd:string|",
  "/Envelope/Body/getDocElementList/startIndex||xsd:unsignedInt|",
  "/Envelope/Body/getDocElementList/maximumResultItems||xsd:unsignedInt|"
 ],
 "getProps": [
  "/Envelope|env11||",
  "/Envelope/Body|env11||",
  "/Envelope/Body/getProps|rrepo||",
  "/Envelope/Body/getProps/sessionId||xsd:string|",
  "/Envelope/Body/getProps/objectId||xsd:string|"
 ],
 "getDocRendition": [
  "/Envelope|env11||",
  "/Envelope/Body|env11||",
  "/Envelope/Body/getDocRendition|rrepo||",
  "/Envelope/Body/getDocRendition/sessionId||xsd:string|",
  "/Envelope/Body/getDocRendition/docId||xsd:string|",
  "/Envelope/Body/getDocRendition/renditionRequest||enc:Array|renditionPreference[N]",
  "/Envelope/Body/getDocRendition/renditionRequest/item||rt:renditionPreference|",
  "/Envelope/Body/getDocRendition/renditionRequest/item/name||xsd:string|",
  "/Envelope/Body/getDocRendition/renditionRequest/item/value||xsd:string|",
  "/Envelope/Body/getDocRendition/renditionRequest/item/preference||xsd:unsignedInt|",
  "/Envelope/Body/getDocRendition/renditionRequest/item/optional||xsd:boolean|"
 ],
 "executeAdvancedSearch": [
  "/Envelope|env11||",
  "/Envelope/Body|env11||",
  "/Envelope/Body/executeAdvancedSearch|rrepo||",
  "/Envelope/Body/executeAdvancedSearch/sessionId||xsd:string|",
  "/Envelope/Body/executeAdvancedSearch/rowOffset||xsd:unsignedInt|",
  "/Envelope/Body/executeAdvancedSearch/resultRowBlockSize||xsd:unsignedInt|",
  "/Envelope/Body/executeAdvancedSearch/resultSetId||xsd:string|",
  "/Envelope/Body/executeAdvancedSearch/fromFolderId||xsd:string|",
  "/Envelope/Body/executeAdvancedSearch/deepSearch||xsd:boolean|",
  "/Envelope/Body/executeAdvancedSearch/select||enc:Array|string[N]",
  "/Envelope/Body/executeAdvancedSearch/select/item||xsd:string|",
  "/Envelope/Body/executeAdvancedSearch/from||xsd:string|",
  "/Envelope/Body/executeAdvancedSearch/whereAnd||enc:Array|querySimpleOperator[0]",
  "/Envelope/Body/executeAdvancedSearch/whereOr||enc:Array|querySimpleOperator[0]",
  "/Envelope/Body/executeAdvancedSearch/orderBy||enc:Array|queryOrderBy[0]"
 ],
 "startSessionByAuthTicket": [
  "/Envelope|env11||",
  "/Envelope/Body|env11||",
  "/Envelope/Body/startSessionByAuthTicket|rrepo||",
  "/Envelope/Body/startSessionByAuthTicket/authTicket||xsd:base64Binary|",
  "/Envelope/Body/startSessionByAuthTicket/authenticationUrl||xsd:string|",
  "/Envelope/Body/startSessionByAuthTicket/timeLimit||xsd:unsignedInt|"
 ],
 "terminateSession": [
  "/Envelope|env11||",
  "/Envelope/Body|env11||",
  "/Envelope/Body/terminateSession|rrepo||",
  "/Envelope/Body/terminateSession/sessionId||xsd:string|"
 ],
 "getProtocolVersion": [
  "/Envelope|env11||",
  "/Envelope/Body|env11||",
  "/Envelope/Body/getProtocolVersion|rrepo||"
 ]
}

def _loc(t: str) -> str:
    return t.rsplit("}", 1)[-1]


def signature(xml: bytes) -> tuple[str, list[str]]:
    rows: list[str] = []
    method = ""

    def walk(el, path):
        nonlocal method
        p = path + "/" + _loc(el.tag)
        if p.count("/") == 3 and "/Body/" in p:
            method = _loc(el.tag)
        ns = el.tag[1:].split("}")[0] if el.tag.startswith("{") else ""
        a = el.get(ARR)
        if a:
            a = a.split(":")[-1].split("[")[0] + ("[0]" if a.endswith("[0]") else "[N]")
        r = "|".join([p, ALIAS.get(ns, ns), el.get(XSI) or "", a or ""])
        if not rows or rows[-1] != r:
            rows.append(r)
        for k in el:
            walk(k, p)

    walk(fromstring(xml), "")
    return method, rows


def _sent(kyo, ricoh, tmp_path) -> dict[str, list[str]]:
    s = _kyo_session(kyo, [])
    s.roots()
    s.download("1", [s.list("1", [])[0]["name"]], tmp_path / "k.bin", 10 * 1024 * 1024)
    s.close()
    s = _ricoh_session(ricoh, [], doc_password="9999")
    s.list("documents", [])
    s.download("documents", [K.enc_name("doc-1")], tmp_path / "r1.bin", 10 * 1024 * 1024)
    s.download("documents", [K.enc_name("doc-3")], tmp_path / "r3.bin", 10 * 1024 * 1024)
    s.close()
    out: dict[str, list[str]] = {}
    for c in kyo.calls + ricoh.calls:
        if c.get("body") and c.get("method") != "POSTDL":
            m, rows = signature(c["body"])
            out.setdefault(m, rows)
    return out


def test_every_request_matches_its_template(kyo, ricoh, tmp_path):
    sent = _sent(kyo, ricoh, tmp_path)
    assert set(sent) == set(TEMPLATES), {"沒送過的範本": sorted(set(TEMPLATES) - set(sent)),
                                         "範本沒有的請求": sorted(set(sent) - set(TEMPLATES))}
    bad = {m: {"範本": TEMPLATES[m], "我們": rows} for m, rows in sent.items() if rows != TEMPLATES[m]}
    assert not bad, bad


def test_the_comparison_has_teeth(kyo, ricoh, tmp_path, monkeypatch):
    """反向：欄位順序換掉（RICOH 的認證參數）就要比得出來。"""
    real = K.RicohSession._envelope

    def swapped(self, ns, method, params):
        if method == "authenticateByPassword":
            params = [params[1], params[0]] + params[2:]
        return real(self, ns, method, params)

    monkeypatch.setattr(K.RicohSession, "_envelope", swapped)
    sent = _sent(kyo, ricoh, tmp_path)          # 假設備照名稱讀參數，所以照樣跑得完
    assert sent["authenticateByPassword"] != TEMPLATES["authenticateByPassword"]


#: v1.17.4：RICOH 資料夾（研究文件 2026-10-20 附的請求範本，內部文件不公開）—— 列資料夾、列資料夾裡的掃描文件
#: 結構相同（`whereAnd` 一筆 querySimpleOperator，operator 不帶型別）、驗證資料夾密碼、批次取日期
_FOLDER_SEARCH = [
 "/Envelope|env11||",
 "/Envelope/Body|env11||",
 "/Envelope/Body/executeAdvancedSearch|rrepo||",
 "/Envelope/Body/executeAdvancedSearch/sessionId||xsd:string|",
 "/Envelope/Body/executeAdvancedSearch/rowOffset||xsd:unsignedInt|",
 "/Envelope/Body/executeAdvancedSearch/resultRowBlockSize||xsd:unsignedInt|",
 "/Envelope/Body/executeAdvancedSearch/resultSetId||xsd:string|",
 "/Envelope/Body/executeAdvancedSearch/fromFolderId||xsd:string|",
 "/Envelope/Body/executeAdvancedSearch/deepSearch||xsd:boolean|",
 "/Envelope/Body/executeAdvancedSearch/select||enc:Array|string[N]",
 "/Envelope/Body/executeAdvancedSearch/select/item||xsd:string|",
 "/Envelope/Body/executeAdvancedSearch/from||xsd:string|",
 "/Envelope/Body/executeAdvancedSearch/whereAnd||enc:Array|querySimpleOperator[N]",
 "/Envelope/Body/executeAdvancedSearch/whereAnd/item||rt:querySimpleOperator|",
 "/Envelope/Body/executeAdvancedSearch/whereAnd/item/operator|||",
 "/Envelope/Body/executeAdvancedSearch/whereAnd/item/propName||xsd:string|",
 "/Envelope/Body/executeAdvancedSearch/whereAnd/item/propVal||xsd:string|",
 "/Envelope/Body/executeAdvancedSearch/whereAnd/item/propVal2||xsd:string|",
 "/Envelope/Body/executeAdvancedSearch/whereOr||enc:Array|querySimpleOperator[0]",
 "/Envelope/Body/executeAdvancedSearch/orderBy||enc:Array|queryOrderBy[0]",
]
FOLDER_TEMPLATES = {
 "checkObjectPassword": [
  "/Envelope|env11||",
  "/Envelope/Body|env11||",
  "/Envelope/Body/checkObjectPassword|rrepo||",
  "/Envelope/Body/checkObjectPassword/sessionId||xsd:string|",
  "/Envelope/Body/checkObjectPassword/objectId||xsd:string|",
  "/Envelope/Body/checkObjectPassword/password||xsd:string|",
 ],
 "getObjectsProps": [
  "/Envelope|env11||",
  "/Envelope/Body|env11||",
  "/Envelope/Body/getObjectsProps|rrepo||",
  "/Envelope/Body/getObjectsProps/sessionId||xsd:string|",
  "/Envelope/Body/getObjectsProps/objectIds||enc:Array|string[N]",
  "/Envelope/Body/getObjectsProps/objectIds/item||xsd:string|",
  "/Envelope/Body/getObjectsProps/select||enc:Array|string[N]",
  "/Envelope/Body/getObjectsProps/select/item||xsd:string|",
  "/Envelope/Body/getObjectsProps/options||enc:Array|property[0]",
 ],
}


def test_ricoh_folder_requests_match_their_templates(ricoh):
    from tests.test_mfp_soap import _with_folders
    _with_folders(ricoh)
    s = _ricoh_session(ricoh, [], user="", password="", auth_mode="none", folder_password="001234")
    s.roots()
    s.list(K.enc_name("fid=1"), [])
    s.close()
    searches = [signature(c["body"])[1] for c in ricoh.calls if c["method"] == "executeAdvancedSearch"]
    folder_kind = [c for c in ricoh.calls if c["method"] == "executeAdvancedSearch" and c["params"]["from"] == "folder"]
    in_folder = [c for c in ricoh.calls if c["method"] == "executeAdvancedSearch" and c["params"]["fromFolderId"] == "fid=1"]
    assert folder_kind and in_folder
    for c in folder_kind + in_folder:
        assert signature(c["body"])[1] == _FOLDER_SEARCH
    for m, want in FOLDER_TEMPLATES.items():
        got = [signature(c["body"])[1] for c in ricoh.calls if c["method"] == m]
        assert got and all(g == want for g in got), (m, got[:1])
    assert searches
