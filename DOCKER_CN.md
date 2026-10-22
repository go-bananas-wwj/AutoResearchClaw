# AutoResearchClaw 本地 Docker 部署（V100 单机版）

三个镜像、分工明确：**control**（研究流水线本体）只吃 API；**experiment / sandbox-rs** 是实验沙箱（前者通用，后者预装遥感库），由流水线在 `experiment.mode: docker` 下动态 `docker run` 到 V100 上。控制面不跑任何本地大模型，agent 推理全部走百炼 API（`config.arc.yaml`→`config.template.yaml` 通用模板已配好 OpenAI 兼容端点）。

## 一次性准备

```bash
# 1. 宿主机装 Docker + NVIDIA 容器工具包（需要你的 sudo 密码）
sudo bash scripts/docker-setup.sh
newgrp docker                     # 让 docker 组生效（或注销重登）

# 2. API key
cp .env.example .env              # 填入百炼 DASHSCOPE_API_KEY

# 3. 构建三个镜像（首次约 20-40 分钟，实验镜像 ~12GB）
bash scripts/build-images.sh
```

## 跑研究（任意领域）

```bash
# 通用：编辑 config.arc.yaml（默认软链到 config.template.yaml）的 research.topic，或命令行直接给
docker compose run --rm control run \
  --config config.arc.yaml --topic "任何领域的研究问题" --mode co-pilot

# 遥感等特定领域：用对应预设（换 metric_key/沙箱镜像/domains，见下）
docker compose run --rm control run \
  --config config.rc-remote-sensing.yaml --topic "..." --mode co-pilot
```

流水线本身领域无关——接入新领域只动四个旋钮：`research.topic/domains`（语境）、`experiment.metric_key/direction`（评分）、`docker.image`（依赖栈，通用镜像 `experiment:latest` 之外可自建领域镜像如 `sandbox-rs`）、以及可选的领域 SKILL.md（写作/辩论规范）。

co-pilot 模式会在 Stage 5（文献）、7-8（假设）、9（实验设计）、15（去留）、20（质量门）停下来等你：批准/拒绝/编辑/协作对话。中途离开终端后：

```bash
docker compose run --rm control status  artifacts/rc-xxxx   # 看进度
docker compose run --rm control attach  artifacts/rc-xxxx   # 接管交互
docker compose run --rm control guide   artifacts/rc-xxxx --stage 9 --message "按 tile 划分，禁随机划分"
```

产物在 `./artifacts/rc-xxxx/deliverables/`（宿主机可见），`paper.tex` 直接传 Overleaf 编译。

## Web 仪表盘

```bash
docker compose up -d dashboard      # 常驻，改代码/配置后 docker compose build control 再 up
```

浏览器打开 **http://localhost:8088**（若你是 SSH 远程连这台机器：`ssh -L 8088:localhost:8088 用户@这台机器` 后本地开同款地址）。可看各 run 的 23 阶段进度、实验曲线/图表、论文预览与成本；面板内含 ChatPanel，可在关键节点与流水线对话协作。终端仍是主交互面（co-pilot 提示与 approve/reject/guide）。**右上角"中文/EN"按钮切换界面语言**（默认跟随浏览器，AI 对话内容不翻译）。

🛠 **Settings 页**：在浏览器里直接编辑 `config*.yaml`（保存前 YAML 校验、自动 .bak 备份），并查看各 API key 环境变量是否就绪（只显示有/无，不回显值）。服务运行于完整 `serve` 模式，`/api/pipeline/start|stop` 亦已开放（前端启动按钮暂未接，可直接调 API）。

## 论文自动同步 Overleaf

`overleaf.enabled: true`（两份配置模板均已开）时，每条 run 在收尾（deliverables 打包后）自动把 `paper.tex + references.bib + figures/` 推到共享 Overleaf 项目的 `runs/<run_id>/` 文件夹，全程走 Overleaf Git 集成；也可随时在对话里说「同步到 Overleaf」手动触发。

- 共享克隆在 `.overleaf/papers/`（已 gitignore）；token 走 `.env` 的 `OVERLEAF_TOKEN`，不落配置文件
- 在 Overleaf 打开对应 run 文件夹，把 `paper.tex` 设为 Main document 即可编译
- 导出模板由 `export.target_conference` 决定：遥感默认 `tgrs`（IEEE TGRS，SCI 一区，IEEEtran 双栏，样式文件 TeX Live 自带），亦可切 `neurips_2025 / iclr_2026 / icml_2026`
- 你在 Overleaf 网页端的修改经 git 双向同步，可用对话或 CLI 拉回

## 本目录新增文件

