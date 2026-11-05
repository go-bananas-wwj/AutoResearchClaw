# AutoResearch v2：复盘、调研与精简核心（2026-11-05）

计划原文见会话计划；本文件是 P0 交付：对首次端到端运行（`rc-20261025-145941-c30e99`）的客观复盘、对公开自动科研系统的调研，以及 v2 的设计约定。用户已确认的选择：先在本仓库精简核心再接 DeepSeek Harness；独立 AI 导师审阅、用户可随时插话，立项/换方向/开始写作三处须用户确认；英文写作附中文对照稿；继续 InSAR 项目试点；**调研与创新点生成沿用现有方法**。

框架边界（AGENTS.md 规则 3）不变：科研决策由 DeepSeek Flash 产出，宿主只做工具、契约、校验、恢复。

## 1. 首次端到端运行复盘

### 1.1 有效、保留

| 机制 | 本次证据 |
|---|---|
| 调研只用已取得全文；创新点按 IdeaSpark 模板逐个独立生成 + 近邻比较 + 用户选择 | 阶段 23 引用 17/17 核验通过；任务书来自用户在卡片上的选择 |
| 数据方案契约（模型起草，宿主校验逐字引文、工具白名单、预算） | 模型第 3 轮才通过，拿到真实 Sentinel-1 burst 数据 |
| 实验准备 + 数据卡 | 消除"代码找不到数据" |
| 宿主关卡（方法断言、dry-run、数据回执、指标定义域）+ 正式失败回到实现修复 | 拦下 CRPS=−858，修复后得到合法值 |
| 证据账本 + 主张校验 | 论文如实写负结果："不作性能优越性声明" |
| rc-worker + 看门狗 + 提示词哈希缓存 + detached 正式运行 | 重启控制面不打断实验 |

### 1.2 问题（按严重程度）

1. **科学迭代环是断的。**
   - 阶段 14 三个视角分析因裸 `llm.chat` 用默认 4096 上限，在推理开启下 0/3 截断，失败只记 warning，综合在空输入上进行（"可用指标：None"）。
   - 阶段 15 据此和上一轮 −858 的陈旧教训两次判 REFINE。
   - 阶段 13 候选只读顶层文件，缺 `methods/` 子包，`import methods` 被当第三方包 pip 安装，全部失败——REFINE 什么也没改。
   - 达到上限后照样写了论文。CRPS 413 对 SBAS 基线 15.9（差 26 倍），论文自列的三条原因没有一条被检验。
   - 两处缺陷已在 P0 修复（`load_project_files` 递归读取；视角调用走截断翻倍，全部失败即阶段失败）。
2. **框架过重，复杂度花在框架自身。** 约 75 KB 实验代码动用 35 个 mission、160 次派工、62 次驳回；本轮修的 9 个缺陷大半是 Tower 自身记账（审查引文、哈希、scope、竞态），与科学无关。
3. **实验力度不足。** 2 个独立对象、2 折、单次、900 秒；立项时没有成功标准，也没论证基线比较是否公平（SBAS 点预测的 CRPS 退化为 MAE）。
4. **写作。** 没先定期刊（配置 TGRS，产出中文 ctex）；0 张图，框架图是占位；通用 8 节结构；审稿与作者同一模型（`independent=no`），审稿人建议拒稿却无门控处理。
5. **不透明。** 过程散在二十多个 JSON、`comms.jsonl`、活动日志里，没有按轮次可读的汇报；全程 auto_approve，用户最后才看到结果。
6. **陈旧产物污染决策**：阶段 15 引用了上一轮 −858。

## 2. 公开自动科研系统调研

