"""研究任务书（Research Brief）—— Chat 多轮确认产物 → 流水线 stage 产物物化。

用户在 Chat 里逐步确认 6 件事（题目/科学问题/调研结论/假设/实验范围/目标会议），
打包成 ResearchBrief；``materialize()`` 把它写成 run_dir 下的标准 stage 产物
（stage-01/02/07/08[/09]），并返回建议的 ``from_stage``，使流水线可以从中间
阶段原生启动（``execute_pipeline(from_stage=...)``），跳过已人工确认的前期阶段。

可选增值：
- ``source_ideation_run``：复用选题引擎 run（artifacts/id-xxx）的 stage-03..06
  真实文献扫描产物；
- ``reproduced_baseline``：把复现模块产物（reproduction/<slug>/repo/）拷贝为
  ``run_dir/baseline_repo/``，并在 exp_plan 的 baselines 里标注 "our reproduction"
  实测基线，供 stage 10 在其基础上改进而不是从零重写。
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from researchclaw.pipeline.stages import Stage

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]

# 拷贝 baseline repo 时排除的名字（rc_ 前缀 = 复现凭证文件）
_BASELINE_EXCLUDE_DIRS = {".git", "__pycache__"}
_BASELINE_WARN_BYTES = 100 * 1024 * 1024  # 单个子目录 >100MB 警告


@dataclass
class ResearchBrief:
    """Chat 确认出的研究任务书（6 个槽位 + 2 个可选来源引用）。"""

    topic: str = ""
    scientific_question: str = ""
    survey_summary: str = ""
    hypotheses: list[str] = field(default_factory=list)
    experiment_scope: str = ""
    datasets: list[str] = field(default_factory=list)
    metrics: list[dict[str, str]] = field(default_factory=list)
    target_conference: str = ""
    # 可选：选题引擎 run id（id-xxx），复用其 stage-03..06 文献扫描产物
    source_ideation_run: str = ""
    # 可选：复现产物目录（reproduction/<slug> 或含它的路径），repo/ 作 baseline
    reproduced_baseline: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResearchBrief":
        """宽松反序列化：缺字段给默认值，metrics 条目容忍 str/dict。"""
        if not isinstance(data, dict):
            return cls()
        hypotheses = data.get("hypotheses") or []
        if isinstance(hypotheses, str):
            hypotheses = [hypotheses] if hypotheses.strip() else []
        datasets = data.get("datasets") or []
        if isinstance(datasets, str):
            datasets = [datasets] if datasets.strip() else []
        metrics: list[dict[str, str]] = []
        for m in data.get("metrics") or []:
            if isinstance(m, str) and m.strip():
                metrics.append({"metric_key": m.strip(), "direction": ""})
            elif isinstance(m, dict) and m.get("metric_key"):
                metrics.append(
                    {
                        "metric_key": str(m["metric_key"]),
                        "direction": str(m.get("direction") or ""),
                    }
                )
        return cls(
            topic=str(data.get("topic") or ""),
            scientific_question=str(data.get("scientific_question") or ""),
            survey_summary=str(data.get("survey_summary") or ""),
            hypotheses=[str(h) for h in hypotheses if str(h).strip()],
            experiment_scope=str(data.get("experiment_scope") or ""),
            datasets=[str(d) for d in datasets if str(d).strip()],
            metrics=metrics,
            target_conference=str(data.get("target_conference") or ""),
            source_ideation_run=str(data.get("source_ideation_run") or ""),
            reproduced_baseline=str(data.get("reproduced_baseline") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def suggest_from_stage(brief: ResearchBrief) -> Stage:
    """按确认深度建议流水线起始阶段。

    - 实验范围已确认（exp_plan.yaml 由任务书物化）→ CODE_GENERATION；
    - 确认到假设（stage-08 产物已物化）→ EXPERIMENT_DESIGN；
    - 否则从头跑。
    """
    if brief.experiment_scope.strip():
        return Stage.CODE_GENERATION
    if brief.hypotheses or brief.scientific_question.strip():
        return Stage.EXPERIMENT_DESIGN
    return Stage.TOPIC_INIT


def resolve_artifact_ref(ref: str) -> Path:
    """把 brief 里的引用解析成绝对路径：绝对路径原样；否则依次试
    仓库根相对路径、artifacts/ 相对路径。"""
    p = Path(ref)
    if p.is_absolute():
        return p
    candidate = REPO_ROOT / ref
    if candidate.exists() or ref.startswith("artifacts/"):
        return candidate
    return REPO_ROOT / "artifacts" / ref


# ---------------------------------------------------------------------------
# 产物 markdown 生成（按 brief 内容生成，不照拷他人产物）
# ---------------------------------------------------------------------------


def _render_goal_md(brief: ResearchBrief) -> str:
    metrics_lines = "\n".join(
        f"- {m['metric_key']}"
        + (f"（{m['direction']}）" if m.get("direction") else "")
        for m in brief.metrics
    ) or "- （待实验设计阶段细化）"
    conf_line = (
        f"\n**Target Venue**: {brief.target_conference}\n"
        if brief.target_conference
        else ""
    )
    return f"""# SMART Research Goal（人工确认 · 研究任务书）

