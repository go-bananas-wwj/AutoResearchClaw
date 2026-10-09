"""PaperTranslator——中文定稿翻译成英文投稿版（IEEE/TGRS 模板）。

流程：deliverables/paper.tex（中文定稿）→ LLM 全文翻译并转换 preamble
为 IEEEtran journal 格式 → 数字多重集对照（翻译不得增删数字）→
VerifiedRegistry 校验兜底 → deliverables/paper_en.tex →
sync_run_to_overleaf(language="en") 推到 runs/<run_id>/en/。
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_NUM_RE = re.compile(r"(?<![A-Za-z0-9_‐‑–\-.])\d+(?:\.\d+)?(?![A-Za-z0-9_‐‑–\-.])")


def _numeric_tokens(tex: str) -> Counter:
    """提取 tex 中的数字 token 多重集（翻译保真对照用）。"""
    return Counter(_NUM_RE.findall(tex))


class PaperTranslator:
    """把中文定稿 .tex 翻译成英文 IEEE 版，数值保真后推到 Overleaf en/。"""

    def __init__(self, config: Any, llm: Any = None) -> None:
        self.config = config
        if llm is None:
            from researchclaw.llm import create_llm_client

            llm = create_llm_client(config)
        self.llm = llm

    def translate(self, run_dir: Path, run_id: str) -> dict:
        from researchclaw.writing.reviser import PaperReviser, _next_version, _strip_fence

        run_dir = Path(run_dir)
        deliverables = run_dir / "deliverables"
        zh_path = deliverables / "paper.tex"
        if not zh_path.is_file():
            return {"ok": False, "reason": "找不到中文定稿 deliverables/paper.tex"}
        zh_tex = zh_path.read_text(encoding="utf-8")

        versions_dir = run_dir / "paper_versions" / "en"
        versions_dir.mkdir(parents=True, exist_ok=True)
        version = _next_version(versions_dir)

        prompt = self._build_prompt(zh_tex)
        resp = self.llm.chat(
            [{"role": "user", "content": prompt}],
            system=(
                "You are an academic translator. Translate the Chinese LaTeX "
                "paper into English for an IEEE journal submission. Output "
                "only the complete .tex source."
            ),
        )
        en_tex = _strip_fence(resp.content)
        en_tex = self._force_ieee_preamble(en_tex)

        # 数字保真两道闸：①多重集对照（翻译不得增删数字）②registry 白名单校验
        src_nums = _numeric_tokens(zh_tex)
        out_nums = _numeric_tokens(en_tex)
        added = list((out_nums - src_nums).elements())
        dropped = list((src_nums - out_nums).elements())

        reverted: list[dict] = []
        fixed: list[dict] = []
        reviser = PaperReviser(self.config, llm=self.llm)
        registry = reviser._build_registry(run_dir)
        if registry is not None:
            en_tex, reverted, fixed = reviser._enforce_verified(en_tex, registry)
            # 校验替换可能改变多重集，重算一次报告值
            out_nums = _numeric_tokens(en_tex)
            added = list((out_nums - src_nums).elements())
            dropped = list((src_nums - out_nums).elements())

        en_path = deliverables / "paper_en.tex"
        en_path.write_text(en_tex, encoding="utf-8")
        (versions_dir / f"v{version}.tex").write_text(en_tex, encoding="utf-8")
        (versions_dir / "base.tex").write_text(en_tex, encoding="utf-8")

        pushed = False
        folder = f"runs/{run_id}/en"
        push_error = ""
        try:
            from researchclaw.overleaf.run_sync import sync_run_to_overleaf

            res = sync_run_to_overleaf(run_dir, run_id, self.config, "en")
            pushed = bool(res.get("ok") and res.get("pushed"))
            folder = res.get("folder", folder)
            if not res.get("ok"):
                push_error = str(res.get("reason", ""))
        except Exception as exc:  # noqa: BLE001
            logger.warning("overleaf push (en) failed for %s: %s", run_id, exc)
            push_error = str(exc)

        result = {
            "ok": True,
            "run_id": run_id,
            "language": "en",
            "version": version,
            "en_file": str(en_path),
            "added_numbers": added,
            "dropped_numbers": dropped,
            "reverted_numbers": reverted,
            "fixed_numbers": fixed,
            "pushed": pushed,
            "folder": folder,
            "registry": "ok" if registry is not None else "unavailable",
        }
        if push_error:
            result["push_error"] = push_error
        if added or dropped:
            logger.warning(
                "翻译数字多重集不一致：新增 %s，丢失 %s", added, dropped
            )
        return result

    # ------------------------------------------------------------------

    def _build_prompt(self, zh_tex: str) -> str:
        return (
            "下面是一篇中文 LaTeX 论文（ctex 模板）的定稿。请完整翻译成英文，"
            "面向 IEEE 期刊投稿。\n\n"
            "硬规则：\n"
            "1. 所有数字、小数、百分比必须逐字保留，一个都不能增删改；\n"
            "2. 所有 \\cite{...}、图表环境（figure/table）及其 \\label/\\ref/\n"
            "   \\includegraphics 路径原样保留；\n"
            "3. 文档结构逐节对应翻译，章节标题译为英文学术惯例"
            "（摘要→Abstract、引言→Introduction、相关工作→Related Work、\n"
            "   方法→Methodology、实验→Experiments、结果→Results、讨论→Discussion、\n"
            "   结论→Conclusion、局限性→Limitations）；\n"
            "4. preamble 转换为 IEEEtran journal 格式：\\documentclass[journal]{IEEEtran}，\n"
            "   去掉 ctex/CJK 相关包，保留 amsmath/graphicx/booktabs/hyperref；\n"
            "   摘要放进 \\begin{abstract}，关键词放进 \\begin{IEEEkeywords}；\n"
            "5. 输出完整 .tex 源码，不要代码围栏，不要任何解释。\n\n"
            "=== 中文定稿 ===\n" + zh_tex
        )

    @staticmethod
    def _force_ieee_preamble(tex: str) -> str:
        """机械兜底：LLM 若没换文档类，强制 ctexart → IEEEtran。"""
        tex = re.sub(
            r"\\documentclass(\[[^\]]*\])?\{(?:ctexart|ctexrep|ctexbook)\}",
            r"\\documentclass[journal]{IEEEtran}",
            tex,
            count=1,
        )
        # 清掉残留的 CJK 专用包引用
        tex = re.sub(r"^.*\\usepackage(\[[^\]]*\])?\{(?:xeCJK|CJKutf8|ctex)\}.*\n?", "", tex, flags=re.MULTILINE)
        return tex
