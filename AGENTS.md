# AGENTS.md — 本仓库（AutoResearchClaw 个人 fork）协作约定

上游：`aiming-lab/AutoResearchClaw`；本 fork：`go-bananas-wwj/AutoResearchClaw`，工作分支 `docker-setup`（git remote 名 `fork`）。

## 硬性规则

1. **每次修改代码后，必须 git commit 并推送到 GitHub 仓库**（`fork` / `docker-setup`），不允许只改本地不提交。一轮工作内多处相关改动可以合并成一个 commit，但会话结束前工作区必须是干净的。
2. commit message 用中文，写清"改了什么、为什么"；对上游文件的修改要在 message 里注明是本地适配（如国内网络、Volta/V100、上游 bug 的 workaround）。
3. 密钥纪律：`.env`、API key、token 一律不得进入 commit / push / 日志输出；配置文件只提交模板（`*.example.yaml`、`config.template.yaml`、`config.rc-remote-sensing.yaml` 等不含密钥的文件可以提交）。

## 本 fork 的本地适配点（不要"顺手改回"上游行为）

- `docker/Dockerfile.control`：控制面镜像（含 fastapi/uvicorn/**wsproto**）。上游 `[web]` extra 缺 fastapi 是已知问题。
- `researchclaw/cli.py`：`uvicorn.run(..., ws="wsproto")` —— websockets≥14 强制 Origin 校验导致 WS 403，本工具按单人本地场景换用 wsproto；多用户部署请配置 `server.auth_token` 而不是回退。
- `researchclaw/docker/Dockerfile`：烘焙数据集层默认关闭（`EMBED_DATASETS`）。
- 前端 `frontend-legacy/`：已接为服务路径 `/app/frontend`（上游目录名不匹配是已知问题）；含 i18n（`src/services/i18n.js`，右上角 中文/EN）与 Settings 页（对应后端 `researchclaw/server/routes/settings.py`）。**2026-10-09 起为 Codex 式项目中心布局**：左侧项目列表（一项目一论文），右侧 `ProjectHome.js` 三 Tab（总览/交互/管理），交互 Tab 是对话驱动流程（门控卡片 + 论文批注区内联）；旧功能导航视图（Dashboard/Pipeline/GateConsole 等 9 个组件）已全部删除，不要按旧结构找回它们。
- `researchclaw/overleaf/`：论文自动同步共享 Overleaf 项目（每 run 一个 `runs/<run_id>/`）。token 走 `.env` 的 `OVERLEAF_TOKEN`；`.overleaf/` 共享克隆已 gitignore；**不要把 Overleaf token 写进任何配置文件提交**。模板 `tgrs`（IEEE TGRS）加在 `templates/conference.py`（author_format `ieee` 分支），IEEEtran 样式随 TeX Live，无需下载。
- **docker-sibling 部署**：serve/control 容器经 `/var/run/docker.sock` 调度实验沙箱兄弟容器。两条铁律：① `DockerSandbox._host_visible()` 依赖 `.env` 的 `RC_HOST_REPO`（宿主机仓库真实路径）做 bind-mount 路径翻译——**删掉它实验沙箱就会挂载空目录**；② serve 与 control 两个服务都必须挂 docker.sock + `group_add` docker GID。
- `PipelineStartRequest.auto_approve` 默认 False（Web 启动=co-pilot）；Chat 显式传 True（全自动）。门控走 REST `/api/runs/{id}/hitl/{waiting,respond}` + hitl 文件 IPC。**Web 启动现已真正挂 HITLSession**（2026-10-08 修复：此前 Web run 门控不真停、waiting.json 不写出），co-pilot 预设下阶段 1/2/3/10/13/16/18/23 也会暂停等人，门控 5/9/20 必须人工放行。
- dashboard 当前 `serve --config config.rc-remote-sensing.yaml`（端到端首跑切到遥感预设；改回通用模板把 compose command 改回 `config.arc.yaml`）。
- `researchclaw/reproduce/`：论文复现模块（finder/runner/compare/report），REST `/api/reproduce/*` + Chat「复现」意图 + 前端选题卡「复现这篇」按钮；产物在 `artifacts/<run_id>/reproduction/<slug>/`，`repo/` 里 `rc_` 前缀文件是复现凭证，当 baseline 用时排除。
- `researchclaw/ideation/landscape.py`：调研讲解层（2026-10-10 新增）——`latest_ideation_run`/`load_shortlist`（stage-05 短名单，回退 stage-04 候选）/`build_landscape`（领域图景中文叙事，缓存 `survey_landscape_zh.md`）/`critical_reading`+`save_paper_note`（精读笔记落盘 `paper_notes/`）。Chat 引导链路：「找选题：方向」→「讲一下现状」→「精读第 N 篇」（PAPER_READ 意图）→「复现第 N 篇」（短名单编号解析，优先 arxiv_id/DOI）→「开始确认」（复现报告 issues/opportunities 会作为接地材料进入任务书起草 seed）。
- `researchclaw/ideation/brief.py`：《研究任务书》注入通道——`materialize()` 把人工确认的 6 件事物化为 stage-01/02/07/08/09 产物 + `run_dir/research_brief.json`，REST `PipelineStartRequest.brief/from_stage` 从中间阶段开工；Chat BRIEF flow（`dialog/router.py`）2026-10-10 起改为 **plan-mode 整稿审阅**：进入即一次性 LLM 起草全部 6 槽位（AI 推断字段带标注），用户回复修改意见整稿迭代/「重新生成」/「确认」启动，不再逐槽位问答；选题证据卡/复现基线预填为权威材料硬覆盖。
- **docker-sibling 硬件检测**：stage-01 在 `experiment.mode=docker` 且 `docker.gpu_enabled=true` 时信任配置标 GPU 可用（控制面容器看不到 GPU 是常态）；`_experiment_design.py` 的硬件档案读 `stage-01/hardware_profile.json`，不要硬编码 GPU 型号。
- **LLM 端点与推理开关（2026-10-20）**：`llm.base_url` 切到 DeepSeek 官方 `https://api.deepseek.com/v1`，key 走 `.env` 的 `DEEPSEEK_API_KEY`（**不要提交**）；主模型 `deepseek-flash`、兜底 `deepseek-v4-pro`。DeepSeek V4 是推理模型：默认先把 completion tokens 花在 `reasoning_tokens` 上，`max_tokens` 小于思考量时 `content` 直接为空（阶段表现为"解析失败/空产物"）——配置必须带 `llm.thinking: "disabled"`（要深推理时改 `"enabled"` 并放大 max_tokens）。官方 `api.deepseek.com` 的JSON调用应发送 `response_format={"type":"json_object"}`，不要沿用第三方代理不支持JSON模式的旧判断；`metaclaw_bridge.proxy_url` 同指该端点，否则默认 `localhost:30000` 不可达、每次调用先失败再回退。
- **两轮审核选题（2026-10-21）**：新入口改为“文献调研与问题定位”。`/api/ideation/sessions` 持久化调研会话至 `artifacts/ideation/is-*/`：检索筛选→人工审文献→全文精读/缺口/构思/独立查重→人工审创新点→统一 `pipeline/start` 从 `EXPERIMENT_DESIGN`（9）执行到 `CITATION_VERIFY`（23）。确认前不创建正式 `rc-*` 项目。旧 `/ideation/cards|confirm` 必须引用 session_id/revision/card_id，禁止恢复无证据的方向直生卡/任意卡直启实验。Chat“找选题”绑定同一会话；旧 id-* 报告保持只读。文献变更使下游失效，必读缺全文/少于3篇核心全文/伪引用/零查重/截断均暂停；不凑论文或假数据。提示词在 `ideation/prompts.py`；server-owned 状态与证据在 `store.py/service.py/sources.py`。默认精读10篇、最多15篇；内置兼容模式每轮最多4张卡片，官方IdeaSpark模式默认12个独立构思任务；模型使用请求内 P/E 短编号，服务端还原内容哈希ID并校验原文。会话与后续科研成本按保守预留（输入$5/百万、输出$25/百万tokens，非账单）合并记账，保留用量记录；该部署按用户2026-10-23要求将 `hitl.cost_budget_usd` 与 `experiment.cli_agent.max_budget_usd` 均设为0（不限预算），不要恢复50/5美元上限；`cost_tracker.py` 为运行上下文隔离，不能回退到跨项目全局计费。控制面镜像安装 `[pdf]` 支持上传PDF正文提取；Debian固定bookworm与Docker源一致。OpenAlex未配置自己的email时省略公共默认mailto，避免所有安装共用额度池；保留显式api_key/email，并在通用检索源不可用时以Crossref出版元数据后备。

- **首轮方向确认交互（2026-10-21）**：用户只核对调研文献是否属于预期研究方向，不承担逐篇选读或精读任务。前端取消“保留/必读”勾选，`POST /ideation/sessions/{id}/direction-confirm` 只需 revision/可选 feedback，由服务端智能体选择相关论文并继续精读、缺口提取和构思/查重。主界面展示“用户输入”和一句话“研究对象”，不显示预算、版本、检索日志等过程小字。文献展示真实期刊/会议、作者、论文发表时机构和一段总结；元数据缺失写“未提供”，禁止按作者当前机构或模型知识猜填。后台 `metadata` 更新只补出版信息，不批准方向、不启动精读。旧逐篇审核接口保留兼容，但不再是页面默认交互。
- **全文文献与通俗解读（2026-10-23）**：`screen` 先做相关性筛选，`summarize` 只接收已取得的完整原文；科学问题、做法、作用与不足由 DeepSeek 面向零基础读者撰写，程序不得按题名拼接或回退到摘要总结。原文不可读、过长、无方法/论证依据或不相关的论文不进入首轮卡片/方向确认；候选中的替补也受最多60篇边界约束，不凑篇数。`selection` 保存正文相关性、方法/验证判断、具体推荐理由及逐字匹配的原文短引文；引用量、年份、发表平台不直接等于质量。首轮显示“已读全文 · 概览完成”，方向确认后进行“论文比较与原文证据核对”，不能再显示误导性的“待精读”。新增论文必须重新整理全文；推荐范围变化使旧批准/卡片失效。模型段落原样保存，材料/提示词哈希支持断点续作。新调研默认20篇，可选10/20/30，精读内部仍10/最多15。实现与参考见 `docs/IDEATION_SUMMARIES_CN.md`。
- **全文真实性与缓存修复（2026-10-23）**：`sources.readable_document` 对HTML要求实质正文章节及段落，摘要/题录/参考文献堆积不能凭长度和“方法”关键词冒充全文；落地页不合格时继续追踪PDF；PDF/HTML还必须在前部正文中匹配题名、DOI或arXiv编号，不能把参考文献链接的PDF关联到请求论文。用户上传材料按服务器校验的内容哈希绑定。旧不合格HTML缓存隔离到 `rejected_fulltext/`，重新获取正文；扫描PDF提取不到文本则不入选。`_summary_material`、缓存精读复用和创新点确认都检查正文可读性。来源修复改变文献范围时，历史保存后撤销旧批准与候选，返回首轮方向确认；`curation_candidate_ids` 可锁定修复范围，避免悄悄补入用户未确认的新文献。
- **论文详情与检索路径（2026-10-23）**：全文解读为8个模型字段：problem/background/innovation/method/data/results/strengths/limitations，页面分为“提出的科学问题、研究意义与研究现状、创新点、方法（含数据和效果）、方法的优点和缺点”。技术精读记录不得覆盖通俗概览；已批准文献可更新解读，批准版本、范围与证据保持原记录。新建/重新调研采用经典检索（无起始年份限制）+综述/主题检索+近期检索，并从最多3篇固定种子做一轮前后向引用追踪；预留候选名额且仍受60篇总上限约束，扩展结果必须重新筛选。材料在会话 `related work/` 中保留可用PDF、提取文本和索引；未取得LaTeX源码时明确记为空，不冒称源码。参考用户粘贴的AI科研工作流，见 `docs/IDEATION_SUMMARIES_CN.md`。
- **中文先行**：`export.paper_language`（默认 `zh`，置 `en` 恢复上游英文行为）。zh 时阶段 16/17/19 用中文指令产中文论文（章节名 摘要/引言/相关工作/方法/实验/结果/讨论/局限性/结论），阶段 18/20 评审 prompt 追加中文评审要求，阶段 22 强制 `ctex` 模板（`templates/conference.py` 的 `CTEX`，ctexart+UTF8，pdflatex/xelatex 均可编译，Overleaf 默认 pdflatex 直接能编）生成 paper.tex 并另存 paper_zh.tex。长度统计走 `researchclaw/utils/text_length.py` 的 `count_words`（zh=CJK 字符+非 CJK 词，1 词≈2 字），阈值按 `scale_word_targets` 同步换算；不要在这些路径回退到 `len(text.split())`。
- **门控控制台**：项目中心布局下，门控操作在「项目 → 交互 Tab」的门控卡片里做（`ProjectHome.js`）——waiting 时卡片插在对话流顶部：产物全文（`GET /runs/{id}/hitl/file?path=`，run_dir 沙箱）、干预历史（`/hitl/interventions`），操作 approve/reject/skip/abort/inject/edit/rollback（respond 已扩展 `edited_files`/`rollback_to_stage`）；`GET /api/hitl/waiting` 聚合所有等待中的 run。论文批注改稿区同处交互 Tab（见下）。
- **Overleaf 双向同步 + 批注改稿循环**：push 按语言分目录 `runs/<id>/zh/`、`runs/<id>/en/`（`sync_run_to_overleaf(language=)`，en 自动推 paper_en.tex；推送保持图表源目录名 charts/，改回 figures/ 会断链）。pull 已是真功能：`POST /api/runs/{id}/overleaf/pull` 或 Chat「拉取 Overleaf 改动」→ 拷回 `paper_annotations/<lang>/`。批注格式：Overleaf 里写 `% 批注: 意见` 注释行或直接改正文。`researchclaw/writing/`（annotations 解析 + PaperReviser 改稿 + PaperTranslator 翻译）：`POST /paper/revise` 按批注改稿（VerifiedRegistry 白名单硬约束，REJECT 数字找回或换 `---` 不静默放行，已处理批注行会删掉防重复命中），`POST /paper/translate` 中文定稿→英文 IEEEtran（数字多重集对照增删必报）。版本快照 `paper_versions/<lang>/vN.tex + base.tex`（diff 基准）。

## 端到端首跑（2026-10-08/09，rc-20261008-143654-557def）固化经验

- **恢复中断的 run**：`docker exec rc-dashboard python3 <驱动脚本> <STAGE_NAME>`（驱动 = 构造 RCConfig + HITLSession（无回调→文件 IPC）+ `execute_pipeline(from_stage=...)`；注意 `dataclasses.replace` 覆盖 `config.research.topic` 为 run 真实题目，否则对题检查拿配置占位符误判）。杀驱动进程要在容器内 kill（本地杀 docker exec 客户端不影响远端）。
- **陈旧产物污染是头号敌人**：PIVOT/回滚后，`_read_prior_artifact`、`_promote_best_stage14`、`_collect_raw_experiment_metrics` 都可能命中旧迭代（`_vN`）数据。改代码时优先"当前迭代（非版本化目录）"语义；人工修数据后要归档旧 `stage-14_vN` 防止按数值提升假数据。
- 阶段 12 硬失败已有自动修复重试（runner `_repair_and_retry_experiment_run`，最多 2 次）；修复循环产物在 `stage-14_repair_vN/`，重试代码回写 `stage-10/experiment/`（原件备份 `experiment_broken_backup/`）。
- 质量门（阶段 20）会正确拦截数字不一致，但回滚目标 PAPER_OUTLINE 不含阶段 14——数据修好后要手动从 RESULT_ANALYSIS 恢复。
- 已知待办：pdflatex/xelatex 未装（无本地 PDF，编译验证交给 Overleaf 端）、matplotlib 缺（无图表）、GitHub code search 401、CLI 无 TTY 时 input() EOF 被当"放弃"、PIVOT 重跑不继承 hitl_guidance、文献限流（S2/OpenAlex 建议配 key）。
- **已知待办（消毒误伤）**：`_sanitize_fabricated_data` / `_enforce_verified` 的白名单只有实验指标值（mIoU 等），超参/设置数字（学习率、batch size、种子、分辨率、时间预算）会被误替换为 `---`——改稿循环里用户让 AI 把这些填回去也会被拦。正确修法：VerifiedRegistry 增采 stage-09 exp_plan.yaml / stage-10 实验代码里声明的设置值（涉及核心结构，待与上游语义对齐后再动）。
- **LLM 全文改写的两个坑**（2026-10-09 zh 重跑实测固化）：① 全文改稿/翻译必须按文档规模给 max_tokens（默认 4096 必截断），且要有截断守卫（`writing/reviser.py` 的 `_output_budget` + 80%/70% 阈值拒写）；② qwen 弱指令遵循会把 prompt 里的 evolution overlay（lessons + `~/.metaclaw/skills/arc-*`）当正文输出，阶段 22 有 `_strip_evolution_dump` 机械兜底，新加 LLM 全文改写路径时两类防护都要带上。

## 生效方式速查

| 改动位置 | 生效方法 |
|---|---|
| `frontend-legacy/**` | bind mount 热生效，浏览器硬刷新即可；无需重建 |
| `researchclaw/**`（python 源码） | `docker compose restart dashboard`（editable 安装指向挂载源码） |
| `docker/Dockerfile.control` 的依赖层 | `docker compose build control && docker compose up -d --force-recreate dashboard` |
| 实验沙箱镜像 | `bash scripts/build-images.sh`（或单独 docker build） |

## 验证习惯

改 JS 跑 `node --check`；改 Python 跑 `python3 -m py_compile`；改服务端 API/WS 后 curl 实测（`/api/settings/files`、WS 握手应返回 101）。做不到真机验证的项，在汇报里明说是哪个环节未验证。

- **单篇上下文与长论文（2026-10-24）**：概览、read/read_part/read_merge调用强制单篇paper_id，独立消息不拼入其他论文历史。DeepSeek官方当前1M上下文，按UTF-8字节保守扣除输出/提示词余量，不恢复18万字符硬截断。超容量自动分段阅读及同篇汇总，reading_parts按材料/提示词哈希保存检查点；引用由原文ID解析，显式虚构引文仍拒绝。领域综合以路线、方法表格、缺口展示，完整分析折叠；旧数据直接兼容，程序不生成科学总结。

- **调研局部失败（2026-10-24）**：单篇附加近邻下载/阅读失败保存具名论文与来源记录，普通替代对照可略过，same_claim/same_mechanism或候选明确依赖的关键论文缺正文仅搁置该候选，继续其余方案；有合格方案即进入审核，全无通过时按既有上限尝试修订后汇总。失败不冒充科学否决或新颖性通过。运行时不打断，结束后按论文合并处理记录；同一次运行复用相同来源组合失败记录，显式恢复允许重试。新错误context包含论文、操作、来源和影响，旧错误可从真实下载记录补全。

- **官方 IdeaSpark 批量构思（2026-10-24）**：用户要求实际调用skill并提供更多卡片。`external/ideaspark/`固定微软ResearchStudio提交`063f10088af49909e1a4a7ea8d29efe19633f34e`，原始113个文件由UPSTREAM.json校验，不改原始执行器。宿主`ideation/ideaspark.py`导入已批准且已读的真实文献，Phase 1使用官方诊断提示词，Phase 2–4遵循官方导航器与prepare/model/record记录。`llm.ideation_backend=ideaspark`、`ideation_card_target=12`（1–20）；每个任务独立目录、独立模型上下文，先生成草稿池再逐个审查，不为数量凑方案。主模型与兜底链沿用DeepSeek；官方推理阶段单独开启thinking并扩大输出，不修改其他科研阶段的thinking配置。只有缺少的传输request_id可由宿主绑定到同一次请求，科学产物保持原样，错误ID/过期输入仍拒绝。模型声称的算术执行由无网络只读CPU容器实际复核，不运行训练或正式实验。草稿/needs_work/FAILED VALIDATION卡片可浏览但不可确认；确认时重查官方发布记录、内容哈希、本系统方法正文及原文版本，再交接原有阶段9–23。旧内置流程与历史记录保留。

- **用户指定的构思简化（2026-10-25，覆盖上一条的默认执行深度）**：确认前只做“生成创新点 → 检索与比较相近研究 → 用户选择”。宿主 `ideation/idea_first.py` 读取官方选题/构思模板及模式资料，结合已保存论文解读生成想法；不运行官方 Phase 2–4 多层机制、公式、可实施性、修订、发布审查。旧官方原始文件和完整执行器不修改，旧记录可读。每个想法独立上下文，沿用最多3路并行及默认关闭thinking。近邻最多2个检索式，每个来源单次15秒请求；失败如实标partial，不能冒称新颖性已通过，也不阻断其余想法或要求用户补短板。`idea_ready/selection_ready` 表示可选择，区别于旧 `validated`；服务端核对本版本候选哈希、近邻检查记录和原文版本后交接阶段9。具体技术、数据可得性、基线/消融和实验验证在选定后由实验设计及后续阶段落实；选定前不运行算例，不要求数据入口已经确定，不创建正式项目。已有草稿直接复用，旧技术错误不作为科学否决；未验证的效果、重合风险和检索缺失随任务书一起传递，不伪造通过记录。
