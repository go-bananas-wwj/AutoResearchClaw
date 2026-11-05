#!/usr/bin/env bash
# 构建四个镜像：官方 GPU 实验沙箱 → 遥感沙箱 → 论文编译 → 控制面
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== [1/4] 官方实验沙箱（GPU，torch/timm/kornia 全栈；约 12GB+，耗时 10-30 分钟）=="
docker build -t researchclaw/experiment:latest researchclaw/docker/

echo "== [2/4] 遥感沙箱（加 rasterio/geopandas/torchgeo/mmseg 生态）=="
docker build -f researchclaw/docker/Dockerfile.remote-sensing \
  -t researchclaw/sandbox-rs:latest researchclaw/docker/

echo "== [3/4] 论文编译镜像（v2 写作：IEEEtran/ctex 真实编译与数页）=="
docker build -f docker/Dockerfile.latex -t researchclaw/latex:latest docker/

echo "== [4/4] 控制面（researchclaw 流水线本体）=="
docker compose build control

echo
echo "全部就绪。启动一次 co-pilot 研究："
echo '  docker compose run --rm control run \'
echo '    --config config.rc-remote-sensing.yaml --topic "你的研究问题" --mode co-pilot'
