"""FUJIFILM request profiles based on NSU3 2.0.60 compatibility research.

Tool-informed request profiles; NOT a public API or hardware compatibility claim.
The selected list properties and bounded negotiation are JTDT integration choices.
"""
from __future__ import annotations

PROFILES = {
    "modern": {
        "sss": "http://www.fujifilm.com/fb/2021/04/ssm/storageService",
        "jt": "http://www.fujifilm.com/fb/2021/04/ssm/jobTemplate",
        "br": "http://www.fujifilm.com/fb/2021/04/backupService",
        "pb": "http://www.fujifilm.com/fb/2021/04/boxAccess",
    },
    "legacy": {
        "sss": "http://www.fujixerox.co.jp/2003/12/ssm/storageService",
        "jt": "http://www.fujixerox.co.jp/2003/12/ssm/jobTemplate",
        "br": "http://www.fujixerox.co.jp/2004/09/backupService",
        "pb": "http://www.fujixerox.co.jp/2005/06/boxAccess",
    },
}
# Chosen for JTDT, from DAV and the studied vendor property names. This is not
# claimed to be the exact property subset of every NSU3 GUI operation.
LIST_PROPERTIES = (
    "d:resourcetype", "d:displayname", "d:creationdate", "d:getlastmodified",
    "d:getcontenttype", "d:getcontentlength", "sss:Pages", "sss:Compression",
    "sss:ColorMode", "sss:DocStatusCode", "br:numofresources",
    "br:totalimagesize", "pb:deleteAfterGet",
)


def request_spec(profile: str, stage: str) -> tuple[bytes, str, dict[str, str]]:
    namespaces = PROFILES[profile]
    if stage not in ("probe", "list"):
        raise ValueError("invalid FUJIFILM request stage")
    properties = ("d:resourcetype",) if stage == "probe" else LIST_PROPERTIES
    attrs = 'xmlns:d="DAV:" ' + " ".join(
        f'xmlns:{prefix}="{uri}"' for prefix, uri in namespaces.items())
    body = ("<?xml version='1.0' encoding='utf-8'?>"
            f"<d:propfind {attrs}><d:prop>"
            + "".join(f"<{name}/>" for name in properties)
            + "</d:prop></d:propfind>").encode("utf-8")
    # List requests omit this header; initial authentication probes use "On".
    extra = {"X-Singlepage": "On"} if stage == "probe" else {}
    return body, "text/xml", extra


def response_profile(qnames: list[str], current: str) -> str:
    """Use actual property QNames, not substring matches in document names.

    DAV-only/ambiguous responses do not prove legacy firmware: keep current.
    This deliberately narrows NSU3's absence-of-modern-namespace heuristic.
    """
    observed = {q[1:].split("}", 1)[0] for q in qnames if q.startswith("{")}
    candidates = [name for name, ns in PROFILES.items() if observed & set(ns.values())]
    return candidates[0] if len(candidates) == 1 else current
