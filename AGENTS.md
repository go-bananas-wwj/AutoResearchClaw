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
- 前端 `frontend-legacy/`：已接为服务路径 `/app/frontend`（上游目录名不匹配是已知问题）；含 i18n（`src/services/i18n.js`，右上角 中文/EN）与 Settings 页（对应后端 `researchclaw/server/routes/settings.py`）。

## 生效方式速查

| 改动位置 | 生效方法 |
|---|---|
| `frontend-legacy/**` | bind mount 热生效，浏览器硬刷新即可；无需重建 |
| `researchclaw/**`（python 源码） | `docker compose restart dashboard`（editable 安装指向挂载源码） |
| `docker/Dockerfile.control` 的依赖层 | `docker compose build control && docker compose up -d --force-recreate dashboard` |
| 实验沙箱镜像 | `bash scripts/build-images.sh`（或单独 docker build） |

## 验证习惯

改 JS 跑 `node --check`；改 Python 跑 `python3 -m py_compile`；改服务端 API/WS 后 curl 实测（`/api/settings/files`、WS 握手应返回 101）。做不到真机验证的项，在汇报里明说是哪个环节未验证。