| 系统 | 公开成果（口径） | 关键机制 | 可借鉴 / 教训 |
|---|---|---|---|
| AI Scientist v2（Sakana，arXiv 2504.08066） | 3 篇投 ICLR 2025 workshop，1 篇均分 6.33 过线，按约定撤稿 | 实验管理器 + agentic 树搜索；VLM 审图 | 树搜索与审图可取；外部评审指出引用错误、把已知概念说成新发现 |
| Zochi（Intology） | 自称 ACL 2025 正会录用（Tempest，meta-review 4）；人工只在页数与引用格式上提示 | 闭源商业系统，早期版本 MIT | "人只做格式润色"即可过审的前提是实验扎实；公司自述，非独立核验 |
| AI-Researcher（HKUDS，NeurIPS 2025 Spotlight） | 系统本身是论文；生成论文的评分来自自建基准 | 文献→实现→实验→写作全流程 | 全流程化，评分口径需谨慎 |
| DeepScientist（西湖大学，arXiv 2509.26603） | 自称三项前沿任务超过人类 SOTA | 月级发现循环，贝叶斯优化式"假设—验证—记录"，重点在挑战现有 SOTA 的局限 | 长周期、以失败记录驱动下一轮，正是导师—学生循环需要的 |
| Agent Laboratory（arXiv 2501.04227） | 有人在环（co-pilot）时质量更高 | 每阶段可人工介入 | 关键节点人工确认 |
| Karpathy autoresearch | — | 单一标量指标 + git 棘轮 + `results.tsv` | 极简、迭代快；每次尝试一行账 |
| ARIS | — | Claude Code 技能化；不同模型做对抗审稿 | 审稿须独立 |
| AI-Supervisor（arXiv 2603.24402） | — | 持久研究世界模型，导师式监督 | 最接近导师—学生模式 |
| data-to-paper（Technion） | — | 论文数字可追溯到代码 | 我们的证据账本 |

ResearchArena（arXiv 2605.19156）评估 117 篇 agent 论文：没有一篇达到顶会线；实验力度不足（25–82%）与论文—产物不符（Kimi Code 72–77% 伪造）是主因；直接用编码 agent 加工具的 Claude Code 表现最好；图数与评分相关（最好者平均 4.8 张/篇）；只看稿件的 AI 审稿会奖励包装，必须对照产物审稿。

**对我们的结论**：瓶颈在实验严谨性与迭代，不在多智能体编排。v2 用单 agent + 宿主关卡 + 导师循环，把力气花在"诊断 → 可检验假设 → 最小验证实验"。

## 3. 论文方法论

- **一个核心贡献**（Mensh & Kording, *Ten simple rules for structuring papers*, PLoS Comput Biol 2017）：全文围绕一个信息；每段"背景—内容—结论"；摘要五句：背景、缺口、做法、结果、意义。
- **OCAR 叙事**（Schimel, *Writing Science*）：Opening（领域与重要性）→ Challenge（现有方法的具体缺口）→ Action（我们做了什么）→ Resolution（结果说明了什么，含局限）。
- **科学问题链**：现象 → 现有方法缺口 → 可检验假设 → 方法 → 验证设计（基线、消融、负对照、统计）→ 结果 → 局限。v2 的立项书就按这条链组织，写作直接从立项书和各轮汇报取材。
- 负结果也是结果：若证据支持"机制成立但精度不足"，按机制可行性 + 失效归因写，明确不作优越性声明。

## 4. GRSL 投稿要求（已核实部分）

- 只收英文；IEEEtran 期刊双栏；最多 5 页，超过 5 页不在 GRSL 发表（可转 TGRS/JSTARS）。
- 前 3 页无强制费用；第 4、5 页按页收费（GRSS 官网：非会员 $230/页，会员价以官网为准）。
- 通常 3–5 张图；图注须独立可读；坐标轴用文字标签而非仅符号；IEEE 引文格式。
- 投稿走 ScholarOne（manuscriptcentral.com/grs），类型选 Letters；参加 IEEE 透明评审。
- 未核实：摘要字数上限、Index Terms 数量（按 IEEE 期刊通行做法，摘要 150–250 词、给出 Index Terms）。写作阶段的页数以实际编译 PDF 为准。

## 5. v2 精简核心

```
调研与创新点（沿用现有 ideation/，原样保留）→ 用户选定 → 研究任务书
立项书 charter.json（继承任务书，补期刊、成功阈值、统计设计；用户确认）
研究循环：每轮 plan → 改码 → 关卡 → 正式运行 → 出图 → 汇报；AI 导师审阅 → continue/pivot/write/stop
GRSL 写作（模板 + 方法论骨架 + 图表计划 + 编译数页）→ 主张校验 + 独立审稿 → 用户确认 → Overleaf
```

- **创新点**：检索、全文解读、`idea_first.py` 生成、近邻比较、用户选择全部沿用。v2 只加两处衔接：导师判 pivot 时先从同一调研会话已比较过的候选中选，并带回本项目的实验结论；导师提出的诊断假设须引用已读原文或本项目产物，宿主逐字核对。
- **研究循环产物**：`rounds/round-NN/{plan.md, report.md, review.md, advisor_note.md, figures/}`，`results.tsv` 每轮一行，best 仍经 `ExperimentTree.promote`。
- **停用（配置关闭、代码保留）**：Tower 多 mission、阶段 13–15 线性 REFINE/PIVOT 与多视角分析、MetaClaw 教训注入、阶段 16–21 旧写作链、原生蓝图与 OpenCode。
- **保留**：`ideation/`、`data_acquisition/`、`experiment_prep.py`、`execution_contract.py` 关卡与正式运行、`experiment_tree.py`、`writing/ledger.py`、`writing/translator.py`、`worker/`、缓存、docker-sibling、wsproto、只用 Flash、预算 0、Overleaf 同步。

