# AutoResearch 目标架构调研与重构方案（2026-11-03）

> 状态：设计稿，尚未实施。调研对象为 2025–2026 年公开的自动科研系统论文/开源框架；结论结合本 fork 实测故障（`rc-20261025-145941-c30e99` 在阶段 10 恢复 11 次）。

## 1. 现状诊断：为什么"实验做一半就断"

| 症状 | 根因 | 架构层面的问题 |
|---|---|---|
| 阶段 10 反复"推理模型返回截断或空正文" | 内置 CodeAgent 让模型**一次写出整个文件**（`methods.py` 单次请求近 3 分钟），推理 token 与正文共用上限 | 代码生成是"一次性大输出"，不是"小步编辑 + 运行反馈" |
| 一个文件失败 → 整个阶段失败 → 恢复时把 `native_draft/` 归档、全部重写 | 断点粒度是"阶段"，不是"文件/实验节点" | 没有细粒度、可续跑的工作状态 |
| 改 Python 必须 `docker compose restart dashboard`，正在跑的实验随之消失 | `server/routes/pipeline.py` 用 `asyncio.create_task + run_in_executor` 在 Web 进程内跑整条流水线 | 编排器与 Web 服务耦合，没有持久化执行（durable execution） |
| 恢复要手动跑驱动脚本 | heartbeat 只记录、无人监管 | 没有看门狗 / 自动续跑 |
| 指标方向矛盾（CRPS 被标 maximize）、方法审查不通过到阶段 10 才暴露 | 规格（spec）是散落在任务书、exp_plan、metric_direction.json 的多份文本 | 缺单一、机器可校验的"实验规格"真相源 |
| 23 个阶段线性串联，回滚目标写死（质量门回滚不含阶段 14） | 上游为"论文流水线"设计，不是为"实验迭代"设计 | 实验本质是**搜索/迭代**，被硬塞进线性阶段 |

代码核对中另外发现的隐患（均为当前工作树实测位置）：

- `_code_generation.py:951-985`：所有生成策略都返回空时，**静默替换为合成的 numpy `main.py`**，与"不凑假数据"原则直接冲突。
- `code_agent.py:582`：逐文件生成时一个文件抛异常就让整个阶段失败；`native_draft/` 只给 UI 读，恢复时从不复用。
- `llm/client.py:740/303`：只在 `thinking=enabled` 时把截断视为错误，其他路径会默默接受截断输出。
- `_paper_writing.py:401/578`：初稿是 3 次大调用，失败时插入占位文本；`:2562` 的 `re.search(draft_text, re.DOTALL)` 缺 pattern，PaperCoWriter 分节永远不生效；`:2532-2546` 的 HITL 指导只看前 8000 字就整稿覆盖。
- `server/routes/pipeline.py`：全局只允许一个活动 run（`_active_run`），无法多项目并行。

对比之下，ideation 已具备：原子写 `session.json` + revision 乐观锁；每次 LLM 调用是带 `call_id/prompt_hash` 的事务记录；截断先翻倍重试再拒绝；按 `sha256(输入+提示词)` 命中即跳过；并行 worker 用快照 + 增量合并。**新架构的核心就是把这套模式推广到实验与写作。**

结论：问题不在某个 token 上限，而在于**实验阶段的执行模型**不对。调研阶段（ideation）之所以好用，正是因为它已经具备了实验阶段缺的东西：持久会话、独立上下文、按内容哈希断点续作、局部失败不阻断整体、人在关键点决策。

## 2. 业界做法速览（只列对我们有用的机制）

