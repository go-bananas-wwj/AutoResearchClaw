"""论文真实编译：在 researchclaw/latex 兄弟容器里跑 latexmk（无网络），返回是否成功、页数与按文件定位的错误。"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

IMAGE = "researchclaw/latex:latest"
_FILE_LINE = re.compile(r"^(?:\./)?([\w./-]+\.(?:tex|bbl)):(\d+): (.*)$", re.MULTILINE)


def _pages(pdf: Path) -> int:
    try:
        import pymupdf  # PyMuPDF：控制面已安装（[pdf] extra）
        with pymupdf.open(pdf) as doc:
            return doc.page_count
    except Exception:  # noqa: BLE001
        data = pdf.read_bytes()
        return len(re.findall(rb"/Type\s*/Page(?!s)", data))


def compile_tex(workdir: Path, tex: str = "paper.tex", *, engine: str = "pdf", timeout: int = 300) -> dict[str, Any]:
    """编译 workdir/tex。engine 为 pdf（pdflatex）或 xelatex。不修改源文件，只产出 PDF 与日志。"""
    from researchclaw.experiment.docker_sandbox import DockerSandbox
    workdir = workdir.resolve()
    host = DockerSandbox._host_visible(workdir)
    stem = Path(tex).stem
    for stale in (workdir / f"{stem}.pdf", workdir / "compile.log"):
        stale.unlink(missing_ok=True)
    # -g：每次都真正重编；否则 latexmk 按 .fdb_latexmk 判定"源文件未变"，直接复述上一次的失败（真实运行中出现过）
    script = (f"latexmk -g -{engine} -interaction=nonstopmode -halt-on-error -file-line-error {tex} "
              f"> compile.log 2>&1; echo $? > compile.rc")
    cmd = ["docker", "run", "--rm", "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}",
           "-e", "HOME=/tmp", "-v", f"{host}:/work", "-w", "/work", IMAGE, "sh", "-c", script]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return {"ok": False, "pages": 0, "errors": [f"编译超时（{timeout}s）"], "log_tail": ""}
    log = (workdir / "compile.log").read_text(encoding="utf-8", errors="replace") if (workdir / "compile.log").is_file() else ""
    rc = (workdir / "compile.rc").read_text().strip() if (workdir / "compile.rc").is_file() else "?"
    pdf = workdir / f"{stem}.pdf"
    errors = [f"{m.group(1)}:{m.group(2)}: {m.group(3)}" for m in _FILE_LINE.finditer(log)]
    if not errors:
        # 没有 file:line 时，按日志里错误之前最后打开的源文件定位（"(./sections/results.tex"）
        lines = log.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("! "):
                opened = re.findall(r"\((?:\./)?((?:sections|sections_zh|figures)/[\w.-]+\.tex)", "\n".join(lines[:i]))
                where = f"{opened[-1]}:0: " if opened else ""
                detail = " ".join(x.strip() for x in lines[i + 1:i + 3] if x.strip())
                errors.append(f"{where}{line[2:]} {detail}".strip())
        errors = errors[:10]
    ok = rc == "0" and pdf.is_file()
    if not ok and not errors:
        errors = [f"latexmk 退出码 {rc}"]
    return {"ok": ok, "pages": _pages(pdf) if pdf.is_file() else 0, "errors": errors[:20] if not ok else [],
            "log_tail": log[-3000:]}
