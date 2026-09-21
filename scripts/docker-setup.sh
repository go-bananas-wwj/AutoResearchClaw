#!/usr/bin/env bash
# AutoResearchClaw 宿主机 Docker 环境安装（Ubuntu 22.04 国内网络实测版）
# 用法：sudo bash scripts/docker-setup.sh
# 注：download.docker.com / registry-1.docker.io 在国内直连常不通，
#     本脚本用 Ubuntu 官方源包 + 公共 registry 镜像加速，实测可用。
set -euo pipefail

echo "== 1/3 安装 docker.io + compose v2 插件 + nvidia-container-toolkit（Ubuntu 源）=="
DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io containerd docker-compose-v2 nvidia-container-toolkit
systemctl enable --now docker

echo "== 2/3 配置：免 sudo 用户 + NVIDIA runtime + 国内 registry 镜像 =="
getent group docker >/dev/null && usermod -aG docker "${SUDO_USER:-$USER}" || true
nvidia-ctk runtime configure --runtime=docker
python3 - <<'EOF'
import json
p = "/etc/docker/daemon.json"
d = json.load(open(p)) if __import__("os").path.exists(p) else {}
d["registry-mirrors"] = ["https://docker.m.daocloud.io", "https://docker.1ms.run", "https://docker.xuanyuan.me"]
json.dump(d, open(p, "w"), indent=2)
EOF
systemctl restart docker

echo "== 3/3 验证 GPU 容器 =="
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi --query-gpu=name,memory.total --format=csv,noheader

cat <<EOF

完成。当前会话生效 docker 组：newgrp docker（或注销重登）。
下一步：
  cp .env.example .env   # 填入百炼 key
  bash scripts/build-images.sh
EOF