| 系统 | 关键机制 | 可借鉴点 |
|---|---|---|
| **AI Scientist v2**（Sakana, 2025；Nature 2026 版 *Towards End-to-End Automation of AI Research*） | 实验管理器驱动的**agentic tree search**：节点=代码+运行结果，可扩展有希望的节点、从错误节点回退；分阶段（可行性→调参→主实验→消融）；并行 worker；VLM 审图 | 实验从"线性阶段"改为"节点树 + 阶段目标" |
| **AIDE**（MLE-bench 最强基线之一） | 解空间树；每次 LLM 调用只做窄任务（debug 某个节点 / improve 某个节点 / draft 新节点），失败节点不丢弃而是被调试 | 小步、窄任务的 LLM 调用 → 天然避免大输出截断 |
| **Karpathy autoresearch**（2026-03） | 人写 `program.md`，agent 只改一个训练文件；单一标量指标；**git 作棘轮**：变好就 commit，变差就 reset；`results.tsv` 记每次尝试 | 最佳版本只增不减；每次尝试有账本 |
| **Deep Researcher Agent**（arXiv 2604.05854） | Think→Execute→Reflect；**训练期间零 LLM 调用**，OS 级监控（进程存活、nvidia-smi、日志尾）；**强制 dry-run**（拦下 18% 计划，dry-run 后崩溃 <3%）；冻结的项目简报 + 有界记忆日志；受保护状态文件；`HUMAN_DIRECTIVE.md` 人工干预；长时运行于 tmux | 实验进程与 agent 解耦；廉价监控；先 smoke test |
| **Anthropic 长时 agent harness**（2025-11） | 初始化 agent 写 `init.sh`、进度文件、feature_list.json（每项含验证步骤与 `passes`，agent 只能改 `passes`），每次会话只做一项、结束必 commit；新会话先读 git log/进度、跑冒烟测试 | 用**文件 + git**承载连续性，而非上下文窗口；方法要求清单化 |
| **Kosmos**（Edison, 2025） | **结构化世界模型**作为共享知识库，数据分析 agent 与文献 agent 并行若干轮，每轮把结果摘要写回世界模型，下一轮任务从中提出；报告每句话可追溯到代码或文献（独立评估 79.4% 准确） | 结果/证据账本，写作从账本取材 |
| **data-to-paper**（Technion） | data-chaining：论文中的数字可追溯到生成它的代码行；逐步写作；人可介入 | 数字溯源到代码行，而非只有白名单 |
| **ScientistOne / Evidence-Ledger**（2026） | Chain-of-Evidence：主张分为引用/数值/方法/结论四类，各有证据链；定稿前 claim verifier 检查 LaTeX；不受支持的主张退回作者 | 写作阶段的"主张—证据"校验器 |
| **ARIS**（Claude Code skills） | 一个模型做研究，**另一个模型做对抗审稿**；夜间迭代；删除缺乏支撑的主张 | 审稿与写作用不同上下文（理想是不同模型） |
| **Agent Laboratory / AgentRxiv** | 人类在各阶段可介入；agent 可检索此前运行的报告并在其上继续 | 跨项目经验库 |
| **Dolphin / InternAgent** | 闭环：实验结果反馈回构思，类似失败想法被过滤，有效想法进入后续提示 | 实验结果回流到创新点池 |
| 综述 *Verification Gap*（arXiv 2608.05179）、MLReplicate、ResearchCodeBench | 83% 系统开源代码但仅 38% 提供种子/执行轨迹；LLM 审稿不能作最终裁判；58.6% 代码错误是"能跑但算法不对"的**语义错误**；人主导协作是最可靠模式 | 方法语义验收（我们已有）、完整执行轨迹、人工终审 |

## 3. 目标架构

```
┌────────────── Web / API（只做展示与指令，可随时重启）──────────────┐
│ 项目中心 UI  ·  REST/WS  ·  人工门控  ·  HUMAN_DIRECTIVE 注入         │
└───────────────▲──────────────────────────────┬─────────────────────┘
                │读状态/事件                   │写指令（文件/队列）
┌───────────────┴──────────────────────────────▼─────────────────────┐
│ Run Supervisor（独立常驻进程/容器，每个项目一个 worker）            │
│  · 状态机 + 持久化（run_state.json / ledger.sqlite）                │
│  · 看门狗：worker 消失 → 从最近节点自动续跑                         │
│  · 资源调度：GPU 队列、并发上限                                     │
└──┬───────────┬────────────────┬──────────────┬───────────────┬─────┘
   ▼           ▼                ▼              ▼               ▼
 ① 构思      ② 实验规格       ③ 实验树       ④ 证据账本      ⑤ 写作与审稿
 (保留现有   ExperimentSpec   节点=git提交    Evidence       大纲→逐节→
  ideation)  方法清单+验收    draft/debug/    Ledger         主张校验→
             +dry-run 契约    improve/ablate  (结果/设置/    独立审稿→
                              训练期零LLM监控  文献/图表)     人工终审
```

