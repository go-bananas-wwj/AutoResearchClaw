"""语言感知的长度统计（本 fork「中文先行」适配）。

英文按空白切分计词；中文按 CJK 字符数 + 非 CJK 连续片段按词计。
供阶段 17 草稿质量校验、阶段 19 修订长度保护、LaTeX converter 完整性
检查共用，保证各处口径一致。
"""

from __future__ import annotations

import re

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

# 1 英文词 ≈ 2 个中文字符（把英文词数目标换算成中文字符目标）
ZH_CHARS_PER_WORD = 2.0


def count_words(text: str, language: str = "auto") -> int:
    """统计文本长度。

    ``language="en"`` 时维持 ``len(text.split())`` 原行为；
    ``"zh"``（或 ``"auto"`` 且文本含 CJK）时按 CJK 字符 + 非 CJK 词计。
    """
    if language == "en":
        return len(text.split())
    if language == "auto" and not _CJK_RE.search(text):
        return len(text.split())
    cjk_count = len(_CJK_RE.findall(text))
    non_cjk_words = len(_CJK_RE.sub(" ", text).split())
    return cjk_count + non_cjk_words


def scale_word_targets(
    targets: dict[str, tuple[int, int]],
    language: str = "en",
) -> dict[str, tuple[int, int]]:
    """把英文词数区间目标换算成目标语言的长度单位（zh → 字符）。"""
    if language != "zh":
        return dict(targets)
    return {
        key: (int(lo * ZH_CHARS_PER_WORD), int(hi * ZH_CHARS_PER_WORD))
        for key, (lo, hi) in targets.items()
    }
