"""Resumable, verified file downloads (no third-party dependencies beyond requests)."""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def fetch(url: str, dest: Path, *, headers: dict[str, str] | None = None,
          expected_size: int | None = None, retries: int = 4, timeout: float = 60.0,
          session: requests.Session | None = None) -> Path:
    """Download ``url`` to ``dest``, resuming a ``.part`` file with HTTP Range.

    Only this file is retried on failure.  The final file appears atomically
    (rename from ``.part``) and only after its size matches ``expected_size``
    or the server's Content-Length, so a half-written file never looks done.
    """
    if dest.is_file() and (expected_size is None or dest.stat().st_size == expected_size):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    if expected_size is not None and part.is_file() and part.stat().st_size > expected_size:
        part.unlink()  # 比预期还大的 .part 已损坏（如并发写入），续传只会卡住

    http = session or requests.Session()
    last: Exception | None = None
    for attempt in range(retries):
        have = part.stat().st_size if part.is_file() else 0
        req_headers = dict(headers or {})
        if have:
            req_headers["Range"] = f"bytes={have}-"
        try:
            with http.get(url, headers=req_headers, stream=True, timeout=timeout) as resp:
                if resp.status_code == 416:  # Range 越过末尾：用 HEAD 拿真实大小核对 .part 是否已完整
                    head = http.head(url, headers=headers or {}, timeout=timeout, allow_redirects=True)
                    length = head.headers.get("Content-Length")
                    if length and length.isdigit() and part.stat().st_size == int(length):
                        part.replace(dest)
                        return dest
                    part.unlink(missing_ok=True)
                    raise OSError("断点续传越界且大小无法核对，已丢弃 .part 重下")
                resp.raise_for_status()
                if have and resp.status_code != 206:
                    have = 0  # server ignored Range: restart this file
                total = expected_size
                if total is None and resp.headers.get("Content-Range", "").rpartition("/")[2].isdigit():
                    total = int(resp.headers["Content-Range"].rpartition("/")[2])
                if total is None and resp.headers.get("Content-Length"):
                    total = have + int(resp.headers["Content-Length"])
                if total is None:
                    raise OSError("服务器未提供文件大小，无法确认下载完整")
                with part.open("ab" if have else "wb") as out:
                    for block in resp.iter_content(1 << 20):
                        out.write(block)
            size = part.stat().st_size
            if total is not None and size != total:
                if size > total:
                    part.unlink()
                raise OSError(f"size mismatch {size} != {total}")
            part.replace(dest)
            return dest
        except (requests.RequestException, OSError) as exc:
            last = exc
            logger.warning("download %s failed (attempt %d/%d): %s", dest.name, attempt + 1, retries, exc)
            time.sleep(min(60, 5 * 2 ** attempt))
    if expected_size is not None and part.is_file() and part.stat().st_size == expected_size:
        part.replace(dest)
        return dest
    # 大小未知或不符时绝不把 .part 当作完成文件；保留 .part 供下次续传
    raise OSError(f"下载失败：{dest.name}（{last}）")
