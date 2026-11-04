# 实验实施多智能体架构（参考 Kimi Code Tower）设计稿

状态：§4 实验准备已实现（2026-11-04，`researchclaw/pipeline/experiment_prep.py`）；§3 Tower 式编排已实现（2026-11-04，`researchclaw/pipeline/tower.py`，`code_agent.backend: tower`，遥感预设默认启用）。背景问题见 `docs/E2E_RUN_ISSUES_CN.md`。

## 1. 参考：Kimi Code Tower 怎么做

- 角色：**Tower 协调者**（规划 mission、派工、路由消息、指派审查、rebase、合并）；**Worker**（一个 mission 一个 git 分支 + worktree，只在自己的 worktree 里提交，不能再派子代理）；**Reviewer**（只读检查 worker 分支，批准或驳回）。
- 合并：mission 必须完成、基于最新 base、审查通过、改动不越出 scope，才按依赖顺序合入本地 base 分支。base 前进后对空闲 worker 自动 rebase，无冲突时原审查仍然有效。
- 持久化：状态与通信审计写在 `.tower/`。agent 失败、超时、被杀会记入协议，roster 标记"负责人已死"的 mission 并给出续跑提示；重启后 mission 和 worktree 都在，新协调者只需重新派工。
- 资源：`max_running_tasks` 限制并发；每个 agent 独立上下文。

## 2. 我们的问题与对应关系

| 本次真实运行的问题 | Tower 式做法 |
|---|---|
| 一步修 4 个缺失要求，来回震荡（#14） | 每条方法要求是一个 mission，独立分支，独立审查 |
| 审查完整源码导致输出截断（#13） | Reviewer 只审一个 mission 的 diff，上下文小 |
| 恢复后从头重放；修复预算被重放架空（#11、越界项） | 已合并的 mission 永久保留在 base 分支，恢复时只重派未完成的 |
| 失败反馈到不了模型（#12） | 每个 mission 的验收结果写进 `.tower/comms/`，是唯一的反馈来源 |
| 人工恢复约 12 次 | 协调者按 roster 自动重派失败的 mission；框架版本变化时自动重试 |

## 3. 设计

### 3.1 角色（全部是 DeepSeek Flash，独立上下文）

- **协调者**：读 ExperimentSpec，生成 mission 图。每条方法要求（M1…M12）一个 mission；另有接线（入口/指标输出）、基线、消融、负对照各一个，并写明依赖。协调者只规划、派工、合并，自己不写代码。
- **Worker**：在 `experiment_tree` 仓库的 mission 分支上，用现有补丁协议（编辑块、续写、截断翻倍）实现，并为自己的要求写 method_check。
- **Reviewer**：独立上下文，只读，对照要求规格审查该 mission 的 diff 和测试，给出逐字证据。
- **宿主**（不是模型）：执行 mission 级关卡（语法、该要求的 method_check 在沙箱实际运行）和 base 级集成关卡（dry-run、指标、数据回执、全局审查），并负责合并。

### 3.2 git 与持久化

- `run_dir/experiment_tree/` 作为仓库，`base` 分支是唯一真相；每个 mission 一个 `mission/<id>` 分支和 worktree。
- `run_dir/.tower/missions.json` 记录 mission 状态（planned/running/review/approved/merged/failed/abandoned）、依赖、负责的 agent、尝试次数和框架版本。`.tower/comms/*.jsonl` 记录派工、报告、审查和合并事件。
- 合并条件与 Tower 相同：完成、基于最新 base、审查通过、改动不越出 scope；合并后再跑 base 级集成关卡。
- 恢复：merged 的 mission 不再重做。running 的 mission 从它分支上的最后提交继续。failed 的按 roster 重派；如果框架版本没变且已失败 2 次，判为真失败。

### 3.3 集成失败的处理

base 级关卡失败时，协调者把失败归到具体 mission 或新开一个修复 mission，例如"数据回执：把合成数据落盘"，并走同样的 worker → reviewer → merge 流程。不再整体重写。

### 3.4 并发与成本

并发上限默认 3（沿用调研模块的 3 路并行），可配置。预算仍为 0（不限）。每个 agent 的模型调用沿用提示词哈希缓存。

## 4. 新增阶段：实验准备（阶段 9 与 10 之间）

目前数据获取塞在阶段 9 的末尾，阶段 10 的代码要自己摸索清单格式、文件类型和路径规则（#5、#10 都由此引起）。拆出独立阶段 **EXPERIMENT_PREP**：