### 3.1 执行底座：Run Supervisor（解决"断掉"的根本）
- 流水线移出 Web 进程：新增 `rc-worker` 服务（同镜像、同挂载、同 docker.sock + GID），Web 只写"启动/恢复/指令"请求并读状态。重启 dashboard 不影响实验。
- 状态以**节点**为单位持久化（见 3.3），每完成一个最小动作就落盘；worker 崩溃或被重启后，看门狗根据 `heartbeat.json` 超时自动从最后一个已完成节点续跑，无需手动驱动脚本。
- 长训练作为**独立兄弟容器 detached 运行**，监控只做 OS 级检查（容器存活、GPU 利用率、日志尾、指标文件），训练期间不调用 LLM；进程结束才唤醒 agent 做 Reflect。
- 保留：docker-sibling 路径翻译（`RC_HOST_REPO`）、wsproto、预算 0、Flash-only。

### 3.2 实验规格：ExperimentSpec（单一真相源）
- 阶段 9 产出一个 JSON（不是散文）：科学问题、主指标 `{key, direction, 计算方式}`、数据来源与清单、基线、消融、资源预算、**方法要求清单**（沿用现有 12 项方法要求，改成 Anthropic feature_list 形态：每项含 `verify` 步骤与 `passes`，agent 只能在给出证据后改 `passes`）。
- 加载时做静态一致性校验（指标方向唯一、数据路径存在、资源不越界），把 CRPS-maximize 这类矛盾挡在阶段 9。
- 人工门控点：规格确认（这是实验阶段最值得人看的一页）。

### 3.3 实验树：Experiment Tree（替代阶段 10–14 的线性串联）
- 工作区是一个 git 仓库（`stage-exp/workspace/`），**每个节点 = 一次 commit + 一次运行记录**（`nodes.jsonl` 类似 `results.tsv`：节点 id、父节点、动作、commit、dry-run/正式结果、主指标、方法清单通过数、状态）。
- 节点动作是**窄任务**（AIDE 式）：`scaffold`（按规格搭骨架）、`implement(要求项)`（一次只实现/修一个方法要求）、`debug(节点)`、`improve(节点)`、`ablate(节点)`。
- 代码改动走**工具式编辑**（read_file / apply_patch / run_tests / run_dry），模型每次只输出补丁，不再一次吐出整个文件 —— 截断问题从根上消失；单次补丁截断仅重试该小步。实现方式：基于 DeepSeek tool calling 的轻量 ACI，复用现有 `LlmCodeAgent` 的验收与活动日志，不依赖 OpenCode。
- 门槛顺序：语法/导入 → `method_checks.py` 断言 → **强制 dry-run**（极小数据、2 步前后向）→ 正式运行 → 方法审查与数据哈希验收（沿用现有 `execution_acceptance` 机制）。
- **棘轮**：只有通过全部门槛且主指标改进的节点才成为 `best`；失败节点保留记录、可被 debug，不会污染 best。天然解决"陈旧产物污染"——结果只按节点 id 取，不按目录名猜。
- 阶段目标（AI Scientist v2 式）：可行性 → 主实验 → 基线对齐 → 消融；每阶段有节点数/时间上限，用尽则带着已有 best 进入下一阶段并如实标注。

### 3.4 证据账本：Evidence Ledger（Kosmos 世界模型 + data-to-paper 溯源）
- 一个 SQLite（或 jsonl）账本，条目类型：`result`（数值 + 产生它的节点/commit/文件/行号/日志位置）、`setting`（学习率、batch、种子、分辨率…来自规格与代码）、`literature`（调研会话的原文引文，直接复用 ideation 证据）、`figure`、`limitation`。
- 替代/扩展现有 `VerifiedRegistry`：设置值也入账，修复 AGENTS.md 记录的"超参被误替换为 `---`"待办。

### 3.5 写作与审稿
- 大纲由账本生成（每节列出要支撑的主张及对应账本条目）→ **逐节写作**（每节独立请求、独立输出上限，避免全文截断）→ **主张校验器**（ScientistOne 四类主张：引用/数值/方法/结论，均须指向账本条目，否则退回或删除）→ **独立上下文审稿**（ARIS 式对抗审稿；当前仅 Flash，可用不同系统提示与只读账本的"审稿人"角色，未来可换异构模型）→ 人工终审 → Overleaf 同步（沿用现有 zh/en 与批注改稿）。
- LLM 审稿只做筛选，不作最终裁判；最终放行在人。

