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
- `researchclaw/ideation/brief.py`：《研究任务书》注入通道——`materialize()` 把人工确认的 6 件事物化为 stage-01/02/07/08/09 产物 + `run_dir/research_brief.json`，REST `PipelineStartRequest.brief/from_stage` 从中间阶段开工；Chat BRIEF flow（`dialog/router.py` 步骤机，`session.pending` 持久化）。
- **docker-sibling 硬件检测**：stage-01 在 `experiment.mode=docker` 且 `docker.gpu_enabled=true` 时信任配置标 GPU 可用（控制面容器看不到 GPU 是常态）；`_experiment_design.py` 的硬件档案读 `stage-01/hardware_profile.json`，不要硬编码 GPU 型号。
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
