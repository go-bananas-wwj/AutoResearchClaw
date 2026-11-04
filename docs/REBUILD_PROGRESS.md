# 重构进度（按 docs/AUTORESEARCH_GOAL_PROMPT_CN.md 执行）

每次开始工作先读本文件、`docs/rebuild_checklist.json` 与 `git log`。

## 当前阶段
P2 实验树已接入；等待真实数据下载完成后用真实项目跑通（P3 写作随后）

## 日志
- 2026-11-03：建立进度文件与完成标准清单（19 条，全部 passes=false）。
- 2026-11-03：P0 执行底座——rc-worker 独立执行 + 看门狗自动续跑（researchclaw/worker，tests/test_worker_supervisor.py）；server.executor=worker 时 Web 只投递任务。
- 2026-11-03：阶段10 CodeAgent 蓝图/逐文件回复按提示词 SHA-256 缓存于 run_dir/.cache/codegen（恢复归档不影响），截断/空回复不缓存；删除阶段10合成 numpy main.py 静默回退；论文初稿删除 [PLACEHOLDER]，失败即阶段失败；修复 PaperCoWriter re.search 缺 pattern；HITL 指导改稿不再截前 8000 字且有截断守卫。
- 2026-11-03：真实数据方案经用户确认（docs/DATA_SELECTION_SUPERSEDED_CN.md）；data_acquisition 模块；/home/wwj/rc-data 以同路径挂载进控制面与沙箱（docker.data_mounts）。规划 8 条轨道 914 个干涉对；Planetary Computer 核实仅有 sentinel-1-grd/rtc，无 SLC/干涉图。后台获取已启动（日志 /home/wwj/rc-data/insar_landslide/acquisition.log）。
- 基线：干净 HEAD 上 5 项既有失败（test_fabrication_guards::TestStage12HardGuards×3、test_overleaf::test_setup_clones、test_rc_templates::test_returns_canonical_names），P0 后全量 3248 通过、仍为这 5 项失败，待修。

- 2026-11-03：5 项既有失败测试按本地适配修正；全量 3269 通过、0 失败。
- 2026-11-03：P1——ExperimentSpec（stage-09/experiment_spec.json）+ validate_spec；补丁式实现 PatchCodeAgent（code_agent.backend=patch，遥感预设启用），验收修复同样走补丁步骤；验收关卡改为 语法→method_checks→RC_VALIDATION dry-run→数据回执→模型审查，并清理上一轮验收输出。
- 2026-11-03：P2——experiment_tree（nodes.jsonl + git 工作区 + best.json 棘轮）；accept_final_code 每次验收记为节点并记录各关卡；阶段12 正式运行用 detached 容器（run_dir/.formal_runs，恢复时按容器名重新接上，运行期只 docker inspect/logs），formal 关卡与主指标写入节点后 promote；阶段13 候选以 best 为父节点，提升由棘轮决定，改进生成走补丁步骤。
- 数据：HyP3 914 对已提交（每对 1 credit），产物 4 路并行下载（CloudFront 实测 12.7 MB/s）；Planetary Computer DEM 很慢，改后台线程。
- 2026-11-03：P3——证据账本 writing/ledger.py（result/setting/reference/literature，均带来源位置）；阶段17初稿注入账本数字表；VerifiedRegistry 并入账本数值（修复超参被消毒为 ---）；阶段23 主张校验（数字与 \cite 均须在账本有出处，否则阶段失败并写 claim_check.json，不自动改正文）。全量 3271 通过。

- 2026-11-03：用户纠正——科研决策必须由 DeepSeek Flash 在框架内产出，协作者只设计框架（AGENTS.md 规则 3）。删除硬编码选点模块 insar_landslide.py，改为 planner（模型起草 data_plan.json + 宿主校验）+ sources（通用工具）；已下载产物迁入 /home/wwj/rc-data/shared 复用；停止并将从阶段9重新运行，由模型自行规划。
- 待办：协同（非 auto_approve）模式下 data_plan 的人工确认门控尚未接入（本次运行为用户选择的全自动）。
- 2026-11-03 06:26：真实运行中 DeepSeek Flash 自行起草数据方案，第 3 轮通过宿主校验（前两轮引文未逐字命中被退回）：白格、鸡场镇（有破坏日期）+ 鱼家营、法那、El Forn（右删失）；HyP3 burst + cop-dem-glo-30，估计 1251 credits / 31.6 GB。
- 由该方案发现的框架缺口（已改框架，作用于今后运行，不改正在运行的方案）：对象相距 < 1 km 判为不独立（鱼家营与法那约 0.7 km）；asf_insar_burst 描述写明按 target 坐标检索。未改：坐标出处尚未纳入契约（模型自述多为近似定位）。
- 2026-11-03 07:00：用户同意重启 worker 以应用两阶段获取；看门狗自动续跑，6 条轨道任务在 40 秒内一次性提交（两阶段生效）。暴露缺陷：stage-09 被恢复归档导致已通过的 data_plan 丢失、模型重新规划（新对象：鸡场镇、El Forn、Soufrière，约 1002 credits）。已修：方案存 run_dir/.cache/data_plan 并复用前重校验（对后续重启生效）。
- HyP3 额度消耗：手选方案 914 + 首个模型方案 91 + 第二个模型方案约 702（其余复用），旧手选方案的大部分任务与当前研究无关。

## 待办（P0 剩余）
- R1–R3 真机实测已通过（见清单证据）。

### 2026-11-04 阶段 9 数据校验矛盾修复
- 真实数据两阶段获取完成并通过哈希校验（SUCCEEDED 210 批次收尾），但计划校验报"本地数据未经清单验证"：提示词要求模型以 `real_data.root` 作 `kind=local` 的 path，而 `validate_plan` 只认 `datasets` 根——框架自相矛盾，模型无错。已让校验同时接受带清单的真实数据根。
- 运行清单累积了作废方案（baige/fana/yujiaying 等）的条目；`planner.execute` 现按当前方案对象清理，文件留在共享库按需重新登记。
- 重启 worker/dashboard 后经 API 从 EXPERIMENT_DESIGN 恢复同一项目。

### 2026-11-04 阶段 10 补丁步骤截断
- 阶段 9 通过（模型方案 + 真实数据清单），阶段 10 补丁式实现在 `req:M4` 连续 3 次截断失败：每次重试都用同一 `step_max_tokens=16384`，推理模型在同一上限下必然再截断，"拆小编辑"反馈不足以恢复。
- 修复：`PatchCodeAgent.step` 截断时仅把本步骤上限翻倍（封顶 `llm.reasoning_max_tokens`），下一步骤回到原上限；与 `_chat_with_prompt` 一致，不是全局下限。
- 框架缺口：正常结束的失败不会被看门狗重试，框架修复后仍需经 API 恢复（`resume_run_id` + `from_stage`）。

### 2026-11-04 沙箱依赖自动检测误判
- 截断翻倍生效：阶段 10 补丁实现 M1–M10、wire 全部完成。随后方法断言在 Phase 0 pip 即失败：上游 `_detect_pip_packages` 用正则扫 import，把 docstring 中 "from the ..." 的 `the`、标准库 `__future__`/`inspect`、宿主包 `researchclaw` 追加进沙箱 requirements.txt。该文件由沙箱在暂存目录生成，模型的两次修复看不到也改不了。
- 修复：改为 AST 解析真实 import，排除标准库（`sys.stdlib_module_names`）、相对导入、本地模块/目录和宿主包；未知第三方包仍按上游语义自动安装。
