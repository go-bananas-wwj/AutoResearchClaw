# AutoResearchClaw 中的官方 IdeaSpark

上游：[microsoft/ResearchStudio](https://github.com/microsoft/ResearchStudio/tree/063f10088af49909e1a4a7ea8d29efe19633f34e/ResearchStudio-Idea/skills/idea_spark)。

本目录保存该提交的原始 skill、执行器、提示词、模式资料与 Apache-2.0 许可证。`UPSTREAM.json` 保存每个原始文件的 SHA-256；宿主启动及确认卡片前验证它们。此说明文件是本仓库新增文件，不属于上游校验清单。

宿主适配在 `researchclaw/ideation/ideaspark.py`、`spark_navigator.py`。Phase 0 导入本系统已审核、经连接器取得的文献与真实方法原文，明确记录为导入，不冒称重新运行了官方检索。Phase 1 使用官方瓶颈诊断提示词；Phase 2–4 逐步执行官方导航器的完整动作以及 prepare → 独立模型请求 → record 流程。一个运行产生一个提案，默认以 12 个独立目录组成批次。先逐个生成草稿，再轮流执行后续检查。

沿用现有 DeepSeek 模型及兜底链。机械标注/中文呈现关闭推理；官方诊断和质量阶段开启推理并提供较大输出空间。各请求仅有自己的 system/user，不继承其他请求的对话。版本绑定输入、输出、质量记录、成本预留与阶段结果均保存在调研会话的 `ideaspark/rN/` 下；密钥只由进程环境读取。

模型不能自行声称执行代码。标准库算术脚本由无网络、只读、无GPU、限制资源的独立 CPU 容器执行，再用实际输出核对报告；这是机制的微型确定性检查，不是训练或正式科研实验。缺少执行证据时保持 unexecuted，不把模型文字当作执行结果。

草稿及 FAILED VALIDATION/needs_work 提案可展示，但不能确认启动科研。只有官方当前版本的发布记录有效、候选内容哈希未变且本系统原文证据仍有效的卡片，才通过原有确认接口交接阶段 9–23。提案审查通过不代表实验结果或全球新颖性已被证明。

`llm.ideation_backend: ideaspark` 为当前默认；`builtin` 保留旧会话及测试的兼容实现。`llm.ideation_card_target` 默认 12、范围 1–20；证据不足或独立方向不足时如实返回，不为数量编造方案。镜像需安装 feedparser、openreview-py、beautifulsoup4、PyMuPDF，并包含本目录。
