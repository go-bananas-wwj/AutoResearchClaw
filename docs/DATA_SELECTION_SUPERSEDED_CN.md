> **已作废（2026-11-03）**：本方案由开发协作者手工选点并硬编码，违反 AGENTS.md 规则 3（科研决策须由流水线模型产出）。现由阶段9模型起草 `stage-09/data_plan.json`、宿主校验后执行；本文仅作历史记录，相关下载已迁入 `/home/wwj/rc-data/shared` 供复用。

# 真实数据方案：滑坡加速段 InSAR（rc-20261025-145941-c30e99）

**用户确认**：2026-11-03 在会话中选择“按此方案”（4 个滑坡、升降轨、INSAR_ISCE_BURST 10x2）。

## 研究区（留一滑坡交叉验证的 4 折）

| 滑坡 | 位置（WGS84） | 破坏时间（公开记录） | 时间窗 | 参考文献 |
|---|---|---|---|---|
| 茂县新磨 Xinmo | 32.069N, 103.665E | 2017-06-24 | 2015-01-01 ~ 2017-06-25 | Intrieri et al., 2018, Landslides, doi:10.1007/s10346-017-0915-7 |
| 白格 Baige | 31.083N, 98.704E | 2018-10-10（第二次 2018-11-03） | 2016-06-01 ~ 2018-10-11 | RSE 2020, doi:10.1016/j.rse.2020.111695 |
| Mud Creek（加州 Big Sur） | 35.866N, 121.432W | 2017-05-20 | 2015-03-01 ~ 2017-05-21 | Handwerger et al., Sci Rep 2025, doi:10.1038/s41598-025-11399-8 |
| Brienz/Brinzauls（瑞士） | 46.68N, 9.66E | 2023-06-15 | 2021-06-01 ~ 2023-06-16 | NHESS 24, 3833 (2024) |

破坏时间与加速起点作为参考真值，必须在 `data_manifest.json` 中逐条记录出处（DOI/URL）。

## 数据来源

- **缠绕相位 + 相干性**：ASF HyP3 `INSAR_ISCE_BURST`（1 credit/对，looks=10x2 ≈ 40 m），每站升降轨各 1 个 burst（ASF 检索 2026-11-03：Xinmo ASC p128 / DES p62；Baige ASC p99 / DES p106；Mud Creek DES p42 / ASC p35；Brienz ASC p15 / DES p66），相邻 1–2 期组对。账号凭据只读 `.env` 的 `EARTHDATA_*`。
- **DEM**：Microsoft Planetary Computer `cop-dem-glo-30`（用 STAC 查询核实后使用）。
- Planetary Computer 不托管 Sentinel-1 SLC/干涉图：需在获取日志中以 STAC 搜索结果核实。

## 规模估计
约 950 个干涉对 ≈ 950 credits（账户 8000），数据约 40–60 GB，存放 `/home/wwj/rc-data/insar_landslide/`（不进 git）。

## 已知科学风险（如实报告，不回避）
白格、Mud Creek、Brienz 破坏前速度达 m/天量级，远超 C 波段相邻期相位可测范围（6–12 天间隔下约 λ/4 ≈ 1.4 cm），方法在加速末段可能失效；该情况作为实验结果报告，不更换方法或数据凑结果。
