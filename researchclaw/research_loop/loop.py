"""研究循环驱动：立项书（用户确认）→ 多轮汇报/审阅 → write / pivot / stop（用户确认）。

替代阶段 13 的线性改进与阶段 15 的 REFINE/PIVOT 计数。门控沿用 HITL 文件 IPC：
有 HITL 会话（Web 协同模式）时在立项、换方向、开始写作处暂停等用户；全自动模式按导师决策继续，并记入日志。"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import yaml

from researchclaw.pipeline.activity import emit
from researchclaw.research_loop import charter as charter_mod
from researchclaw.research_loop.common import loop_dir, read_json, round_dir, save_state, state, write_json
from researchclaw.research_loop.round import Round



class LoopOutcome(dict):
    """{"status": write|pivot|stop|exhausted|rejected, "reason", "round", "review"}"""


def _gate(hitl: Any, run_dir: Path, *, stage: int, name: str, summary: str, files: tuple[str, ...]) -> tuple[bool, str]:
    """用户门控。返回 (是否批准, 用户留言)。没有 HITL 会话（全自动）时视为批准并记入活动日志。"""
    if hitl is None:
        emit("research", f"{name}（全自动模式，按导师决策继续）", summary[:2000], run_dir=run_dir,
             phase="research", status="done")
        return True, ""
    from researchclaw.hitl.intervention import HumanAction, PauseReason
    hitl.pause(stage, name, PauseReason.GATE_APPROVAL, context_summary=summary, output_files=files)
    answer = hitl.wait_for_human()
    message = " ".join(x for x in (answer.message, answer.guidance) if x)
    approved = answer.action in (HumanAction.APPROVE, HumanAction.SKIP, HumanAction.RESUME)
    if answer.action == HumanAction.ABORT:
        raise RuntimeError("用户中止研究循环：" + message)
    return approved, message


def _context(run_dir: Path, config: Any, tree: Any) -> dict[str, Any]:
    best = tree.best()
    metrics = {}
    if best:
        node = tree.node(best["node"]) or {}
        for path in sorted((run_dir / ".formal_runs").glob(f"*-{best['node']}/results.json")):
            from researchclaw.writing.ledger import MAX_LIST_LEAVES, _flatten
            flat = _flatten(read_json(path, {}) or {}, max_list=MAX_LIST_LEAVES)
            # 先放主指标与汇总指标，逐站点/配置等细节排在后面
            order = sorted(flat, key=lambda k: (not k.startswith(("primary_metric", "metrics/")), k.startswith("config/"), k))
            metrics = {k: flat[k] for k in order}
            break
        best = {**best, "gates": {k: v.get("ok") for k, v in (node.get("gates") or {}).items()}}
    return {"current_best": best, "current_best_metrics": dict(list(metrics.items())[:60]),
            "time_budget_sec": config.experiment.time_budget_sec}


def _snapshot_baseline(run_dir: Path, tree: Any, context: dict[str, Any]) -> None:
    """循环起点的可引用产物：最佳节点的标量结果与代码、此前的结果分析与模拟评审。

    第 1 轮还没有往期汇报，学生与导师的引文需要有出处；正式结果文件常有十几 MB 的数组，不适合直接引用。"""
    base = loop_dir(run_dir) / "baseline"
    best = tree.best()
    if not best or read_json(base / "best.json", {}).get("node") == best["node"]:
        return
    if base.exists():
        shutil.rmtree(base)
    write_json(base / "best.json", best)
    write_json(base / "metrics.json", context.get("current_best_metrics") or {})
    for name, code in tree.files_at(best["commit"]).items():
        (base / "code" / name).parent.mkdir(parents=True, exist_ok=True)
        (base / "code" / name).write_text(code, encoding="utf-8")
    for rel in ("stage-14/analysis.md", "stage-18/reviews.md", "stage-12/runs/run-1.json"):
        src = run_dir / rel
        if src.is_file() and src.stat().st_size < 2_000_000:
            (base / "earlier").mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, base / "earlier" / rel.replace("/", "__"))


def _finalize(run_dir: Path, stage_dir: Path, tree: Any, outcome: dict[str, Any]) -> None:
    """下游阶段沿用的产物：stage-13/experiment_final 为当前最佳节点代码，refinement_log.json 为循环摘要。"""
    best = tree.best()
    if best:
        final = stage_dir / "experiment_final"
        if final.exists():
            shutil.rmtree(final)
        for name, code in tree.files_at(best["commit"]).items():
            (final / name).parent.mkdir(parents=True, exist_ok=True)
            (final / name).write_text(code, encoding="utf-8")
    write_json(stage_dir / "refinement_log.json", {"backend": "research_loop", "outcome": outcome,
                                                   "best": best, "state": state(run_dir)})


def run_research_loop(run_dir: Path, stage_dir: Path, config: Any, llm: Any, *, hitl: Any = None) -> LoopOutcome:
    from researchclaw.pipeline._helpers import _read_prior_artifact
    from researchclaw.pipeline.execution_contract import _tree
    if llm is None:
        raise ValueError("研究循环需要模型")
    tree = _tree(run_dir, config)
    if tree.best() is None:
        raise ValueError("实验树没有最佳节点：先完成阶段 10–12 的首个实现与正式运行")
    spec = read_json(run_dir / "stage-09" / "experiment_spec.json", {}) or {}
    plan_text = _read_prior_artifact(run_dir, "exp_plan.yaml") or ""
    exec_plan = yaml.safe_load(plan_text) or {}

    # ---- 立项书（用户确认）
    context = _context(run_dir, config, tree)
    rejected = round_dir(run_dir, 1) / "advisor_note_charter.md"
    if rejected.is_file():  # 用户未批准上一版立项书：意见进入重写（上下文变化即重新起草）
        context["user_feedback_on_previous_charter"] = rejected.read_text(encoding="utf-8")
    _snapshot_baseline(run_dir, tree, context)
    charter = charter_mod.draft(run_dir, config, llm, context=context)
    (loop_dir(run_dir) / "charter.md").write_text(charter_mod.render(charter), encoding="utf-8")
    if not charter.get("confirmed"):
        ok, note = _gate(hitl, run_dir, stage=13, name="确认立项书", summary=charter_mod.render(charter),
                         files=("research_loop/charter.md", "research_loop/charter.json"))
        if not ok:
            # 用户意见作为第一轮的用户意见；立项书本身不由宿主改写
            (round_dir(run_dir, 1) / "advisor_note_charter.md").write_text(note or "用户未批准立项书", encoding="utf-8")
            outcome = LoopOutcome(status="rejected", reason="用户未批准立项书：" + note, round=0, review=None)
            save_state(run_dir, status="rejected", reason=outcome["reason"])
            return outcome
        charter["confirmed"] = True
        write_json(charter_mod.charter_path(run_dir), charter)

    max_rounds = int(charter.get("max_rounds") or config.experiment.research_loop.max_rounds)
    previous: dict[str, Any] | None = None
    st = state(run_dir)
    start = 1
    for n in range(1, max_rounds + 1):  # 已完成的轮次直接读回（断点续作）
        meta = read_json(round_dir(run_dir, n) / "round.json", {}) or {}
        review = read_json(round_dir(run_dir, n) / "review.json")
        if meta.get("done") and isinstance(review, dict):
            previous, start = review, n + 1
        else:
            break
    if st.get("status") in ("write", "stop", "pivot") and previous and previous.get("decision") == st["status"]:
        notes = sorted(round_dir(run_dir, start).glob("advisor_note*.md")) if start <= max_rounds else []
        if st["status"] == "write" and notes:
            # 写作决策之后又有新意见（审稿人要求补实验或用户意见）：重开循环，从下一轮继续
            emit("research", f"收到新的意见，研究循环从第 {start} 轮继续", "；".join(p.name for p in notes),
                 run_dir=run_dir, phase="research", status="running")
            previous = {**previous, "decision": "continue", "base": "best",
                        "next_task": "\n".join(p.read_text(encoding="utf-8") for p in notes)}
        else:
            outcome = LoopOutcome(status=st["status"], reason=st.get("reason", ""), round=start - 1, review=previous)
            _finalize(run_dir, stage_dir, tree, outcome)
            return outcome

    for n in range(start, max_rounds + 1):
        save_state(run_dir, status="running", round=n)
        emit("research", f"研究循环第 {n}/{max_rounds} 轮开始", run_dir=run_dir, phase="research", status="running")
        review = Round(n, run_dir=run_dir, config=config, llm=llm, charter=charter, spec=spec, exec_plan=exec_plan,
                       plan_text=plan_text, tree=tree, final_round=(n == max_rounds)).run(previous)
        decision = review["decision"]
        rel = f"research_loop/rounds/round-{n:02d}"
        if decision == "continue":
            previous = review
            continue
        if decision in ("write", "pivot"):
            name = "开始写作" if decision == "write" else f"换方向到 {review.get('pivot_to')}"
            ok, note = _gate(hitl, run_dir, stage=13, name=name,
                             summary=(round_dir(run_dir, n) / "review.md").read_text(encoding="utf-8"),
                             files=(f"{rel}/report.md", f"{rel}/review.md", "research_loop/results.tsv"))
            if not ok:
                # 用户不同意：意见写入下一轮，循环继续（最后一轮则如实结束）
                (round_dir(run_dir, n + 1) / "advisor_note_gate.md").write_text(
                    note or f"用户未同意「{name}」，请继续改进", encoding="utf-8")
                previous = {**review, "decision": "continue", "next_task": note or review.get("next_task", "")}
                if n == max_rounds:
                    break
                continue
        if decision == "pivot":
            _write_back_to_ideation(run_dir, review, charter)
        outcome = LoopOutcome(status=decision, reason=review.get("assessment", ""), round=n, review=review)
        save_state(run_dir, status=decision, round=n, reason=outcome["reason"])
        _finalize(run_dir, stage_dir, tree, outcome)
        return outcome
    outcome = LoopOutcome(status="exhausted", reason="达到最多轮数仍未得到写作或换方向的决策", round=max_rounds,
                          review=previous)
    save_state(run_dir, status="exhausted", reason=outcome["reason"])
    _finalize(run_dir, stage_dir, tree, outcome)
    return outcome


def _write_back_to_ideation(run_dir: Path, review: dict[str, Any], charter: dict[str, Any]) -> None:
    """换方向时把本项目的实验结论写回调研会话（结果回流），供同一创新点池后续选择参考。"""
    brief = read_json(run_dir / "research_brief.json", {}) or {}
    source = str(brief.get("source_ideation_run") or "")
    if "/ideation/" not in source:
        return
    session_dir = Path(source.split("/exports/")[0])
    if not session_dir.is_dir():
        session_dir = run_dir.parent / "ideation" / session_dir.name
    if not session_dir.is_dir():
        return
    write_json(session_dir / "experiment_feedback" / f"{run_dir.name}.json", {
        "run_id": run_dir.name, "topic": charter.get("topic"), "decision": "pivot", "pivot_to": review.get("pivot_to"),
        "assessment": review.get("assessment"), "diagnosis": review.get("diagnosis"),
        "results": (loop_dir(run_dir) / "results.tsv").read_text(encoding="utf-8")
        if (loop_dir(run_dir) / "results.tsv").is_file() else ""})

