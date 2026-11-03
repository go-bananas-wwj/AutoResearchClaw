# 重构进度（按 docs/AUTORESEARCH_GOAL_PROMPT_CN.md 执行）

每次开始工作先读本文件、`docs/rebuild_checklist.json` 与 `git log`。

## 当前阶段
P2 实验树已接入；等待真实数据下载完成后用真实项目跑通（P3 写作随后）

## 日志
- 2026-11-03：建立进度文件与完成标准清单（19 条，全部 passes=false）。
- 2026-11-03：P0 执行底座——rc-worker 独立执行 + 看门狗自动续跑（researchclaw/worker，tests/test_worker_supervisor.py）；server.executor=worker 时 Web 只投递任务。
- 2026-11-03：阶段10 CodeAgent 蓝图/逐文件回复按提示词 SHA-256 缓存于 run_dir/.cache/codegen（恢复归档不影响），截断/空回复不缓存；删除阶段10合成 numpy main.py 静默回退；论文初稿删除 [PLACEHOLDER]，失败即阶段失败；修复 PaperCoWriter re.search 缺 pattern；HITL 指导改稿不再截前 8000 字且有截断守卫。
- 2026-11-03：真实数据方案经用户确认（docs/DATA_PLAN_INSAR_CN.md）；data_acquisition 模块；/home/wwj/rc-data 以同路径挂载进控制面与沙箱（docker.data_mounts）。规划 8 条轨道 914 个干涉对；Planetary Computer 核实仅有 sentinel-1-grd/rtc，无 SLC/干涉图。后台获取已启动（日志 /home/wwj/rc-data/insar_landslide/acquisition.log）。
- 基线：干净 HEAD 上 5 项既有失败（test_fabrication_guards::TestStage12HardGuards×3、test_overleaf::test_setup_clones、test_rc_templates::test_returns_canonical_names），P0 后全量 3248 通过、仍为这 5 项失败，待修。

- 2026-11-03：5 项既有失败测试按本地适配修正；全量 3269 通过、0 失败。
- 2026-11-03：P1——ExperimentSpec（stage-09/experiment_spec.json）+ validate_spec；补丁式实现 PatchCodeAgent（code_agent.backend=patch，遥感预设启用），验收修复同样走补丁步骤；验收关卡改为 语法→method_checks→RC_VALIDATION dry-run→数据回执→模型审查，并清理上一轮验收输出。
- 2026-11-03：P2——experiment_tree（nodes.jsonl + git 工作区 + best.json 棘轮）；accept_final_code 每次验收记为节点并记录各关卡；阶段12 正式运行用 detached 容器（run_dir/.formal_runs，恢复时按容器名重新接上，运行期只 docker inspect/logs），formal 关卡与主指标写入节点后 promote；阶段13 候选以 best 为父节点，提升由棘轮决定，改进生成走补丁步骤。
- 数据：HyP3 914 对已提交（每对 1 credit），产物 4 路并行下载（CloudFront 实测 12.7 MB/s）；Planetary Computer DEM 很慢，改后台线程。
- 2026-11-03：P3——证据账本 writing/ledger.py（result/setting/reference/literature，均带来源位置）；阶段17初稿注入账本数字表；VerifiedRegistry 并入账本数值（修复超参被消毒为 ---）；阶段23 主张校验（数字与 \cite 均须在账本有出处，否则阶段失败并写 claim_check.json，不自动改正文）。全量 3271 通过。

## 待办（P0 剩余）
- R1–R3 真机实测已通过（见清单证据）。
