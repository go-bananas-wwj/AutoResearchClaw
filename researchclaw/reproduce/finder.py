"""官方代码仓库查找器——给定论文标识（arXiv id / 标题 / URL）找可复现的代码。

三个来源，任一挂掉自动降级到下一个，全挂返回空列表并说明原因：
  1. Papers with Code API —— repo 列表带官方/非官方标注与 stars；
  2. arXiv API —— 解析论文元数据（标题/摘要），并从评论/摘要里抠代码链接；
  3. Semantic Scholar API —— 输入是标题时反查 arXiv id，再串联前两个源。

只用 stdlib urllib（与 researchclaw.literature 一致），所有请求带超时。
国内网络下 PwC/S2/arXiv 任一不可达都不阻塞整体流程。
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_ARXIV_ID_RE = re.compile(r"\b(\d{4}\.\d{4,5})(v\d+)?\b")
_ARXIV_OLD_ID_RE = re.compile(r"\b([a-z-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?\b")
_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
_CODE_HOST_RE = re.compile(
    r"https?://(?:www\.)?(github\.com|gitlab\.com|huggingface\.co)/[^\s<>\"')\]]+",
    re.IGNORECASE,
)

_PWC_PAPERS_URL = "https://paperswithcode.com/api/v1/papers/"
_ARXIV_API_URL = "https://export.arxiv.org/api/query"
_S2_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
_S2_PAPER_URL = "https://api.semanticscholar.org/graph/v1/paper"

_UA = "researchclaw-reproduce/0.1 (+https://github.com/aiming-lab/AutoResearchClaw)"


@dataclass(frozen=True)
class PaperRef:
    """规范化后的论文标识。"""

    raw: str
    kind: str  # "arxiv_id" | "url" | "title"
    arxiv_id: str = ""
    title: str = ""


@dataclass(frozen=True)
class RepoCandidate:
    url: str
    stars: int = 0
    is_official: bool = False
    source: str = ""  # "papers_with_code" | "arxiv" | "semantic_scholar"
    note: str = ""


@dataclass(frozen=True)
class FinderResult:
    candidates: tuple[RepoCandidate, ...] = ()
    paper_title: str = ""
    paper_abstract: str = ""
    arxiv_id: str = ""
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return bool(self.candidates)


def parse_paper_ref(text: str) -> PaperRef:
    """把用户输入规范化为 PaperRef（arXiv id / URL / 标题）。"""
    raw = text.strip()
    m = _ARXIV_ID_RE.search(raw) or _ARXIV_OLD_ID_RE.search(raw)
    if m:
        return PaperRef(raw=raw, kind="arxiv_id", arxiv_id=m.group(1))
    if raw.lower().startswith(("http://", "https://")):
        return PaperRef(raw=raw, kind="url")
    return PaperRef(raw=raw, kind="title")


def normalize_repo_url(url: str) -> str:
    """归一化 repo URL 用于去重（去尾斜杠/.git、host 小写）。"""
    u = url.strip().rstrip("/")
    if u.endswith(".git"):
        u = u[: -len(".git")]
    return re.sub(r"^(https?://)([^/]+)", lambda m: m.group(1) + m.group(2).lower(), u)


def paper_slug(text: str, *, max_len: int = 48) -> str:
    """从 arXiv id 或标题生成目录安全的 slug。"""
    m = _ARXIV_ID_RE.search(text)
    base = m.group(1) if m else text
    slug = re.sub(r"[^A-Za-z0-9]+", "-", base).strip("-").lower()
    return (slug or "paper")[:max_len]


def _http_get(url: str, timeout: int, *, api_key: str = "") -> tuple[bytes | None, str]:
    """GET 请求，返回 (body, error)。任何失败都不抛异常。"""
    headers = {"User-Agent": _UA, "Accept": "*/*"}
    if api_key:
        headers["x-api-key"] = api_key
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), ""
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code} from {url}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, f"{type(exc).__name__}: {exc} ({url})"


def _http_get_json(url: str, timeout: int, *, api_key: str = "") -> tuple[object | None, str]:
    body, err = _http_get(url, timeout, api_key=api_key)
    if body is None:
        return None, err
    try:
        return json.loads(body.decode("utf-8", errors="replace")), ""
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON from {url}: {exc}"


# ---------------------------------------------------------------------------
# Source 1: Papers with Code
# ---------------------------------------------------------------------------


def _pwc_lookup(arxiv_id: str, timeout: int) -> tuple[list[RepoCandidate], str]:
    """Papers with Code：按 arXiv id 查论文，再查其 repositories。"""
    url = f"{_PWC_PAPERS_URL}?{urllib.parse.urlencode({'arxiv_id': arxiv_id})}"
    data, err = _http_get_json(url, timeout)
    if data is None:
        return [], f"papers_with_code: {err}"
    results = (data or {}).get("results") or []  # type: ignore[union-attr]
    if not results:
        return [], ""  # 论文不在 PwC——不是错误
    paper_id = results[0].get("id")
    if not paper_id:
        return [], "papers_with_code: paper entry missing id"
    repos_url = f"{_PWC_PAPERS_URL}{urllib.parse.quote(str(paper_id))}/repositories/"
    repos_data, err = _http_get_json(repos_url, timeout)
    if repos_data is None:
        return [], f"papers_with_code: {err}"
    candidates: list[RepoCandidate] = []
    for repo in (repos_data or {}).get("results") or []:  # type: ignore[union-attr]
        repo_url = str(repo.get("url") or "").strip()
        if not repo_url:
            continue
        try:
            stars = int(repo.get("stars") or 0)
        except (TypeError, ValueError):
            stars = 0
        candidates.append(
            RepoCandidate(
                url=repo_url,
                stars=stars,
                is_official=bool(repo.get("is_official")),
                source="papers_with_code",
                note=str(repo.get("framework") or ""),
            )
        )
    return candidates, ""


# ---------------------------------------------------------------------------
# Source 2: arXiv API（元数据 + 摘要/评论里的代码链接）
# ---------------------------------------------------------------------------


def _arxiv_lookup(arxiv_id: str, timeout: int) -> tuple[dict[str, object], str]:
    """arXiv API：取标题/摘要，并从文本里抠代码托管链接。"""
    url = f"{_ARXIV_API_URL}?{urllib.parse.urlencode({'id_list': arxiv_id, 'max_results': 1})}"
    body, err = _http_get(url, timeout)
    if body is None:
        return {}, f"arxiv: {err}"
    try:
        root = ET.fromstring(body.decode("utf-8", errors="replace"))
    except ET.ParseError as exc:
        return {}, f"arxiv: invalid XML: {exc}"
    ns = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    entry = root.find("a:entry", ns)
    if entry is None:
        return {}, f"arxiv: no entry for {arxiv_id}"
    title = (entry.findtext("a:title", default="", namespaces=ns) or "").strip()
    abstract = (entry.findtext("a:summary", default="", namespaces=ns) or "").strip()
    comment = (entry.findtext("arxiv:comment", default="", namespaces=ns) or "")
    code_urls = list(
        dict.fromkeys(
            m.group(0).rstrip(".,;")
            for m in _CODE_HOST_RE.finditer(abstract + "\n" + comment)
        )
    )
    return {"title": title, "abstract": abstract, "code_urls": code_urls}, ""


# ---------------------------------------------------------------------------
# Source 3: Semantic Scholar（标题 → arXiv id；或补元数据）
# ---------------------------------------------------------------------------


def _s2_resolve_title(title: str, timeout: int, api_key: str = "") -> tuple[str, str]:
    """S2 按标题搜索，返回 (arxiv_id, error)。找不到返回 ("", "")。"""
    url = (
        f"{_S2_SEARCH_URL}?"
        + urllib.parse.urlencode({"query": title, "fields": "title,externalIds", "limit": 3})
    )
    data, err = _http_get_json(url, timeout, api_key=api_key)
    if data is None:
        return "", f"semantic_scholar: {err}"
    for paper in (data or {}).get("data") or []:  # type: ignore[union-attr]
        ext = paper.get("externalIds") or {}
        arxiv_id = str(ext.get("ArXiv") or "").strip()
        if arxiv_id:
            return arxiv_id, ""
    return "", ""


def _s2_paper_metadata(arxiv_id: str, timeout: int, api_key: str = "") -> tuple[dict[str, str], str]:
    """S2 补论文标题/摘要（arXiv API 挂时的降级）。"""
    url = f"{_S2_PAPER_URL}/arXiv:{urllib.parse.quote(arxiv_id)}?fields=title,abstract"
    data, err = _http_get_json(url, timeout, api_key=api_key)
    if data is None:
        return {}, f"semantic_scholar: {err}"
    return {
        "title": str((data or {}).get("title") or ""),  # type: ignore[union-attr]
        "abstract": str((data or {}).get("abstract") or ""),  # type: ignore[union-attr]
    }, ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _dedupe_and_rank(candidates: list[RepoCandidate], max_results: int) -> list[RepoCandidate]:
    by_url: dict[str, RepoCandidate] = {}
    for c in candidates:
        key = normalize_repo_url(c.url)
        existing = by_url.get(key)
        if existing is None:
            by_url[key] = c
            continue
        # 同一 repo 多来源命中：官方标注优先，stars 取最大
        if c.is_official and not existing.is_official:
            by_url[key] = dataclasses.replace(c, stars=max(c.stars, existing.stars))
        else:
            by_url[key] = dataclasses.replace(
                existing, stars=max(existing.stars, c.stars)
            )
    out = list(by_url.values())
    out.sort(key=lambda c: (not c.is_official, -c.stars))
    return out[:max_results]


def find_code_repos(
    paper: str,
    *,
    s2_api_key: str = "",
    timeout_sec: int = 15,
    max_results: int = 10,
) -> FinderResult:
    """找论文的官方代码仓库。

    Args:
        paper: arXiv id / arXiv URL / 论文标题 / 代码仓库 URL（直接返回）。
        s2_api_key: Semantic Scholar API key（空 = 匿名限额）。
        timeout_sec: 每个源的 HTTP 超时。
        max_results: 返回候选上限。
    """
    ref = parse_paper_ref(paper)
    errors: list[str] = []

    # 直接给了 repo URL：不用找，单候选返回
    if ref.kind == "url" and _CODE_HOST_RE.match(ref.raw):
        return FinderResult(
            candidates=(RepoCandidate(url=ref.raw, source="user_provided"),),
        )

    arxiv_id = ref.arxiv_id
    title = ""
    abstract = ""

    # 标题输入 → 先用 S2 反查 arXiv id
    if ref.kind == "title":
        title = ref.raw
        arxiv_id, err = _s2_resolve_title(ref.raw, timeout_sec, s2_api_key)
        if err:
            errors.append(err)

    if not arxiv_id:
        # 不是 arXiv 论文（或反查失败）：无法继续，给出原因
        if ref.kind == "url" and "arxiv.org" in ref.raw:
            errors.append(f"无法从 URL 解析 arXiv id: {ref.raw}")
        if not errors:
            errors.append("未能确定 arXiv id（PwC/arXiv/S2 均不可用或无匹配）")
        return FinderResult(paper_title=title, errors=tuple(errors))

    # 元数据：arXiv API 优先，S2 兜底
    code_urls: list[str] = []
    meta, err = _arxiv_lookup(arxiv_id, timeout_sec)
    if err:
        errors.append(err)
        s2_meta, s2_err = _s2_paper_metadata(arxiv_id, timeout_sec, s2_api_key)
        if s2_err:
            errors.append(s2_err)
        else:
            meta = {"title": s2_meta.get("title", ""), "abstract": s2_meta.get("abstract", ""), "code_urls": []}
    title = str(meta.get("title") or title)
    abstract = str(meta.get("abstract") or "")
    code_urls = list(meta.get("code_urls") or [])

    # 候选 1：Papers with Code
    candidates: list[RepoCandidate] = []
    pwc_candidates, err = _pwc_lookup(arxiv_id, timeout_sec)
    if err:
        errors.append(err)
    candidates.extend(pwc_candidates)

    # 候选 2：arXiv 页面文本里抠出的代码链接（非官方标注）
    for u in code_urls:
        candidates.append(
            RepoCandidate(url=u, is_official=False, source="arxiv", note="from arXiv text")
        )

    ranked = _dedupe_and_rank(candidates, max_results)
    if not ranked and not errors:
        errors.append(f"三个源都没有 arXiv:{arxiv_id} 的公开代码记录")
    return FinderResult(
        candidates=tuple(ranked),
        paper_title=title,
        paper_abstract=abstract,
        arxiv_id=arxiv_id,
        errors=tuple(errors),
    )