| 文件 | 作用 |
|---|---|
| `docker/Dockerfile.control` | 控制面镜像（python3.11 + researchclaw + docker CLI，uid=1000 对齐宿主） |
| `researchclaw/docker/Dockerfile.remote-sensing` | 遥感实验沙箱：官方 GPU 镜像之上加 rasterio/geopandas/torchgeo/segmentation_models_pytorch |
| `docker-compose.yml` | 控制面编排（源码热挂载 + artifacts 落宿主 + docker.sock 调度沙箱） |
| `config.template.yaml` | **通用配置模板（领域无关）**：`config.arc.yaml` 默认指向它；改 topic 即跑 |
| `config.rc-remote-sensing.yaml` | 遥感领域预设：百炼端点、900s 实验预算、val_miou 指标、V100 显存限额、HITL+成本护栏全开（可复制此文件改成其他领域预设） |
| `.env.example` | key 模板 |
| `scripts/docker-setup.sh` / `scripts/build-images.sh` | 安装与构建 |

## V100 相关说明

- 驱动 580.178.04 仍支持 Volta；官方实验镜像基于 CUDA 12.4.1，PyTorch cu124 轮子覆盖 sm_70，V100 可直接跑。
- `max_parallel_tasks: 1`：单卡串行跑实验，防止显存互踩；租到多卡后再调。
- 磁盘紧张时可先不构建 12GB 的 GPU 主镜像，用轻量 `Dockerfile.generic`（CPU、无 torch）验证流水线连通性，再把 config 的 `docker.image` 指过去。

## 常见故障

| 症状 | 处置 |
|---|---|
| `permission denied ... docker.sock` | 未执行 `newgrp docker`/重登 |
| 容器内 `nvidia-smi` 不可见 | 确认 `sudo bash scripts/docker-setup.sh` 第 3/4 步跑完，`docker info` 中可见 nvidia runtime |
| Stage 4 文献检索极慢 | `.env` 里补 `S2_API_KEY`（Semantic Scholar 免费申请） |
| API 花费超预期 | 已设 `cost_budget_usd: 50` 自动暂停；重跑时调低 `primary_model` 档位 |

## 合规提醒

产出的 `paper.tex` 用于投稿时，请按目标会议（ICLR/ICML 2026 等）政策如实披露 AI 辅助使用情况；本系统定位是研究助理，作者栏只列对内容负责的人。

### 文献卡片、全文与 OpenAlex Key（本 fork 适配）

相关文献按页展示卡片；点击可查看科学问题、方法、优势与局限、出版信息及原文获取记录。初筛概览只依据摘要，全文精读后展示正文记录。首轮只需确认方向；详情中的“从相关文献中删除”用于纠正不相关结果，会撤销该版本的下游批准，返回方向确认。

服务器 `.env` 可配置 `OPENALEX_API_KEY`（[申请入口](https://openalex.org/settings/api)）。检索、元数据解析与 OpenAlex 缓存全文下载共用此配置；未配置时仍会尝试出版社、公开仓储与 arXiv 正文。OpenAlex 只对有 `content_urls.pdf` 的记录提供缓存全文，Key 不能解锁所有出版商付费内容。[官方全文说明](https://help.openalex.org/access/fulltext/)。

修改 `.env` 后，先确认没有科研或调研任务运行，再执行 `docker compose up -d --force-recreate dashboard`（单纯 restart 不会更新容器环境）。不要把 Key 写入 YAML、Git 或聊天。服务器只将清除查询凭据后的来源地址与分类结果写入获取记录。

论文概览参考 IdeaSpark 将瓶颈与未解决问题分开的调研记录方式（[retrieval-runbook](https://github.com/microsoft/ResearchStudio/blob/main/ResearchStudio-Idea/skills/idea_spark/references/retrieval-runbook.md)），本系统另以“科学问题→方法→优势→局限”组织展示。页面“生成方式与提示词”返回正在使用的实际模板，包括摘要概览与全文精读，不能将摘要未说明的事项认定为研究缺口。

2026-10-22 验证记录：科研、调研与交接相关回归测试 381 项通过，JS 语法与 Python 编译通过；实际服务接口正常，`/ws/events`、`/ws/chat` 均返回 101。DOM 模拟验证了 13 篇论文的三页卡片、摘要论文弹窗、移除与版本失效、即时反馈、精读进度及独立创新点批准。LS 因子会话 `is-6d5924d11be24da58dc7` 已有 3 篇核心论文通过全文精读与引文校验，继续自动调研，未代替用户确认创新点。尚未完成原生浏览器视觉验收；服务器未配置 OpenAlex Key，缓存全文鉴权仅完成模拟验证。
