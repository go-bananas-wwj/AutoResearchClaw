# AutoResearchClaw 本地 Docker 部署（V100 单机版）

三个镜像、分工明确：**control**（研究流水线本体）只吃 API；**experiment / sandbox-rs** 是实验沙箱，由流水线在 `experiment.mode: docker` 下动态 `docker run` 到 V100 上。控制面不跑任何本地大模型，agent 推理全部走百炼 API（`config.rc-remote-sensing.yaml` 已配好 OpenAI 兼容端点）。

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

## 跑研究

```bash
# 编辑 config.rc-remote-sensing.yaml 的 research.topic，或直接在命令行给：
docker compose run --rm control run \
  --config config.rc-remote-sensing.yaml \
  --topic "用边缘保持先验改进小样本遥感地物分割" --mode co-pilot
```

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

浏览器打开 **http://localhost:8088**（若你是 SSH 远程连这台机器：`ssh -L 8088:localhost:8088 用户@这台机器` 后本地开同款地址）。可看各 run 的 23 阶段进度、实验曲线/图表、论文预览与成本；面板内含 ChatPanel，可在关键节点与流水线对话协作。终端仍是主交互面（co-pilot 提示与 approve/reject/guide）。

## 本目录新增文件

| 文件 | 作用 |
|---|---|
| `docker/Dockerfile.control` | 控制面镜像（python3.11 + researchclaw + docker CLI，uid=1000 对齐宿主） |
| `researchclaw/docker/Dockerfile.remote-sensing` | 遥感实验沙箱：官方 GPU 镜像之上加 rasterio/geopandas/torchgeo/segmentation_models_pytorch |
| `docker-compose.yml` | 控制面编排（源码热挂载 + artifacts 落宿主 + docker.sock 调度沙箱） |
| `config.rc-remote-sensing.yaml` | 遥感配置模板：百炼端点、900s 实验预算、val_miou 指标、V100 显存限额、HITL+成本护栏全开 |
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
