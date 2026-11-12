"""Bilingual (Chinese + English) matching vocabularies for chat commands and intent routing.

QQ friends write in Chinese; the English entries exist for English-speaking users and tests.
Keep every vocabulary here instead of inline literals: an earlier translation pass blanked
inline Chinese literals into ``''``, and ``'' in text`` is always True, which silently broke
routing. ``tests/test_literal_integrity.py`` guards these tables.
"""
from __future__ import annotations

import re

TASK_STATUS_EXACT = ("任务状态", "查看任务", "任务列表", "task status", "view tasks", "task list", "/tasks", "/task list")
TASK_STATUS_PHRASES = ("任务怎么样了", "任务完成了吗", "任务做完了吗", "任务进展", "查一下任务进度", "刚才的任务", "任务执行到哪了",
                       "how is the task", "is the task done", "task progress", "check task status", "previous task")

TASK_CANCEL_PREFIXES = ("取消任务", "停止任务", "cancel task", "stop task")
TASK_PAUSE_PREFIXES = ("暂停任务", "pause task")
TASK_STEER_PATTERN = re.compile(r"^(追加任务要求|修改任务要求|steer task|add task requirement)\s*[:：]?\s*", re.I)
TASK_CONTROL_PREFIXES = TASK_CANCEL_PREFIXES + TASK_PAUSE_PREFIXES + (
    "追加任务要求：", "追加任务要求:", "修改任务要求：", "修改任务要求:", "steer task:", "add task requirement:")

APPROVE_PATTERN = re.compile(r"^(批准|同意|approve)\s*[:：]?\s*((?:apr|appr|task|grant)_[A-Za-z0-9_]+)\s*$", re.I)
REJECT_PATTERN = re.compile(r"^(拒绝|驳回|reject|deny)\s*[:：]?\s*((?:apr|appr|task|grant)_[A-Za-z0-9_]+)(?:\s+(.+))?$", re.I)
PENDING_APPROVALS_EXACT = ("待审批", "待批准", "审批列表", "pending approvals", "approvals", "/approvals")

CORRECTION_PREFIXES = ("不对，我", "不对,我", "不是，我", "不是,我", "记错了，我", "记错了,我", "更正一下：", "纠正一下：", "纠正一下,",
                       "no, i ", "actually, i ", "not really, i ", "correction:", "correct that:")
CORRECTION_STRIP = re.compile(r"^(不对[，,]|不是[，,]|记错了[，,]|更正一下[：:]?|纠正一下[：:,]?|no,\s*|actually,\s*|"
                              r"not really,\s*|correction:\s*|correct that:\s*)", re.I)

EXPLICIT_TASK_PREFIXES = ("/task ", "任务：", "任务:", "创建任务：", "创建任务:", "task:", "create task:")
EXPLICIT_TASK_STRIP = re.compile(r"^(/task\s+|任务[:：]\s*|创建任务[:：]\s*|task:\s*|create task:\s*)", re.I)
HEAVY_TASK_PREFIXES = ("帮我开发", "帮我写一个完整", "自动构建", "生成科研日报", "抓取论文并验证", "批量剪辑渲染", "帮我制作视频", "帮我剪辑",
                       "全网比价", "help me develop", "write a complete", "build an autonomous", "generate research digest",
                       "fetch papers and verify", "batch render video", "make a video", "compare prices")
HEAVY_TASK_VERBS = ("帮我", "请帮我", "自动", "批量", "help me", "please", "automatically", "batch")
HEAVY_TASK_OBJECTS = ("开发一个", "写一个完整", "构建系统", "生成科研日报", "抓取论文", "剪辑渲染", "渲染视频", "制作视频样片", "多平台比价",
                      "develop a", "write a complete", "build a system", "research digest", "fetch papers", "render video",
                      "compare prices across")

# Checked in order: the first category whose words appear wins; otherwise "general".
TASK_CATEGORY_WORDS = (
    ("video", ("视频", "剪辑", "渲染", "分镜", "字幕", "video", "render", "clip", "storyboard", "subtitle")),
    ("ecommerce", ("比价", "电商", "淘宝", "京东", "拼多多", "物流", "订单", "ecommerce", "price", "order", "shipping", "buy", "product")),
    ("browser", ("浏览器", "网页", "爬取", "抓取网页", "表单", "browser", "webpage", "crawl", "scrape", "form")),
    ("research", ("论文", "科研", "arXiv", "学术", "定理", "证明", "research", "paper", "arxiv", "theorem", "hypothesis", "digest")),
    ("code", ("代码", "开发", "构建", "修复", "重构", "测试", "python", "code", "refactor", "bug", "test", "develop", "build")),
)

USER_FACT_TOPICS = {
    "location": ("搬", "住", "城市", "位置", "地址", "所在地", "省", "地区", "moved", "live", "city", "location", "address"),
    "age": ("年龄", "岁", "生日", "出生", "age", "years old", "birthday", "born"),
    "family": ("儿子", "女儿", "孩子", "父亲", "母亲", "家人", "son", "daughter", "child", "father", "mother", "family"),
    "name": ("名字", "昵称", "称呼", "姓名", "name", "nickname", "call me"),
}


def starts_with_any(text: str, prefixes) -> bool:
    lowered = text.lower()
    return any(lowered.startswith(p.lower()) for p in prefixes)


def contains_any(text: str, words) -> bool:
    lowered = text.lower()
    return any(w.lower() in lowered for w in words)


def is_task_status_query(text: str) -> bool:
    return text.lower() in TASK_STATUS_EXACT or contains_any(text, TASK_STATUS_PHRASES)


def is_heavy_task_request(text: str) -> bool:
    if len(text) <= 12:
        return False
    return starts_with_any(text, HEAVY_TASK_PREFIXES) or (
        contains_any(text, HEAVY_TASK_VERBS) and contains_any(text, HEAVY_TASK_OBJECTS))


def task_category(text: str) -> str:
    if re.search(r'https?://', text, re.I) and contains_any(text, ('打开', '截图', '网页', '浏览器', 'open', 'screenshot')):
        return 'browser'
    for category, words in TASK_CATEGORY_WORDS:
        if contains_any(text, words):
            return category
    if re.search(r'(?:创建|新建|保存|写入|读取|移动|重命名|整理).*(?:文件|目录|文件夹|\.[a-zA-Z]{1,8}\b)', text):
        return 'file'
    return "general"
