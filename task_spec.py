"""Structured task spec built from the owner's QQ task message.

A task created from chat used to carry an empty checkpoint, so browser tasks had no URL, code
tasks no test command or target files, and the verifier could only check "some file changed".
This extracts what the owner actually wrote (Chinese or English labels) into the checkpoint the
executors and TaskVerifier already read. Extraction is deterministic: it runs in the chat path,
so it must not call a model, and it never invents values the message does not contain.

    修复登录 bug；测试: pytest -q tests/test_login.py；文件: app/login.py, app/session.py
    fix the parser. test: npm test --silent; files: src/parse.ts
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

# A Chinese full stop also ends a value: a model's plan writes "文件: notes.md。验收方式：…" (2026-10-26, Claude Haiku),
# and the rest of that sentence used to become target files ("存在", "Git") that could never exist.
_END = r'(?=[；;。\n]|$)'
_QUOTED = r'''(?:"(?:\\.|[^"\n])*"|'(?:\\.|[^'\n])*')'''
_TEST = re.compile(r'(?:测试命令|测试|test\s*command|tests?)\s*[:：]\s*(?P<v>(?:' + _QUOTED + r'''|[^；;。\n"'])+)''', re.I)
_FILES = re.compile(r'(?:目标文件|文件|target\s*files?|files?)\s*[:：]\s*(?P<v>.+?)' + _END, re.I)
_URL = re.compile(r'https?://[^\s，。；;、<>"\']+', re.I)
_ORDER = re.compile(r'(?:订单号?|order(?:\s*(?:id|no\.?|number))?)\s*[:：#]?\s*(?P<v>[A-Za-z0-9][A-Za-z0-9_-]{5,63})', re.I)
_SPLIT = re.compile(r'[,，、\s]+')
# A bare imperative on the workbench ("修复当前项目的登录错误") is a command, not conversation.
_POLITE_IMPERATIVE = re.compile(
    r'^帮(?:我|忙)(?:把|给|列|查|看|找|改|修|跑|执行|下载|装|整理|生成|创建|删|统计|分析|算|转|导出|打包|替换|更新|上传|打开|检查|测试|监控)')
_WORKBENCH_IMPERATIVES = ('打开', '运行', '执行', '修复', '修改', '创建', '新建', '保存', '下载', '上传',
                          '抓取', '爬取', '监控', '定时', '提醒', '安排', '生成', '制作', '部署', '安装',
                          '编写', '重构', '测试', '跑', '检查', '整理', '导出', '导入', '打包', '统计')


def input_intent(text: str, source: str = 'qq') -> str:
    """Workbench commands are tasks; QQ requires an actual delegation, not a topic keyword.

    2026-10-26 user: "你好" in the workbench console became task_20261026_141126_31727e. The console now
    defaults to automatic routing; 'workbench-auto' classifies the same way a QQ message is classified.
    """
    if source == 'workbench':
        return 'task'
    if source not in ('qq', 'workbench-auto'):
        raise ValueError('未知的输入入口')
    import lexicon
    text = text.strip()
    if lexicon.starts_with_any(text, lexicon.EXPLICIT_TASK_PREFIXES):
        return 'task'
    if re.match(r'^(?:如果|假如|要是|你觉得|你说|他说|她说|我说|聊聊|[“"「])', text):
        return 'chat'
    if source == 'workbench-auto' and len(text) >= 6:
        if lexicon.starts_with_any(text, _WORKBENCH_IMPERATIVES):
            # The workbench already points at a project, so a bare "修复…" is a command there, not chat.
            return 'task'
        if _POLITE_IMPERATIVE.match(text):
            # 2026-10-26: “帮我列一下 build 目录里的 png” 被当成闲聊，模型只能回“没有可用的工具”。
            # 帮忙类开头 + 动作动词，仍然是任务。
            return 'task'
    if lexicon.is_heavy_task_request(text):
        return 'task'
    delegation = re.match(r'^(?:(?:能不能|可以|能否)\s*)?(?:请)?(?:帮我|替我|麻烦你|请你|给我)\s*(.+)', text)
    if delegation and re.match(r'(?:做|执行|安排)(?:一个|一项)?任务\s*[:：，,]\s*\S', delegation[1]):
        return 'task'
    if delegation and re.match(r'(?:(?:先|现在|立即|马上|继续)\s*)?'
                              r'(?:在[^，。！？\n]{1,40}?(?:里|内|中|下|上)\s*)?'
                              r'(?:打开|运行|执行|修复|修改|创建|新建|保存|下载|上传|抓取|爬取|监控|定时|提醒|安排|生成|制作|部署|安装|编写|重构|测试)', delegation[1]):
        return 'task'
    return 'chat'


def parse_autonomous_goal(text: str) -> dict | None:
    prefix = re.match(r'^(?:自主目标|长期目标)\s*[:：]\s*', text)
    if not prefix:
        return None
    rest = text[prefix.end():]
    labels = list(re.finditer(r'(?:^|[\n；;])\s*(授权范围|范围|验收标准|验收)\s*[:：]\s*', rest))
    values = {'description': rest[:labels[0].start()].strip() if labels else rest.strip()}
    for index, label in enumerate(labels):
        key = 'scope' if label.group(1) in ('范围', '授权范围') else 'completion_criteria'
        values[key] = rest[label.end():labels[index + 1].start() if index + 1 < len(labels) else len(rest)].strip()
    if not all(values.get(key) for key in ('description', 'scope', 'completion_criteria')):
        raise ValueError('请按三行填写：自主目标：要完成什么\n范围：授权操作范围\n验收：可检查的完成标准')
    return values


def _safe_relative(path: str) -> str | None:
    """Workspace-relative path or None: no absolute paths, drive letters or parent traversal."""
    clean = path.strip().strip('"\'`').replace('\\', '/')
    if not clean or clean.startswith('/') or re.match(r'^[A-Za-z]:', clean):
        return None
    parts = PurePosixPath(clean).parts
    if not parts or '..' in parts:
        return None
    return str(PurePosixPath(*parts))


def _file_targets(value: str) -> list[str]:
    """Paths from a "文件:" value. Models write the label mid-sentence ("（文件: notes.md），内容为中文 Markdown，…"),
    so the list ends at the first word that is not a path; unsafe paths are skipped."""
    targets = []
    for token in _SPLIT.split(value):
        token = token.strip('"\'`“”‘’「」（）()【】[]')
        if not token or re.search('[:：]', token):
            continue
        if not re.fullmatch(r'[\w\-./\\]+', token) or not ('.' in token or '/' in token or token.isascii()):
            break
        path = _safe_relative(token)
        if path:
            targets.append(path)
    return targets


def build_task_spec(text: str, category: str, provider: str = '') -> dict:
    """Checkpoint fields for a new task, taken only from what the owner wrote."""
    spec: dict = {'spec_source': 'qq_message'}
    if category == 'research':
        spec['research_autoplan'] = True
    if category == 'browser':
        spec['browser_autoplan'] = True
    if provider:
        spec['backend'] = provider
    if category == 'data':
        keys = re.search(r'(?:必要字段|字段|expected_keys)\s*[:：]\s*(.+?)(?=[。；;\n]|$)', text, re.I)
        if keys:
            spec['expected_keys'] = [key for key in _SPLIT.split(keys.group(1).strip()) if key]
    test = _TEST.search(text)
    if test:
        spec['test_command'] = test.group('v').strip().strip('`')
    files = _FILES.search(text)
    if files:
        targets = _file_targets(files.group('v'))
        if targets:
            spec['target_files'] = targets
    url = _URL.search(text)
    if url and category == 'browser':
        spec['url'] = url.group(0).rstrip('.,)')
        expected = re.search(r'(?:期望文本|页面包含|expected_text)\s*[:：]\s*(.+?)(?=[。；;\n]|$)', text, re.I)
        if expected:
            spec['expected_text'] = expected.group(1).strip()
    order = _ORDER.search(text)
    if order and category == 'ecommerce':
        spec['order_id'] = order.group('v')
    return spec


def describe_spec(spec: dict) -> str:
    """One line telling the owner what the task will be checked against."""
    parts = []
    if spec.get('test_command'):
        parts.append(f"test: {spec['test_command']}")
    if spec.get('target_files'):
        parts.append('files: ' + ', '.join(spec['target_files']))
    if spec.get('url'):
        parts.append(f"url: {spec['url']}")
    if spec.get('order_id'):
        parts.append(f"order: {spec['order_id']}")
    return '; '.join(parts)