> 本文件由研究任务书物化生成：以下内容已经过用户在对话中逐步确认，
> 非 LLM 自动起草。来源存证见 run_dir/research_brief.json。

## **Topic**
{brief.topic}

## **Scientific Question**
{brief.scientific_question or "（见研究假设）"}

## **Scope**
{brief.experiment_scope or "（待实验设计阶段细化）"}

## **SMART Goal**
**Specific**: {brief.scientific_question or brief.topic}

**Measurable**: 以确认的评价指标衡量：
{metrics_lines}

**Achievable**: 实验范围与数据已由研究者确认：
{chr(10).join(f"- {d}" for d in brief.datasets) or "- （待细化）"}

**Relevant**: 调研结论与空白见 stage-07/synthesis.md。

**Time-bound**: 由 experiment.time_budget_sec 控制。
{conf_line}
## **Success Criteria**
- 在确认的数据集上，所提方法相对基线在确认指标上取得可复现的改进
- 消融实验支持各核心假设
- 结果达到目标会议/期刊的实证标准
"""


def _render_problem_tree_md(brief: ResearchBrief) -> str:
    subs = "\n".join(
        f"### SQ{i}: 假设 {i}\n{h}\n" for i, h in enumerate(brief.hypotheses, 1)
    ) or "### SQ1: 核心科学问题\n" + (brief.scientific_question or brief.topic) + "\n"
    return f"""# Research Problem Decomposition（人工确认 · 研究任务书）: {brief.topic}

## Source

{brief.survey_summary or "（调研结论见 stage-07/synthesis.md）"}

核心科学问题：{brief.scientific_question or "（见各子问题）"}

## Sub-questions

{subs}
## Priority Ranking

各子问题按研究假设的依赖关系推进；实验范围已在任务书中确认：

{brief.experiment_scope or "（待实验设计阶段细化）"}

## Risks

- 数据可得性与规模是否支撑确认的实验范围
- 基线实现的公平性（若使用复现基线，见 baseline_repo/ 与复现报告）
- 指标方向与统计显著性检验
"""


def _render_synthesis_md(brief: ResearchBrief) -> str:
    return f"""# Literature Synthesis（人工确认 · 研究任务书）: {brief.topic}

> 本综述结论由研究者在对话中确认
{f"（文献扫描产物复用自选题运行 {brief.source_ideation_run}）" if brief.source_ideation_run else ""}。

## 调研结论与研究空白

{brief.survey_summary or "（未提供，阶段 7 之后的阶段将基于任务书其余部分推进）"}

## 核心科学问题

