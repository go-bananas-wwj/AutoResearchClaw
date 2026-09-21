#!/usr/bin/env bash
# AutoResearchClaw 宿主机 Docker 环境安装（V100 / Ubuntu 22.04 / driver 580）
# 用法：sudo bash scripts/docker-setup.sh   （需要密码，故由用户亲自执行）
set -euo pipefail

echo "== 1/4 Docker Engine + Compose 插件 =="
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
else
  echo "docker 已安装：$(docker --version)"
fi
docker compose version || echo "!! compose 插件缺失，请检查 get.docker.com 安装日志"

echo "== 2/4 将 wwj 加入 docker 组（免 sudo 用 docker）=="
getent group docker >/dev/null
usermod -aG docker wwj
echo "已加入。当前会话生效需：newgrp docker 或重新登录"

echo "== 3/4 NVIDIA Container Toolkit（把 V100 暴露给容器）=="
if ! command -v nvidia-ctk >/dev/null 2>&1; then
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  distribution=$(. /etc/os-release; echo "$ID$VERSION_ID")
  curl -fsSL "https://nvidia.github.io/libnvidia-container/${distribution}/libnvidia-container.list" \
    | sed "s#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g" \
    | tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
  apt-get update
  apt-get install -y nvidia-container-toolkit
fi
nvidia-ctk runtime configure --runtime=docker
systemctl enable --now docker
systemctl restart docker

echo "== 4/4 验证：容器内可见 GPU =="
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi | head -12

cat <<'EOF'

完成。下一步（不要 sudo）：
  newgrp docker          # 或注销重登
  cd ~/AutoResearchClaw
  cp .env.example .env   # 填入百炼 key
  bash scripts/build-images.sh
EOF
