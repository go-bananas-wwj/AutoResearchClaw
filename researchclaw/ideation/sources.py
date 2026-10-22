"""Real academic retrieval and bounded, public full-text fetching."""
from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from html.parser import HTMLParser

from researchclaw.ideation.store import SessionError, fingerprint
from researchclaw.web._ssrf import check_url_ssrf


def _unique(values):
    return list(dict.fromkeys(html.unescape(str(value)).strip() for value in values if value and str(value).strip()))


def crossref_metadata(item: dict[str, Any]) -> dict[str, Any]:
    """Publication-time affiliations only; never infer from an author's current profile."""
    kind = item.get("type", "")
    venue_type = {"journal-article": "journal", "proceedings-article": "conference",
                  "posted-content": "preprint", "book-chapter": "book", "book": "book"}.get(kind, "unknown")
    authors = [{"name": " ".join(_unique([a.get("given"), a.get("family")])),
                "affiliation": "；".join(_unique(x.get("name") for x in a.get("affiliation", [])))}
               for a in item.get("author", [])]
    return {"venue": html.unescape(next(iter(item.get("container-title") or []), "")), "venue_type": venue_type,
            "authors": [a for a in authors if a["name"]],
            "institutions": _unique(x.get("name") for a in item.get("author", []) for x in a.get("affiliation", []))}


def openalex_metadata(item: dict[str, Any]) -> dict[str, Any]:
    locations = [item.get("primary_location"), *(item.get("locations") or [])]
    venues = [loc["source"] for loc in locations if loc and loc.get("source")]
    source = next((s for s in venues if s.get("type") in ("journal", "conference")), next(iter(venues), {}))
    authors, seen = [], set()
    institutions = []
    for entry in item.get("authorships", []):
        author = entry.get("author") or {}
        names = _unique(i.get("display_name") for i in entry.get("institutions", []))
        names = names or _unique(entry.get("raw_affiliation_strings") or [])
        institutions.extend(names)
        identity = author.get("id") or author.get("display_name")
        if identity and identity not in seen:
            authors.append({"name": author.get("display_name", ""), "affiliation": "；".join(names)})
            seen.add(identity)
    venue_type = source.get("type", "unknown")
    if item.get("type") == "preprint":
        venue_type = "preprint"
    return {"venue": html.unescape(source.get("display_name", "")), "venue_type": venue_type,
            "authors": authors, "institutions": _unique(institutions)}


class CitationMetadataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags: dict[str, list[str]] = {}

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "meta":
            return
        values = dict(attrs)
        key = (values.get("name") or values.get("property") or "").lower()
        content = (values.get("content") or "").strip()
        if content:
            self.tags.setdefault(key, []).append(content)


def citation_metadata(text: str) -> dict[str, Any]:
    parser = CitationMetadataParser()
    parser.feed(text)
    tags = parser.tags
    venue = next(iter(tags.get("citation_journal_title") or tags.get("citation_conference_title") or []), "")
    return {"venue": venue, "venue_type": "conference" if tags.get("citation_conference_title") else "journal" if venue else "unknown",
            "authors": [{"name": name, "affiliation": ""} for name in _unique(tags.get("citation_author", []))],
            "institutions": _unique(tags.get("citation_author_institution", []) + tags.get("citation_author_affiliation", []))}


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        error = check_url_ssrf(newurl)
        if error:
            raise SessionError("论文链接重定向到非公共地址", 400)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, limit: int = 25 * 1024 * 1024, *, timeout: float = 30) -> tuple[bytes, str, str]:
    if check_url_ssrf(url):
        raise SessionError("仅允许获取公共 HTTP/HTTPS 论文地址", 400)
    if urllib.parse.urlsplit(url).scheme == "https" and urllib.parse.urlsplit(url).hostname == "content.openalex.org" and os.getenv("OPENALEX_API_KEY"):
        parts = urllib.parse.urlsplit(url)
        params = dict(urllib.parse.parse_qsl(parts.query))
        params["api_key"] = os.environ["OPENALEX_API_KEY"]
        url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(params), parts.fragment))
    parts = urllib.parse.urlsplit(url)
    url = urllib.parse.urlunsplit((parts.scheme, parts.netloc,
        urllib.parse.quote(parts.path, safe="/%:@!$&'()*+,;=-._~"),
        urllib.parse.quote(parts.query, safe="=&%/:?@!$'()*+,;=-._~"), parts.fragment))
    req = urllib.request.Request(url, headers={"User-Agent": "ResearchClaw/0.5 AcademicResearch"})
    # Never forward credentials across redirects, including signed storage redirects.
    if urllib.parse.urlsplit(url).scheme == "https" and urllib.parse.urlsplit(url).hostname in ("api.openalex.org", "content.openalex.org"):
        key = os.getenv("OPENALEX_API_KEY", "")
        if key:
            req.add_unredirected_header("Authorization", "Bearer " + key)
    with urllib.request.build_opener(PublicRedirect()).open(req, timeout=timeout) as response:
        data = response.read(limit + 1)
        if len(data) > limit:
            raise SessionError("论文文件超过25MB，请提供较小PDF", 400)
        return data, response.headers.get("Content-Type", ""), safe_url(response.url)


