"""Intent classification for conversational research."""

from __future__ import annotations

import re
from enum import Enum
from typing import Any


class Intent(str, Enum):
    """Research chat intents."""

    BRIEF = "brief"
    TOPIC_SELECTION = "topic_selection"
    START_PIPELINE = "start_pipeline"
    STOP_PIPELINE = "stop_pipeline"
    SYNC_OVERLEAF = "sync_overleaf"
    PULL_OVERLEAF = "pull_overleaf"
    IDEATION = "ideation"
    REPRODUCE = "reproduce"
    CHECK_STATUS = "check_status"
    MODIFY_CONFIG = "modify_config"
    DISCUSS_RESULTS = "discuss_results"
    EDIT_PAPER = "edit_paper"
    GENERAL_CHAT = "general_chat"
    HELP = "help"


# Keyword patterns for fast classification
_INTENT_PATTERNS: list[tuple[Intent, re.Pattern[str]]] = [
    # BRIEF 必须排在最前：「开始确认」「定题」等词与 START/TOPIC 模式重叠
    (Intent.BRIEF, re.compile(
        r"(?:确认选题|开始确认|定题|敲定|研究任务书|research\s*brief|就选第|选第\s*\d|用第\s*\d+\s*个|就要第\s*\d)",
        re.IGNORECASE,
    )),
    (Intent.IDEATION, re.compile(
        r"(?:找选题|选个题|选题结果|选题报告|证据卡|有什么可做|有没有.*(?:选题|方向可做)|推荐.*(?:课题|题目)|ideat)",
        re.IGNORECASE,
    )),
    (Intent.PULL_OVERLEAF, re.compile(
        r"(?:拉取?|拉回|取回|同步回来).{0,6}overleaf|overleaf.{0,6}(?:改动|修改|批注)|pull.{0,12}overleaf",
        re.IGNORECASE,
    )),
    (Intent.SYNC_OVERLEAF, re.compile(
        r"(?:overleaf|同步到?\s*overleaf|推送到?\s*overleaf)", re.IGNORECASE
    )),
    (Intent.REPRODUCE, re.compile(
        r"(?:复现|reproduc|跑一下?这篇|复刻)",
        re.IGNORECASE,
    )),
    (Intent.HELP, re.compile(
        r"(?:^\s*help\s*$|\bhow\s+to\b|\busage\b|帮助|怎么用)", re.IGNORECASE
    )),
    (Intent.STOP_PIPELINE, re.compile(
        r"(?:\b(?:stop|abort|cancel|halt)\b|停止|终止|取消|停下来)",
        re.IGNORECASE,
    )),
    (Intent.START_PIPELINE, re.compile(
        r"(?:\b(?:start|begin|launch)\b|\brun\s+(?:a|an|the|new|experiment|pipeline)|开始|启动|开跑|跑一个|跑起来|运行)",
        re.IGNORECASE,
    )),
    (Intent.CHECK_STATUS, re.compile(
        r"(?:\b(?:status|progress|stage|current)\b|阶段|进度|到哪|第几|哪一步)", re.IGNORECASE
    )),
    (Intent.TOPIC_SELECTION, re.compile(
        r"(?:\b(?:topic|idea|direction)\b|research\s+direction|研究方向|选题|研究主题|想法)",
        re.IGNORECASE,
    )),
    (Intent.MODIFY_CONFIG, re.compile(
        r"(?:\b(?:config|setting|parameter|batch|epoch)\b|learning\s+rate|学习率|修改|设置)",
        re.IGNORECASE,
    )),
    (Intent.DISCUSS_RESULTS, re.compile(
        r"(?:\b(?:results?|metrics?|accuracy|loss|performance)\b|结果|指标|效果|怎么样)",
        re.IGNORECASE,
    )),
    (Intent.EDIT_PAPER, re.compile(
        r"(?:\b(?:paper|abstract|introduction|draft)\b|论文|摘要|改一下|写)",
        re.IGNORECASE,
    )),
]


def classify_intent(message: str) -> tuple[Intent, float]:
    """Classify user intent from message text.

    Returns (intent, confidence) where confidence is 0-1.
    Uses keyword matching for speed; can be replaced with LLM.
    """
    message_lower = message.strip().lower()

    if not message_lower:
        return Intent.GENERAL_CHAT, 0.0

    for intent, pattern in _INTENT_PATTERNS:
        if pattern.search(message_lower):
            return intent, 0.8

    return Intent.GENERAL_CHAT, 0.5