## 6. 分期

| 期 | 内容 | 验收 |
|---|---|---|
| P0 | 本文档；修阶段 13 子包与阶段 14 空分析 | 测试通过；文档同步 Overleaf |
| P1 | `research_loop/`（charter、round、advisor、journal），`code_agent.backend=loop` | InSAR 项目从 best 起至少 3 轮，每轮可读汇报 + 导师意见，并检验一个诊断假设 |
| P2 | `grsl` 模板、`writing/methodology.py`、图表计划与 `rcstyle.mplstyle`、TeX 编译数页、英译中对照、独立审稿回路 | ≤5 页可编译英文稿，3–5 张真实数据图，主张校验通过 |
| P3 | DeepSeek Harness 插件（工具封装 REST、技能、"AutoResearch 模式"预设） | 在 dsh Trajectory 中看到研究循环全过程 |

## 参考

- DeepSeek Harness: https://github.com/deepseek-ai/deepseek-harness ；创造模式报道：https://cn.technode.com/post/2026-11-03/deepseek-harness-creator-mode-custom-plugins/
- GRSL: https://www.grss-ieee.org/publications/geoscience-and-remote-sensing-letters/
- How Far Are We From True Auto-Research?: https://arxiv.org/abs/2605.19156
- AI Scientist v2: https://arxiv.org/abs/2504.08066 ；Zochi: https://www.lesswrong.com/posts/LtsgfGsXpiLTSGpaW/zochi-publishes-a-paper
- DeepScientist: https://arxiv.org/abs/2509.26603 ；AI-Researcher: https://github.com/HKUDS/AI-Researcher
- AI-Supervisor: https://arxiv.org/abs/2603.24402 ；Agent Laboratory: https://arxiv.org/abs/2501.04227

## 7. 实施状态

### P1 研究循环（2026-11-05 已实现，待真实运行验收）

- 代码：`researchclaw/research_loop/`
  - `charter.py`：立项书起草与校验。任务书字段由宿主逐字带入，模型回复不能覆盖。
  - `round.py`：单轮的计划 → 补丁改码 + `figures.py` → `accept_final_code` 关卡 → detached 正式运行 → 出图 → 汇报（宿主指标表）。
  - `advisor.py`：导师审阅与决策校验；pivot 时从同一调研会话的其他候选中选。
  - `loop.py`：驱动、门控、断点续作，以及循环起点快照 `research_loop/baseline/`。
  - `journal.py`：研究日志视图与用户意见写入。
- 接入：阶段 13 在 `experiment.research_loop.enabled` 时由研究循环接管（write 才进入写作，其余如实结束）；阶段 15 只记录循环决策。
- 网页：项目总览顶部"研究日志"展示立项书、results.tsv、各轮计划/汇报/图/导师意见，可直接提交意见。API 为 `GET /api/runs/{id}/research-loop`、`GET …/research-loop/figure?path=`、`POST …/research-loop/note`。
- 门控：Web 协同模式（`auto_approve=false`）下，立项书、开始写作、换方向三处暂停等用户；不批准时用户意见写入下一轮。

### P2 GRSL 写作（2026-11-05 已实现）

- `researchclaw/research_loop/paper.py`：
  - 方法论大纲：一个核心贡献、OCAR 叙事、摘要五句；每条主张必须指向账本、文献或图。
  - 论文图：模型编写 `paper_figures.py`，在沙箱里从最佳节点的结果画 PDF 图；示意图用 TikZ。
  - 逐节写作与摘要。
  - 检查：真实编译、页数（≤5）、证据账本主张校验，问题回给对应小节修改。
  - 独立 GRSL 审稿人：major 意见回小节修订，最多 2 轮。
  - 中文对照稿：逐节翻译并核对数字，用 xelatex 编译。
- `researchclaw/research_loop/latex.py` + `docker/Dockerfile.latex`：论文编译兄弟容器（IEEEtran、ctex、TikZ），无网络。
- 接入：阶段 16 运行写作器，阶段 17–21 跳过；阶段 22 导出并推 Overleaf en/zh；阶段 23 照常做引用与主张校验。