def safe_url(url):
    parts = urllib.parse.urlsplit(url)
    # Store the source location, never signed download tokens or API keys.
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def fetch_failure(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return {401: "需要授权", 403: "访问被拒绝", 404: "链接不存在", 429: "来源限流"}.get(exc.code, f"来源返回HTTP {exc.code}")
    if isinstance(exc, SessionError):
        return str(exc)
    return "网络连接或正文解析失败"


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
                    **crossref_metadata(item),
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
                   **crossref_metadata(item),
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

    def enrich_metadata(self, paper: dict[str, Any]) -> dict[str, Any]:
        """Return verifiable bibliographic fields without changing paper identity or evidence."""
        if paper.get("bibliography_version") == 1:
            return {}
        result = {k: paper.get(k) for k in ("venue", "venue_type", "authors", "institutions")}
        result["authors"] = result.get("authors") or []
        result["institutions"] = _unique((result.get("institutions") or []) + [
            a.get("affiliation") for a in result["authors"] if isinstance(a, dict)])
        provenance = list(paper.get("bibliography_sources") or [])
        landing_pages = []
        doi, url = paper.get("doi"), paper.get("url", "")
        endpoint = "https://api.openalex.org/works/https://doi.org/" + urllib.parse.quote(doi, safe="/") if doi else (
            url.replace("https://openalex.org/", "https://api.openalex.org/works/") if url.startswith("https://openalex.org/W") else "")
        if endpoint:
            try:
                raw, _, _ = fetch(endpoint, 2 * 1024 * 1024)
                record = json.loads(raw)
                metadata = openalex_metadata(record)
                result.update({k: v for k, v in metadata.items() if v and v != "unknown"})
                provenance.append({"source": "OpenAlex", "url": endpoint})
                landing_pages = _unique(loc.get("landing_page_url") for loc in record.get("locations", []) if loc)
            except Exception:
                pass
        # DOI registration provides a useful independent publication record and missing affiliations.
        if doi and (not result.get("venue") or not result.get("institutions")):
            try:
                endpoint = "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="")
                raw, _, _ = fetch(endpoint, 2 * 1024 * 1024)
                record = json.loads(raw)["message"]
                if str(record.get("DOI", "")).lower() == doi.lower():
                    metadata = crossref_metadata(record)
                    for key in ("venue", "venue_type", "authors"):
                        if metadata.get(key) and metadata[key] != "unknown":
                            result[key] = metadata[key]
                    result["institutions"] = _unique(result.get("institutions", []) + metadata["institutions"])
                    provenance.append({"source": "Crossref", "url": endpoint})
            except Exception:
                pass
        if not result.get("venue") or result.get("venue_type") == "repository":
            for landing in landing_pages[:2]:
                if "openalex.org" in urllib.parse.urlparse(landing).netloc:
                    continue
                try:
                    raw, content_type, _ = fetch(landing, 2 * 1024 * 1024)
                    if "html" not in content_type:
                        continue
                    metadata = citation_metadata(raw.decode("utf-8", "replace"))
                    if metadata["venue"]:
                        result.update({k: v for k, v in metadata.items() if v and v != "unknown"})
                        provenance.append({"source": "publisher_metadata", "url": landing})
                        break
                except Exception:
                    continue
        if paper.get("arxiv_id") and not result.get("venue"):
            result.update(venue="arXiv", venue_type="preprint")
        result.update(bibliography_sources=provenance, bibliography_version=1)
        return {k: v for k, v in result.items() if v is not None}

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
        deadline = time.monotonic() + 90
        def read_url(url):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SessionError("本篇全文获取超时，可稍后重试")
            return fetch(url, timeout=min(30, remaining))
        urls = list(paper.get("pdf_urls") or [])
        if paper.get("arxiv_id"):
            urls += ["https://arxiv.org/pdf/" + paper["arxiv_id"], "https://arxiv.org/html/" + paper["arxiv_id"]]
        doi = paper.get("doi")
        attempts = []
        paper["fetch_attempts"] = attempts
        identifier = "https://doi.org/" + urllib.parse.quote(doi, safe="/") if doi else ""
        if not identifier and re.fullmatch(r"https://openalex.org/W[0-9]+", paper.get("url", "")):
            identifier = paper["url"].rsplit("/", 1)[-1]
        if identifier:
            lookup = "https://api.openalex.org/works/" + identifier
            try:
                data, _, _ = read_url(lookup)
                meta = json.loads(data)
                content = (meta.get("content_urls") or {}).get("pdf")
                if content:
                    if os.getenv("OPENALEX_API_KEY"):
                        urls.insert(0, content)
                    else:
                        attempts.append({"url": safe_url(content), "outcome": "OpenAlex缓存全文需要配置OPENALEX_API_KEY"})
                for loc in [meta.get("best_oa_location"), meta.get("primary_location"), *(meta.get("locations") or [])]:
                    if loc:
                        urls.extend(u for u in [loc.get("pdf_url"), loc.get("landing_page_url")] if u)
            except Exception as exc:
                attempts.append({"url": safe_url(lookup), "outcome": fetch_failure(exc)})
        urls += [paper.get("url") or ("https://doi.org/" + doi if doi else "")]
        # Publisher HTML comes before a PDF fallback when that is the available URL.
        visited: set[str] = set()
        for url in urls:
            if not url or url in visited or len(visited) >= 12:
                continue
            if time.monotonic() >= deadline:
                attempts.append({"url": safe_url(url), "outcome": "本篇全文获取超时，可稍后重试"})
                break
            visited.add(url)
            callback = getattr(self, "progress", None)
            if callback:
                callback(paper, "正在获取正文：" + (urllib.parse.urlsplit(url).hostname or "公开来源"))
            try:
                raw, kind, final = read_url(url)
                if raw.startswith(b"%PDF"):
                    target = directory / "downloads" / (paper["paper_id"] + ".pdf")
                    target.parent.mkdir(exist_ok=True)
                    target.write_bytes(raw)
                    document = self.pdf_document(target)
                else:
                    text = raw.decode("utf-8", "replace")
                    # Discover publisher-provided PDF links, never guess a paper URL.
                    parser = CitationMetadataParser()
                    parser.feed(text)
                    links = parser.tags.get("citation_pdf_url", [])
                    links += re.findall(r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)', text, re.I)
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
                    attempts.append({"url": safe_url(url), "outcome": "已取得方法正文"})
                    return {**document, "source_url": final}
                attempts.append({"url": safe_url(url), "outcome": "页面未提供可提取的方法正文"})
            except Exception as exc:
                attempts.append({"url": safe_url(url), "outcome": fetch_failure(exc)})
        reasons = list(dict.fromkeys(a["outcome"] for a in attempts))
        raise SessionError("未取得方法正文：" + "；".join(reasons or ["未提供公开全文地址"]))
