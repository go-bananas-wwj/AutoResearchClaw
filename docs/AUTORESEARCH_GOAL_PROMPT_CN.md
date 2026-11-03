# 目标式任务提示词：让 AutoResearch 端到端跑通

> 用法：在新的 Claude Code 会话（本仓库目录）里整段粘贴「提示词正文」。它描述的是**要达到的结果和怎样算完成**，不规定每一步怎么写代码；执行者自己规划、自己验证、按期汇报。

---

## 提示词正文

### 目标
把本仓库（AutoResearchClaw 个人 fork，分支 `docker-setup`）从“实验做一半就断”改造成能端到端跑通的自动科研系统，并用真实项目证明：

> 以项目 `rc-20261025-145941-c30e99`（《把相位模糊当作分布：让滑坡加速段留在InSAR估计里》，主指标“留一滑坡CRPS”，方向 minimize）为例，从阶段 9 开始，一路跑到产出可在 Overleaf 编译的中文论文 `paper.tex`；中途重启 dashboard 容器或 worker 进程也不丢进度，自动从断点继续。

架构方向已经定好，按 `docs/AUTORESEARCH_ARCHITECTURE_CN.md` 的 P0→P3 推进（P4 构思优化本轮不做）。先读这份文档和 `AGENTS.md`，二者是硬约束。

### 完成标准（全部满足才算完成，每条都要有可复查的证据）
1. **不怕重启**：流水线在独立 worker 中运行；实验进行中执行 `docker compose restart dashboard`，实验继续；kill worker 后由看门狗自动从最近完成的节点续跑，不需要手动驱动脚本。附上实测日志。
2. **不怕截断**：代码通过“补丁式小步编辑 + 运行反馈”生成，单次模型调用截断只重试那一小步；已通过检查的文件/节点在恢复时复用，不再整体重写。项目运行全程不再出现“阶段 10 因单文件截断整体失败”。
3. **实验可信**：ExperimentSpec JSON 是指标/方向/数据/方法清单的唯一真相源，阶段 9 静态校验通过；每个实验节点依次通过 语法/导入 → `method_checks.py` → dry-run → 正式运行 → 方法审查与数据哈希验收；`nodes.jsonl` 记录每次尝试，best 只在全部通过且主指标改进时更新。
4. **论文可溯源**：论文中每个数值、超参数、引用都能在证据账本里找到来源（节点/commit/文件/行号或文献原文）；主张校验器报告零条无证据主张；超参不再被替换成 `---`。
5. **诚实**：删除或改为显式失败所有“静默造假”回退（如 `_code_generation.py` 的合成 numpy `main.py`、写作中的占位文本）。不得把合成数据写成真实遥感验证，不得换成通用模型。
6. **真实数据**：实验用真实公开数据完成，数据获取本身是流水线的一个可续跑、可验证的步骤（见下方“数据获取”）。只有在真实数据确实无法取得并已向我报告后，才允许退回同方法的合成 pilot，且论文与总览必须标注“合成数据 pilot，外部效度未验证”。
7. **回归不坏**：现有测试全部通过（新增功能各自有测试）；构思（ideation）流程、HITL 门控、Overleaf 同步、前端项目中心照常工作。

### 数据获取
本机磁盘约 780 GB 可用，存储不是瓶颈；数据放在仓库外的固定数据根目录（例如 `/home/wwj/rc-data/`，加入 `experiment.data_roots` 并挂载进实验沙箱），不进 git。

- **首选 Microsoft Planetary Computer**（STAC API `https://planetarycomputer.microsoft.com/api/stac/v1`，用 `pystac-client` + `planetary-computer` 签名，匿名可用）。可直接取：Copernicus DEM（`cop-dem-glo-30`，用于去地形相位）、Sentinel-1 GRD/RTC（`sentinel-1-grd` / `sentinel-1-rtc`，仅幅度，可做辅助/掩膜）、ESA WorldCover 等辅助层。先查询 STAC catalog 确认集合实际存在再用，不要凭记忆写集合名。
- **注意相位数据缺口**：本方法需要缠绕相位与相干性，而 Planetary Computer 目前不托管 Sentinel-1 SLC 或干涉图（先用 STAC 搜索核实这一点）。相位数据的候选来源：ASF HyP3 按需 InSAR（`INSAR_GAMMA`，勾选包含 wrapped phase，产物含相干性）或 ARIA S1 GUNW 产品；它们需要 NASA Earthdata 账号。**需要账号时停下来告诉我**，我把凭据放进 `.env`（如 `EARTHDATA_USERNAME/EARTHDATA_PASSWORD`），绝不写入配置或提交。
- 滑坡编目、加速/破坏时间的参考真值同样要来自公开来源（论文附录、公开编目或 GNSS/现场监测数据集），并在 `data_manifest.json` 记录 `source_url`、许可、下载时间与 SHA-256。
- 研究区与时段先从任务书和调研文献中选有公开加速/破坏记录的滑坡；选定方案（站点、轨道、时间范围、预计数据量）先给我确认再大批下载。
- 下载必须断点续传、按文件校验，失败只重试该文件；下载完成后由宿主重新计算哈希，不相信模型口述“文件已存在”。

### 约束（不可违反）
- 遵守 `AGENTS.md` 全部条款：每完成一个可交付的小步就**中文 commit 并 push 到 `fork/docker-setup`**；密钥（`.env`、API key、GitHub/Overleaf token）不得进入 commit、日志或配置文件。
- 只用 `deepseek-flash`，无兜底模型；推理开启；美元预算保持 0；不设全局 token 下限；保留 wsproto、docker-sibling 的 `RC_HOST_REPO` 路径翻译、docker.sock + GID。
- 不推倒重写：复用现有 `ideation/` 的会话/事务/哈希断点模式、`execution_contract.py` 的验收、`LlmCodeAgent`、`verified_registry.py`、`writing/`、`overleaf/`；能改造就不新造。
- 实验期间不轮询模型；长训练用 detached 兄弟容器 + OS 级监控。
- 不得为了“跑通”而降低验收标准、跳过方法审查或伪造结果。验收失败就如实停下并报告原因。

### 工作方式
- 先写 `docs/REBUILD_PROGRESS.md`（进度文件）和 `docs/rebuild_checklist.json`（上面 7 条完成标准拆成可验证条目，每条含 `verify` 步骤和 `passes:false`）；只有拿到证据才能把 `passes` 改成 true，不得删改条目。每次开始工作先读这两个文件和 `git log`。
- 按 P0 → P1 → P2 → P3 顺序推进，每期结束：跑相关测试、真机验证（`AGENTS.md` 的“生效方式速查/验证习惯”）、commit+push、更新进度文件。
- 每期结束简要汇报：做了什么、证据在哪、什么没验证；然后直接进入下一期，不需要等我批准。
- **以下情况停下来问我**：需要改变已确认的科学方法或主指标；需要外部账号（如 Earthdata）；确认研究区/时段/数据量方案；某个约束和完成标准互相冲突；连续两次尝试仍无法让某条完成标准通过。
- 真实项目运行耗时长时，放后台执行并用看门狗/日志跟踪，不要阻塞；运行结果以文件状态为准，不以模型口述为准。

### 最终交付
- 汇报每条完成标准的证据位置（文件路径、测试名、日志片段）。
- 项目 `rc-20261025-145941-c30e99` 的 `paper.tex` / `paper_zh.tex` 已同步到 Overleaf `runs/<run_id>/zh/`。
- 列出仍未解决的问题与下一步建议（含 P4）。
