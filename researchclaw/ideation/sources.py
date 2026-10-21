"""Real academic retrieval and bounded, public full-text fetching."""
from __future__ import annotations

import html
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from researchclaw.ideation.store import SessionError, fingerprint
from researchclaw.web._ssrf import check_url_ssrf


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        error = check_url_ssrf(newurl)
        if error:
            raise SessionError("论文链接重定向到非公共地址", 400)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, limit: int = 25 * 1024 * 1024) -> tuple[bytes, str, str]:
    if check_url_ssrf(url):
        raise SessionError("仅允许获取公共 HTTP/HTTPS 论文地址", 400)
    req = urllib.request.Request(url, headers={"User-Agent": "ResearchClaw/0.5 AcademicResearch"})
    with urllib.request.build_opener(PublicRedirect()).open(req, timeout=30) as response:
        data = response.read(limit + 1)
        if len(data) > limit:
            raise SessionError("论文文件超过25MB，请提供较小PDF", 400)
        return data, response.headers.get("Content-Type", ""), response.url


def paper_record(row: dict[str, Any], query: str = "", source: str = "") -> dict[str, Any]:
    title = str(row.get("title") or "").strip()
    doi = re.sub(r"^https?://(?:dx\.)?doi.org/", "", str(row.get("doi") or "")).lower()
    identity = doi or row.get("content_hash") or re.sub(r"\W", "", title.lower())
    if not identity:
        raise SessionError("未取得真实论文标题或DOI", 400)
    return {**row, "paper_id": "p-" + fingerprint(identity)[:20], "doi": doi, "title": title,
            "provenance": [{"query": query, "source": source or row.get("source"),
                            "retrieved_at": datetime.now(timezone.utc).isoformat()}],
            "fulltext_status": "not_fetched", "relevance": "unreviewed", "selected": False,
            "required": False}


