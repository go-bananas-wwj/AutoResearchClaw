# 重构进度（按 docs/AUTORESEARCH_GOAL_PROMPT_CN.md 执行）

每次开始工作先读本文件、`docs/rebuild_checklist.json` 与 `git log`。

## 当前阶段
P0 执行底座（进行中）

## 日志
- 2026-11-03：建立进度文件与完成标准清单（19 条，全部 passes=false）。
- 2026-11-03：P0 执行底座——rc-worker 独立执行 + 看门狗自动续跑（researchclaw/worker，tests/test_worker_supervisor.py）；server.executor=worker 时 Web 只投递任务。
- 2026-11-03：阶段10 CodeAgent 蓝图/逐文件回复按提示词 SHA-256 缓存于 run_dir/.cache/codegen（恢复归档不影响），截断/空回复不缓存；删除阶段10合成 numpy main.py 静默回退；论文初稿删除 [PLACEHOLDER]，失败即阶段失败；修复 PaperCoWriter re.search 缺 pattern；HITL 指导改稿不再截前 8000 字且有截断守卫。
- 2026-11-03：真实数据方案经用户确认（docs/DATA_PLAN_INSAR_CN.md）；data_acquisition 模块；/home/wwj/rc-data 以同路径挂载进控制面与沙箱（docker.data_mounts）。规划 8 条轨道 914 个干涉对；Planetary Computer 核实仅有 sentinel-1-grd/rtc，无 SLC/干涉图。后台获取已启动（日志 /home/wwj/rc-data/insar_landslide/acquisition.log）。
- 基线：干净 HEAD 上 5 项既有失败（test_fabrication_guards::TestStage12HardGuards×3、test_overleaf::test_setup_clones、test_rc_templates::test_returns_canonical_names），P0 后全量 3248 通过、仍为这 5 项失败，待修。

## 待办（P0 剩余）
- R1–R3 真机实测（需真实 run 运行中重启 dashboard / kill 子进程 / 重启 worker）。
- 5 项既有失败测试修复。
