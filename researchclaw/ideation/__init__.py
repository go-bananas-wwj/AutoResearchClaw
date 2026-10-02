"""Ideation 选题引擎——证据卡汇总。

把流水线 Stage 1–8 的产出（文献扫描→缺口分析→假设+查新）汇总成结构化的
``ideation_report.json``：每个候选科学问题一张"证据卡"，含缺口证据、查新
结果、数据集建议、V100 可行性与推荐排序，供人在拍板前审阅。
"""

from researchclaw.ideation.report import build_ideation_report  # noqa: F401