class Sources:
    def __init__(self, config: Any):
        self.config = config

    def search(self, query: str, period: str = "all", source: str = "openalex") -> list[dict[str, Any]]:
        from researchclaw.literature.search import search_papers

        if source == "crossref":
            params = {"query.bibliographic": query, "rows": "8"}
            if period == "recent":
                params["filter"] = f"from-pub-date:{datetime.now().year - 2}-01-01"
            raw, _, _ = fetch("https://api.crossref.org/works?" + urllib.parse.urlencode(params))
            records = []
            for item in json.loads(raw)["message"]["items"]:
                titles = item.get("title") or []
                if not titles or not item.get("DOI"):
                    continue
                date = item.get("published", {}).get("date-parts", [[0]])
                records.append(paper_record({"title": titles[0], "doi": item["DOI"],
                    "year": date[0][0], "url": "https://doi.org/" + item["DOI"],
                    "abstract": html.unescape(re.sub("<[^>]+>", " ", item.get("abstract", ""))),
                    "authors": [{"name": (a.get("given", "") + " " + a.get("family", "")).strip()} for a in item.get("author", [])],
                    "source": "crossref", "pdf_urls": [p["URL"] for p in item.get("link", []) if p.get("content-type") == "application/pdf"]}, query, "crossref"))
            return records

        rows = search_papers(query, limit=8, sources=(source,),
                             year_min=datetime.now().year - 2 if period == "recent" else 0,
                             s2_api_key=os.environ.get("S2_API_KEY", ""),
                             openalex_api_key=os.environ.get("OPENALEX_API_KEY", ""))
        return [paper_record(p.to_dict(), query, source) for p in rows]

    def resolve(self, reference: str) -> dict[str, Any]:
        reference = reference.strip()
        doi = re.sub(r"^https?://(?:dx\.)?doi.org/", "", reference)
        if doi.startswith("10."):
            raw, _, _ = fetch("https://api.crossref.org/works/" + urllib.parse.quote(doi, safe=""))
            item = json.loads(raw)["message"]
            dates = item.get("published", {}).get("date-parts", [[0]])
            row = {"title": (item.get("title") or [""])[0], "doi": doi,
                   "year": dates[0][0], "abstract": re.sub("<[^>]+>", " ", item.get("abstract", "")),
                   "url": "https://doi.org/" + doi, "source": "crossref",
                   "pdf_urls": [x["URL"] for x in item.get("link", []) if x.get("content-type") == "application/pdf"]}
            return paper_record(row, reference, "crossref")
        raw, kind, final_url = fetch(reference)
        if raw.startswith(b"%PDF"):
            title = Path(urllib.parse.urlparse(final_url).path).name
        else:
            match = re.search(r"<title[^>]*>(.*?)</title>", raw.decode("utf-8", "replace"), re.S | re.I)
            title = html.unescape(match.group(1)).strip() if match else ""
        if not title:
            raise SessionError("无法识别论文元数据，请提供 DOI 或上传 PDF", 400)
        return paper_record({"title": title, "url": reference, "source": "user_url",
                             "abstract": "", "year": 0}, reference, "user_url")

    def pdf_document(self, path: Path) -> dict[str, Any]:
        from researchclaw.web.pdf_extractor import PDFExtractor

        result = PDFExtractor().extract(path)
        if not result.has_content:
            raise SessionError("PDF无法提取正文：请检查文件或提供可提取文本的版本", 400)
        import fitz

        with fitz.open(path) as doc:
            pages = [{"location": f"第 {i + 1} 页", "text": page.get_text()} for i, page in enumerate(doc)]
        return {"title": result.title, "abstract": result.abstract, "pages": pages}

    def fulltext(self, paper: dict[str, Any], directory: Path) -> dict[str, Any]:
        uploaded = paper.get("uploaded_pdf")
        if uploaded:
            return {**self.pdf_document(directory / uploaded), "source_url": "user_pdf"}
        urls = list(paper.get("pdf_urls") or [])
        if paper.get("arxiv_id"):
            urls += ["https://arxiv.org/pdf/" + paper["arxiv_id"]]
        doi = paper.get("doi")
        if doi:
            try:
                data, _, _ = fetch("https://api.openalex.org/works/https://doi.org/" + urllib.parse.quote(doi, safe="/"))
                meta = json.loads(data)
                for loc in [meta.get("best_oa_location"), meta.get("primary_location"), *(meta.get("locations") or [])]:
                    if loc:
                        urls.extend(u for u in [loc.get("pdf_url"), loc.get("landing_page_url")] if u)
            except Exception:
                pass
        urls += [paper.get("url") or ("https://doi.org/" + doi if doi else "")]
        # Publisher HTML comes before a PDF fallback when that is the available URL.
        visited: set[str] = set()
        for url in urls:
            if not url or url in visited or len(visited) >= 8:
                continue
            visited.add(url)
            try:
                raw, kind, final = fetch(url)
                if raw.startswith(b"%PDF"):
                    target = directory / "downloads" / (paper["paper_id"] + ".pdf")
                    target.parent.mkdir(exist_ok=True)
                    target.write_bytes(raw)
                    document = self.pdf_document(target)
                else:
                    text = raw.decode("utf-8", "replace")
                    # Discover publisher-provided PDF links, never guess a paper URL.
                    links = re.findall(r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)', text, re.I)
                    links += re.findall(r'href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']', text, re.I)
                    urls.extend(urllib.parse.urljoin(final, html.unescape(link)) for link in links[:3])
                    from researchclaw.web.crawler import WebCrawler
                    # Reuse the existing parser without another uncontrolled fetch.
                    article = re.search(r"<(?:article|main)\b[^>]*>(.*?)</(?:article|main)>", text, re.S | re.I)
                    body = article.group(1) if article else text
                    body = re.sub(r"<(nav|footer|header)\b[^>]*>.*?</\1>", "", body, flags=re.S | re.I)
                    document = {"pages": [{"location": "HTML正文", "text": html.unescape(WebCrawler._html_to_markdown(body))}]}
                full = "\n".join(p["text"] for p in document["pages"])
                if len(full) >= 2000 and re.search(r"\bmethod(?:s|ology)?\b|\bmaterials\b|\balgorithm\b|方法|模型构建", full, re.I):
                    return {**document, "source_url": final}
            except Exception:
                continue
        raise SessionError("未取得含方法正文的全文，可补充PDF或公开链接后重试")
