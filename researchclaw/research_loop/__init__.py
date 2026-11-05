"""v2 研究循环：立项书 → 多轮"学生汇报—导师审阅"迭代（docs/AUTORESEARCH_V2_CN.md §5）。

科研内容（假设、改动、诊断、决策）全部由模型产出；本包只提供工具、契约、校验与断点续作：
立项书字段校验、引文逐字核对、宿主关卡与正式运行、宿主注入的指标表、结构化决策与用户门控。
"""

from researchclaw.research_loop.loop import run_research_loop

__all__ = ["run_research_loop"]