### 3.6 构思模块（保留并优化）
现有"生成创新点 → 近邻比较 → 用户选择"保持。可加：
1. **实验结果回流**（Dolphin）：实验树的结论（有效/无效/不可行原因）写回调研会话，下次构思时同类失败想法降权。
2. **群体多样性监控**（Verification Gap 综述）：候选间相似度矩阵，防止 12 张卡同质化。
3. **时间留出式查重**（BioDisco）：近邻检索补一条"最近 6 个月"检索，降低"看似新颖实则已发表"。
4. **跨项目经验库**（AgentRxiv）：把每个项目的规格、best 节点、失败原因归档成可检索记录。

## 4. 与现有 23 阶段的映射

| 新模块 | 替代/复用 |
|---|---|
| ① 构思 | 现有 ideation 会话（阶段 1–8 物化保留） |
| ② ExperimentSpec | 阶段 9（`_experiment_design.py`、`execution_contract.py`） |
| ③ 实验树 | 阶段 10–14（`code_agent.py`、`_execution.py`、`experiment_repair.py`、`stage-13 candidate` 逻辑） |
| ④ 证据账本 | `verified_registry.py`、阶段 14 指标收集 |
| ⑤ 写作审稿 | 阶段 15–23（`writing/`、`overleaf/`、质量门、引用校验保留） |
| Supervisor | `runner.py` 外包一层，`server/routes/pipeline.py` 改为投递任务 |

阶段编号对外可保留（UI 已按"实验设计 → 方法实现 → 实验验证 → 结果与结论 → 论文整理"展示），内部由实验树驱动。

## 5. 分期落地（每期都可独立交付、可回退）

| 期 | 内容 | 直接收益 | 粗估 |
|---|---|---|---|
| **P0 执行底座** | `rc-worker` 独立服务 + 看门狗自动续跑；文件级断点（已完成文件恢复时复用，不再整体归档重写） | 重启 Web 不断；单文件失败不重来 | 2–3 天 |
| **P1 规格 + 工具式编码** | ExperimentSpec JSON 与静态校验；补丁式编辑 ACI；强制 dry-run | 截断问题根除；规格矛盾前置 | 4–6 天 |
| **P2 实验树 + 棘轮** | 节点账本、git 工作区、draft/debug/improve/ablate、阶段目标 | 迭代不丢进度；best 可追溯 | 5–7 天 |
| **P3 证据账本 + 写作** | Ledger、逐节写作、主张校验、独立审稿 | 论文数字/设置可溯源 | 5–7 天 |
| **P4 构思优化** | 结果回流、多样性、时间留出查重、经验库 | 选题质量 | 3–4 天 |

建议先做 P0 + P1，用原遥感项目（`rc-20261025-145941-c30e99`）真跑通实验，再推进 P2。

## 6. 参考
- AI Scientist v2: https://arxiv.org/abs/2504.08066 ；Towards End-to-End Automation of AI Research: https://arxiv.org/abs/2606.15497
- AIDE: https://arxiv.org/abs/2502.13138 ；Agent Laboratory: https://arxiv.org/abs/2501.04227
- Karpathy autoresearch: https://github.com/karpathy/autoresearch
- Deep Researcher Agent: https://arxiv.org/abs/2604.05854
- Anthropic, Effective harnesses for long-running agents: https://anthropic.com/engineering/effective-harnesses-for-long-running-agents
- Kosmos: https://arxiv.org/abs/2511.02824
- data-to-paper: https://github.com/Technion-Kishony-lab/data-to-paper
- ScientistOne: https://arxiv.org/abs/2605.26340 ；Evidence-Ledger: https://arxiv.org/abs/2607.26512
- ARIS: https://github.com/wanshuiyin/Auto-claude-code-research-in-sleep
- Dolphin: https://arxiv.org/abs/2501.03916 ；InternAgent: https://arxiv.org/abs/2505.16938
- Autonomous Research Agents: A Survey of AI Scientists and the Verification Gap: https://arxiv.org/abs/2608.05179
- AI for Auto-Research: Roadmap & User Guide: https://arxiv.org/abs/2605.18661
