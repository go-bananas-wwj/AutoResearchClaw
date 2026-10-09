"""PaperReviser——按 Overleaf 批注改稿，数值保真校验后推回 Overleaf。

流程：拉回稿批注/diff → LLM 按批注改稿（白名单数值硬约束）→
VerifiedRegistry 校验（REJECT 数字能定位正确值就替换回去，否则换成
``---`` 并记进 reverted_numbers 报告，不静默放行）→ 写回
deliverables/paper.tex + 版本快照 → sync_run_to_overleaf 推回。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^```(?:latex|tex)?\s*\n(.*?)\n?```\s*$", re.DOTALL)
# 数字两侧不得紧邻这些字符（避免误伤 ResNet-18、ema_decay_0.9 等标识符）
_BOUNDARY = "A-Za-z0-9_‐‑–\\-."


def _strip_fence(text: str) -> str:
    """去掉 LLM 输出可能带的 ```latex 代码围栏。"""
    t = text.strip()
    m = _FENCE_RE.match(t)
    return m.group(1).strip() if m else t


def _next_version(versions_dir: Path) -> int:
    """下一个快照编号：已有 v<N>.tex 的最大 N + 1。"""
    n = 0
    if versions_dir.is_dir():
        for p in versions_dir.glob("v*.tex"):
            m = re.fullmatch(r"v(\d+)\.tex", p.name)
            if m:
                n = max(n, int(m.group(1)))
    return n + 1


def _replace_number(line: str, value: float, repl: str) -> tuple[str, int]:
    """把行内 value 的常见书写形式替换为 repl（词边界正则）。"""
    for cand in (
        f"{value:.4f}".rstrip("0").rstrip("."),
        f"{value:.3f}",
        f"{value:.2f}",
        f"{value:.1f}",
        f"{value:g}",
        str(value),
    ):
        pat = rf"(?<![{_BOUNDARY}])" + re.escape(cand) + rf"(?![{_BOUNDARY}])"
        if re.search(pat, line):
            return re.sub(pat, repl, line, count=1), 1
    return line, 0


class PaperReviser:
    """按 Overleaf 批注驱动的一轮论文改稿。"""

    def __init__(self, config: Any, llm: Any = None) -> None:
        self.config = config
        if llm is None:
            from researchclaw.llm import create_llm_client

            llm = create_llm_client(config)
        self.llm = llm

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def revise(
        self,
        run_dir: Path,
        run_id: str,
        language: str = "zh",
        extra_instruction: str = "",
    ) -> dict:
        from researchclaw.writing.annotations import load_annotation_report

        run_dir = Path(run_dir)
        report = load_annotation_report(run_dir, language)
        if not report["has_user_version"] and not extra_instruction:
            return {"ok": False, "reason": "没有拉取到用户改动，先拉取 Overleaf 改动"}

        deliverables = run_dir / "deliverables"
        paper_path = deliverables / "paper.tex"
        # 用户直接改的版本生效，以其为底稿；否则用当前 deliverables 稿
        if report["has_user_version"]:
            current = Path(report["pulled_file"]).read_text(encoding="utf-8")
        else:
            current = paper_path.read_text(encoding="utf-8")

        # 版本快照：改稿前的 deliverables/paper.tex 存 v<N>.tex
        versions_dir = run_dir / "paper_versions" / language
        versions_dir.mkdir(parents=True, exist_ok=True)
        version = _next_version(versions_dir)
        if paper_path.is_file():
            (versions_dir / f"v{version}.tex").write_text(
                paper_path.read_text(encoding="utf-8"), encoding="utf-8"
            )

        registry = self._build_registry(run_dir)
        prompt = self._build_prompt(current, report, extra_instruction, registry)
        resp = self.llm.chat(
            [{"role": "user", "content": prompt}],
            system="你是论文写作助手，按批注修改 LaTeX 论文，只输出完整 .tex 源码。",
        )
        new_tex = _strip_fence(resp.content)

        reverted: list[dict] = []
        fixed: list[dict] = []
        if registry is not None:
            new_tex, reverted, fixed = self._enforce_verified(new_tex, registry)

        deliverables.mkdir(parents=True, exist_ok=True)
        paper_path.write_text(new_tex, encoding="utf-8")
        if language == "zh":
            (deliverables / "paper_zh.tex").write_text(new_tex, encoding="utf-8")
        # base.tex 记录"最后一次推送版本"，作为下轮 diff 基准
        (versions_dir / "base.tex").write_text(new_tex, encoding="utf-8")

        pushed = False
        folder = f"runs/{run_id}/{language}"
        push_error = ""
        try:
            from researchclaw.overleaf.run_sync import sync_run_to_overleaf

            res = sync_run_to_overleaf(run_dir, run_id, self.config, language)
            pushed = bool(res.get("ok") and res.get("pushed"))
            folder = res.get("folder", folder)
            if not res.get("ok"):
                push_error = str(res.get("reason", ""))
        except Exception as exc:  # noqa: BLE001
            logger.warning("overleaf push failed for %s: %s", run_id, exc)
            push_error = str(exc)

        result = {
            "ok": True,
            "run_id": run_id,
            "language": language,
            "version": version,
            "applied_comments": len(report["comments"]),
            "applied_edits": len(report["edits"]),
            "reverted_numbers": reverted,
            "fixed_numbers": fixed,
            "pushed": pushed,
            "folder": folder,
            "registry": "ok" if registry is not None else "unavailable",
        }
        if push_error:
            result["push_error"] = push_error
        return result

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _build_registry(self, run_dir: Path) -> Any | None:
        """构建数值白名单；没有实验数据时返回 None（降级，跳过校验）。"""
        try:
            from researchclaw.pipeline.verified_registry import VerifiedRegistry

            direction = (
                getattr(
                    getattr(self.config, "experiment", None),
                    "metric_direction",
                    "higher_better",
                )
                or "higher_better"
            )
            reg = VerifiedRegistry.from_run_dir(
                run_dir, metric_direction=direction, best_only=True
            )
            if reg.values:
                return reg
        except Exception as exc:  # noqa: BLE001
            logger.warning("VerifiedRegistry 构建失败，降级为无白名单: %s", exc)
        return None

    def _build_prompt(
        self,
        current: str,
        report: dict,
        extra_instruction: str,
        registry: Any | None,
    ) -> str:
        parts = [
            "下面是一篇 LaTeX 论文的当前稿，以及用户在 Overleaf 上留下的批注和直接改动。\n",
            "=== 当前稿（以此为底稿，用户直接改动已生效，予以保留） ===\n" + current + "\n",
        ]
        if report["comments"]:
            parts.append("=== 用户批注（% 注释行，需逐条处理） ===")
            for c in report["comments"]:
                loc = (
                    f"（{c['section']}，第{c['line_no']}行）"
                    if c["section"]
                    else f"（第{c['line_no']}行）"
                )
                entry = f"- {loc}{c['text']}"
                if c["context"]:
                    entry += f"\n  原文上下文: {c['context']}"
                parts.append(entry)
            parts.append("")
        if report["edits"]:
            parts.append("=== 用户直接改动（改前 → 改后，供理解意图） ===")
            for e in report["edits"][:30]:
                sec = f"（{e['section']}）" if e["section"] else ""
                parts.append(f"- {sec}\n  改前: {e['before']}\n  改后: {e['after']}")
            parts.append("")
        if extra_instruction:
            parts.append(f"=== 用户补充指令 ===\n{extra_instruction}\n")
        if registry is not None and registry.values:
            wl = "、".join(f"{v:g}" for v in sorted(registry.values)[:300])
            parts.append(
                "=== 实验数值白名单（论文中允许出现的实验数值，逐字使用） ===\n"
                + wl
                + "\n"
            )
        parts.append(
            "硬规则：\n"
            "1. 所有实验数值必须逐字来自白名单，禁止编造新数字，"
            "禁止四舍五入出白名单外的数字；\n"
            "2. 保持 LaTeX 结构、ctex 文档类、图表环境及其引用、\\cite 引用不变；\n"
            "3. 只按批注/补充指令修改，无关内容一字不动；\n"
            "4. 批注处理完后，删除已处理的 % 批注注释行；\n"
            "5. 输出完整 .tex 源码，不要代码围栏，不要任何解释。"
        )
        return "\n".join(parts)

    def _enforce_verified(
        self, tex: str, registry: Any
    ) -> tuple[str, list[dict], list[dict]]:
        """REJECT 级未验证数字逐个处理：能找回正确值就替换，否则换 --- 并报告。"""
        from researchclaw.pipeline.paper_verifier import verify_paper

        try:
            result = verify_paper(tex, registry)
        except Exception as exc:  # noqa: BLE001
            logger.warning("verify_paper 失败，跳过数值校验: %s", exc)
            return tex, [], []
        if result.severity != "REJECT":
            return tex, [], []

        lines = tex.split("\n")
        reverted: list[dict] = []
        fixed: list[dict] = []
        for unv in sorted(result.unverified_numbers, key=lambda u: -u.line_number):
            if not (0 < unv.line_number <= len(lines)):
                continue
            line = lines[unv.line_number - 1]
            correct = self._nearest_verified(registry, unv.value)
            if correct is not None:
                new_line, n = _replace_number(line, unv.value, f"{correct:g}")
                if n:
                    lines[unv.line_number - 1] = new_line
                    fixed.append({
                        "line": unv.line_number,
                        "bad": unv.value,
                        "fixed_to": correct,
                    })
                    continue
            new_line, n = _replace_number(line, unv.value, "---")
            if n:
                lines[unv.line_number - 1] = new_line
            reverted.append({
                "line": unv.line_number,
                "value": unv.value,
                "section": unv.section,
                "replaced": bool(n),
            })
        if reverted or fixed:
            logger.warning(
                "revise 数值保真：%d 个数字替换回正确值，%d 个无据数字替换为 ---",
                len(fixed),
                len(reverted),
            )
        return "\n".join(lines), reverted, fixed

    @staticmethod
    def _nearest_verified(
        registry: Any, number: float, max_rel: float = 0.10
    ) -> float | None:
        """白名单里相对误差 ≤ max_rel 的最近值（疑似 LLM 改偏的正确值）。"""
        best = None
        best_rel = max_rel
        for v in registry.values:
            if v == number:
                continue
            rel = abs(v - number) / max(abs(v), 1e-9)
            if rel <= best_rel:
                best, best_rel = v, rel
        return best