1. **数据获取**：把阶段 9 里现有的模型起草数据方案、宿主校验、获取挪到这里。
2. **数据准备**：由 DeepSeek 写一个预处理脚本（例如读取 GeoTIFF、按研究对象裁剪、按时间堆叠、对齐相干性与相位），在沙箱里实际运行，产出 `prepared/` 数据集。
3. **数据卡**：宿主实际加载 `prepared/`，生成 `dataset_card.json`，内容是每个数组的形状、dtype、单位、坐标系、日期范围和 SHA-256。这是给阶段 10 的稳定接口。
4. **环境配置**：确认依赖（如 rasterio）在实验镜像里可用，并记录版本。
5. **合成数据**：计划里声明的合成数据也在这里生成并落盘、出具回执。

阶段 10 的代码只通过数据卡读 `prepared/`，不再直接面对原始清单。

实现方式：阶段号在 HITL 门控、恢复、前端和历史运行目录里有数十处硬编码（按阶段号算目录名、range(1, 24) 等），因此不重新编号，也不新增枚举项；实验准备作为阶段 10 开头的独立环节运行，产物放在 `run_dir/prep/`（不是阶段目录，恢复时不会被归档）和 `<data_base>/runs/<run_id>/prepared/`，活动日志里以 phase=prep、标题前缀“实验准备：”显示。数据获取暂留阶段 9 末尾，因为实验计划的校验需要知道真实数据已存在；以后可以改为阶段 9 只起草并校验数据方案，下载挪到这里。

## 5. 实施顺序（建议）

1. 实验准备阶段（改动面较小，直接消除一类契约问题）。
2. Tower 式 mission 编排：先串行（并发 1），把 mission 图、分支、审查、合并、持久化做对。
3. 打开并发，再加入框架版本感知的自动恢复。
4. 用本次运行 `.cache/` 里的真实 DeepSeek 回复做离线回放回归测试。

## 6. 实现说明（与设计的差异）

- **协调者**：DeepSeek 只负责判断方法要求之间的实现依赖（宿主校验 id 与无环，有环或失败时按互不依赖调度并记入协议）。mission 骨架（layout → 每条要求一个 req → wire）由宿主按规格生成。集成失败的分派是确定性的：审查判定缺失的要求重开对应 req；traceback 最内层文件归属的 mission 重开；其余新建 `repair-N`。分派不交给模型猜。
- **宿主脚手架**（2026-11-04 真实运行后调整）：`methods/<id>.py`、`checks/check_<id>.py` 桩和检查运行器 `method_checks.py` 由宿主按规格顺序确定性生成，运行器为宿主所有、任何 mission 改动都判越界。layout mission 只写 `config.py`/`data.py`/`main.py` 骨架。原因：真实运行中让模型一次写 28 个文件，两次只交出空 `__init__.py`，第三次运行器按字典序排 m1,m10,m11 却断言数字序。
- **scope**（原设计）：layout 生成"每条要求一个模块 `methods/<id>.py` + 一个检查 `checks/check_<id>.py`"的结构，`method_checks.py` 自动发现并断言检查数。req mission 只能改自己的两个文件，wire 只能改顶层文件。宿主按 diff 逐段匹配路径（`*` 不跨目录），越界直接退回，不送审。
- **mission 级关卡**：语法静态检查 + 独立审查（证据必须是改动后文件中的逐字片段，否则驳回）。审查分成 blocking/suggestions，只有阻断类问题（未完成核心任务、伪造或冒充、接口必然出错、越界）才驳回；审查能看到本 mission 之前的意见，不得前后矛盾。工作区里有宿主生成的 `experiment_spec.json`，供代码在运行时读取路径与指标键。沙箱实际运行仍在 base 级集成关卡里做（方法断言、dry-run、数据回执、全局审查），沿用阶段 10 的进展式修复预算。
- **合并**：worker 在 `mission/<id>` 分支提交 → 宿主在该 worktree 上 `rebase base` → `merge --ff-only`。冲突时中止 rebase，退回 worker 基于最新 base 重做，计一次尝试。
- **恢复**：`missions.json` 里 merged 的 mission 不再重做；running 的重新派工。尝试次数只在失败时增加，所以中断后的重新派工沿用同一步骤名，补丁回复按提示词哈希命中缓存。审查回复也按内容哈希缓存在 `.tower/missions/<id>/reviews/`。规格或计划变化时整个 `.tower/` 归档为 `.tower_superseded_<时间>` 后重建。
- **未做**：框架版本感知的自动恢复（§5 第 3 步）；mission 级沙箱运行该要求的检查；用 `.cache/` 回放做离线回归。