{brief.scientific_question or brief.topic}
"""


def _render_hypotheses_md(brief: ResearchBrief) -> str:
    blocks = []
    for i, h in enumerate(brief.hypotheses, 1):
        blocks.append(
            f"## Hypothesis {i}\n\n{h}\n\n"
            "**Measurable Prediction**: 见 stage-09/exp_plan.yaml 的指标设定。\n\n"
            "**Failure Condition**: 在确认数据集上相对基线无显著改进。"
        )
    body = "\n\n---\n\n".join(blocks) if blocks else (
        "## Hypothesis 1\n\n" + (brief.scientific_question or brief.topic)
    )
    return (
        f"# Research Hypotheses（人工确认 · 研究任务书）: {brief.topic}\n\n"
        "> 以下假设经研究者逐条确认，非自动生成。\n\n" + body + "\n"
    )


# ---------------------------------------------------------------------------
# exp_plan.yaml（必须过 stage 9 schema 守卫：
# baselines / proposed_methods / ablations 至少其一非空）
# ---------------------------------------------------------------------------


def _build_exp_plan(brief: ResearchBrief, config: Any = None) -> dict[str, Any]:
    metric_keys = [m["metric_key"] for m in brief.metrics if m.get("metric_key")]
    if not metric_keys:
        default_key = getattr(getattr(config, "experiment", None), "metric_key", "")
        metric_keys = [default_key or "primary_metric", "secondary_metric"]

    baselines: list[Any] = []
    if brief.reproduced_baseline.strip():
        slug = Path(brief.reproduced_baseline.rstrip("/")).name
        baselines.append(
            {
                "name": f"{slug} (our reproduction)",
                "note": (
                    "已复现跑通的实测基线：代码在 run_dir/baseline_repo/，"
                    "在其基础上按本计划改进，而不是从零重写；"
                    "实测指标见复现报告 reproduction_report.json"
                ),
            }
        )

    topic_prefix = brief.topic.split()[0] if brief.topic.split() else "method"
    proposed_methods = [f"{topic_prefix}_proposed"]
    ablations = ["without_key_component", "simplified_version"]

    time_budget = getattr(getattr(config, "experiment", None), "time_budget_sec", 0)
    compute_budget: dict[str, Any] = {"max_gpu": 1, "max_hours": 4}
    if isinstance(time_budget, int) and time_budget > 0:
        compute_budget["max_hours"] = max(1, round(time_budget / 3600, 1))

    plan = {
        "topic": brief.topic,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "research_brief (human-confirmed via chat)",
        "objectives": [
            brief.scientific_question or "Evaluate confirmed hypotheses"
        ],
        "experiment_scope": brief.experiment_scope,
        "datasets": brief.datasets or ["primary_dataset"],
        "baselines": baselines,
        "proposed_methods": proposed_methods,
        "ablations": ablations,
        "metrics": metric_keys,
        "risks": ["validity threats", "confounding variables"],
        "compute_budget": compute_budget,
    }
    # Schema 守卫自检（与 _experiment_design.py:352 同一判定）：三者全空不可落盘
    if not any(plan[k] for k in ("baselines", "proposed_methods", "ablations")):
        plan["proposed_methods"] = ["proposed_method"]
    return plan


# ---------------------------------------------------------------------------
# baseline repo 拷贝（排除 rc_ 凭证 / .git / __pycache__，大目录警告）
# ---------------------------------------------------------------------------


def _entry_size(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def _copy_baseline_repo(repro_dir: Path, dest: Path) -> list[str]:
    """把 reproduction/<slug>/repo/ 拷贝到 run_dir/baseline_repo/。

    返回警告列表（缺失 repo/、超大子目录等），不抛异常——baseline 是加分项，
    缺了不该阻断流水线。
    """
    warnings: list[str] = []
    src = repro_dir / "repo"
    if not src.is_dir():
        warnings.append(f"复现目录下没有 repo/：{repro_dir}")
        logger.warning("materialize: %s", warnings[-1])
        return warnings

    for entry in sorted(src.iterdir()):
        if entry.name.startswith("rc_") or entry.name in _BASELINE_EXCLUDE_DIRS:
            continue
        size = _entry_size(entry)
        if size > _BASELINE_WARN_BYTES:
            warnings.append(
                f"baseline 子目录 {entry.name} 体积 {size / 1024 / 1024:.0f}MB "
                f"(>100MB)，已拷贝但请注意实验沙箱挂载开销"
            )
            logger.warning("materialize: %s", warnings[-1])

    def _ignore(dir_path: str, names: list[str]) -> list[str]:
        return [
            n
            for n in names
            if n.startswith("rc_") or n in _BASELINE_EXCLUDE_DIRS
        ]

    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest, ignore=_ignore, dirs_exist_ok=True)
    logger.info("materialize: baseline repo copied %s -> %s", src, dest)
    return warnings


def _copy_ideation_stages(src_run_dir: Path, run_dir: Path) -> list[str]:
    """复用选题引擎 run 的 stage-03..06（真实文献扫描产物）。"""
    copied: list[str] = []
    for num in (3, 4, 5, 6):
        src = src_run_dir / f"stage-{num:02d}"
        if not src.is_dir():
            continue
        shutil.copytree(src, run_dir / src.name, dirs_exist_ok=True)
        copied.append(src.name)
    if copied:
        logger.info(
            "materialize: reused ideation stages %s from %s", copied, src_run_dir
        )
    return copied


# ---------------------------------------------------------------------------
# materialize
# ---------------------------------------------------------------------------


def materialize(
    run_dir: Path,
    brief: ResearchBrief,
    config: Any = None,
) -> Stage:
    """把研究任务书物化为 run_dir 下的 stage 产物，返回建议的 from_stage。"""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    def _write(stage_num: int, filename: str, content: str) -> None:
        d = run_dir / f"stage-{stage_num:02d}"
        d.mkdir(parents=True, exist_ok=True)
        (d / filename).write_text(content, encoding="utf-8")

    _write(1, "goal.md", _render_goal_md(brief))
    _write(2, "problem_tree.md", _render_problem_tree_md(brief))
    _write(7, "synthesis.md", _render_synthesis_md(brief))
    _write(8, "hypotheses.md", _render_hypotheses_md(brief))

    suggested = suggest_from_stage(brief)

    warnings: list[str] = []
    if brief.experiment_scope.strip():
        plan = _build_exp_plan(brief, config)
        _write(
            9,
            "exp_plan.yaml",
            yaml.dump(plan, default_flow_style=False, allow_unicode=True),
        )

    reused: list[str] = []
    if brief.source_ideation_run.strip():
        src_run = resolve_artifact_ref(brief.source_ideation_run)
        if src_run.is_dir():
            reused = _copy_ideation_stages(src_run, run_dir)
        else:
            warnings.append(f"选题运行目录不存在：{brief.source_ideation_run}")
            logger.warning("materialize: %s", warnings[-1])

    if brief.reproduced_baseline.strip():
        repro_dir = resolve_artifact_ref(brief.reproduced_baseline)
        warnings.extend(_copy_baseline_repo(repro_dir, run_dir / "baseline_repo"))

    record = brief.to_dict()
    record["generated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    record["suggested_from_stage"] = suggested.name
    record["reused_ideation_stages"] = reused
    record["warnings"] = warnings
    (run_dir / "research_brief.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(
        "materialize: brief -> %s (suggested from_stage=%s, warnings=%d)",
        run_dir,
        suggested.name,
        len(warnings),
    )
    return suggested
