"""Localized presentation strings for Open QQ Dot's desktop panel.

Only pass human-facing labels to tr(). Never translate model IDs, database columns,
protocol values, state keys or command keywords. The default Chinese release can
switch to English with OPENQQ_UI_LANG=en. Locale is read at the point of use.
"""
from __future__ import annotations
import os

ZH_CN = {'Agent Workbench': '智能体工作台', 'Status & Remote': '状态与远程连接', 'Instructions & Persona': '常驻指令与角色设定', 'Memory & Knowledge': '长期记忆与知识库', 'Engine & Tools': '模型引擎与工具', 'Tasks & Proactive': '任务与主动通知', 'Chat History & Logs': '聊天记录与审计日志', 'Friends Overview': '好友总览', 'Reading status...': '正在读取状态…', 'Connect QQ 直连': '连接 QQ 直连', 'Exit & Stop': '退出并停止服务', 'Switch Friend': '切换好友', 'Open Window': '打开主窗口', 'Exit & Stop Backend': '退出并停止后端', 'Backend Bridge: ': '后端连接：', 'Backend Bridge: Starting QQ 直连 and backend...': '后端连接：正在启动 QQ 直连 和后端…', 'QQ 直连 is currently online (OneBot port 3001 OK).': 'QQ 直连 已在线（OneBot 端口 3001 正常）。', 'Connection Status': '连接状态', 'Preparing QQ 直连 connection...': '正在准备 QQ 直连 连接…', 'Connect': '连接', 'QQ 直连 is connected online, establishing bridge.': 'QQ 直连 已连接，正在建立消息桥接。', 'Connection Succeeded': '连接成功', 'Scan to Login': '扫码登录', 'Connection Notice': '连接提示', 'Startup Notice': '启动提示', 'Persistent memory has unsaved changes. Please save before switching friends.': '长期记忆尚有未保存的修改，请保存后再切换好友。', 'This friend is enabled.': '该好友已启用。', 'This friend is paused.': '该好友已暂停。', 'Failed to stop, bridge may still be running': '停止失败，消息桥接可能仍在运行', 'Stopping and exiting...': '正在停止并退出…', 'Cancel': '取消', 'Close': '关闭', 'Projects': '项目', 'Add local project folder': '添加本地项目文件夹', 'New chat under active project': '在当前项目中新建对话', '+ New Chat': '+ 新建对话', 'Workspace: Default': '工作区：默认', 'Template: None': '模板：无', 'Reasoning...': '思考中…', 'Stop generation': '停止生成', '🔄 Refresh': '🔄 刷新', '⚡ Flush Queue': '⚡ 疏通队列', 'Drain and flush any stagnant message queues or backlog': '检查并安全恢复积压消息，不会直接删除未读内容', 'Save to Long-Term Memory (Pin)': '收藏到长期记忆', 'Rename current session': '重命名当前对话', 'More options': '更多选项', 'Attach project file reference': '引用项目文件', 'Full Access': '完全访问权限', 'Switch CLI permission and approval policy': '切换 CLI 权限及审批策略', 'Model & Effort ⌄': '模型与思考强度 ⌄', 'Choose model or adjust reasoning effort': '选择模型或调整思考强度', 'Enable Fast mode acceleration': '启用快速模式', 'Send (Enter)': '发送（Enter）', 'Just now': '刚刚', 'Click to collapse': '点击收起', 'Click to expand': '点击展开', 'Retrieval API': '检索 API', 'Embedding Model': '向量模型', 'Rerank Model': '重排模型', 'Add Function API': '添加功能 API', 'Function API': '功能 API', 'Configured APIs': '已配置 API', 'Tool Name': '工具名称', 'Description': '功能描述', 'Method': '请求方法', 'Auth Header': '鉴权请求头', 'API Key': 'API 密钥', 'Manual Configuration': '手动配置', 'Local Model Settings': '本地模型配置', 'Browse Directory': '浏览文件夹', 'Local Path': '本地模型路径', 'API URL': '接口地址', 'Enable ': '启用 ', 'Device': '计算设备', 'Source': '来源', 'Model': '模型', 'Preset': '预设', 'Select File': '选择文件', 'Start Generation': '开始生成', 'Import Template Package': '导入模板包', 'Export Template Package': '导出模板包', 'Delete Template': '删除模板', 'Save Templates': '保存模板', 'Discard': '放弃修改', 'Apply to Current Friend': '应用到当前好友', 'Cloud Model (Follow Current Friend)': '云端模型（跟随当前好友）', 'Statistical Analysis (No Model Required)': '统计分析（无需模型）', 'Refresh': '刷新', 'View Full Input': '查看完整输入', 'View Actual Prompt': '查看实际提示词', 'View This Run': '查看本次执行', 'Model Output': '模型输出', 'Retrieval Hits': '检索命中', 'Compare with Previous': '与上次比较', 'Clear Local Chat': '清除本地聊天', 'Load Older': '加载更早记录', 'Save Memory': '保存记忆', 'Category': '分类', 'Content': '内容', 'Locked': '锁定', 'Pinned': '置顶', 'Date Mentioned': '提及日期', 'Expires': '到期时间', 'Move to Category': '移动到分类', 'Retire Entry': '停用条目', 'Reset Persistent Memory': '重置长期记忆', 'Roll Back Memory': '回滚记忆版本', 'Save Settings': '保存设置', 'Scheduled Time': '计划时间', 'Task Type': '任务类型', 'Cannot Send Test Round': '无法发送测试轮次', 'Pause': '暂停', 'Retry': '重试', 'Stickers & Multimodal': '表情与多模态', '0 Intro': '0 简介', '1 Rules': '1 规则', '2 Persona & Style': '2 人设与风格', '3 Contact Profile': '3 用户画像', '4 Recent Status': '4 近期状态', '5 Retrieved Knowledge': '5 检索知识', '6 Example Dialogues': '6 对话示例', '7 Summary & Chat': '7 摘要与聊天', '7 History & Chat': '7 历史与聊天', '8 Author Notes': '8 作者备注', '9 Current Task': '9 当前任务'}

ZH_CN.update({
    'Chat': '对话', 'You': '我', 'Copy content': '复制内容', 'Save': '保存', 'Failed': '操作失败',
    'Clear Chat': '清空对话', 'Queue Drained': '队列已疏通', 'items': '项目', 'itemsFailed': '项目读取失败',
    'ModelFailed': '模型切换失败', 'Reasoning EffortFailed': '思考强度切换失败', ' CLI Failed': '智能体调用失败',
    'Session history cleared. ': '会话记录已清空。', 'Intent / Topic': '任务意图 / 主题',
    'Provider': '来源', 'Agent': '智能体', 'Agent 工具': '智能体工具', 'Agent 协作网络': '智能体协作网络',
    'HEADLESS': '无界面命令行', 'headless': '无界面命令行', 'Fast': '快速',
    '快捷命令 (Slash Commands)': '快捷命令', '添加与上下文引用 (Mentions)': '添加与上下文引用',
    '发送指令 (Enter)': '发送指令（回车）', '常驻指令与角色 (Standing Goals)': '常驻指令与角色',
    '交互与回复偏好 (Preferences)': '交互与回复偏好', '用户画像与背景 (User Context)': '用户画像与背景',
    '长期事实与记忆 (Persistent Memory)': '长期事实与记忆', '深度研究与资料库 (Knowledge & Topics)': '深度研究与资料库',
    '会话历史记录 (Chat History)': '会话历史记录', '本轮输入与推理审计 (Context Audit)': '本轮输入与推理审计',
    '活动与心情日志 (Activity & Emotion)': '活动与心情日志', '移动设备与网页访问 (Mobile & Web Access)': '移动设备与网页访问',
    '重新规划待办 (Replan)': '重新规划待办', '打开 Web 工作台 ↗': '打开网页工作台 ↗', 'SaveBasic Info': '好友资料保存失败',
    'Base URL (including /v1) or full endpoint; local services allowed': '接口基础地址（可含 /v1）或完整地址；支持本地服务',
    'Provider model ID or local model name': '服务商提供的模型标识，或本地模型名称',
    'Dedicated API Key; leave empty to keep existing': '独立接口密钥；留空保留原密钥',
    'Must match model output dimension': '须与模型输出的向量维度一致',
    'Downloaded Transformers model directory; does not auto-download': '已下载的 Transformers 模型目录；不会自动下载',
    'API sources send queries and candidate texts to the configured service; local sources read local models only. Falls back to keyword/BM25 if unconfigured or failed.':
        '远程接口会把查询和候选文本发送到所配置的服务；本地模式只读取本机模型。未配置或不可用时降级为关键词检索。',
    'Allow model to invoke this Function API for this friend': '允许模型为当前好友调用这个功能接口',
    'Tool name, e.g. api_weather': '工具名称，例如 api_weather',
    'Describe when the model should call it and what it returns': '说明何时调用这个工具，以及它会返回什么',
    'Static HTTP(S) endpoint URL': '固定的 HTTP(S) 接口地址',
    'API key for this function; leave empty to keep existing': '该功能的独立密钥；留空保留原密钥',
    'Parameters Schema (JSON Schema)': '参数结构（JSON Schema）',
    'Fixed Parameters (JSON object, optional)': '固定参数（可选的 JSON 对象）',
    'Each API saves keys independently. Enabled APIs become tools available to CLI and chat models. GET parameters are query params, POST parameters form JSON body.':
        '各接口分别保存密钥；启用后，智能体和聊天模型均可调用。GET 参数放在查询中，POST 参数组成 JSON 请求正文。',
    'Leave empty to keep saved key': '留空保留已保存的密钥', 'Pooling': '向量聚合方式', 'Query Prefix': '查询前缀',
    'Doc Prefix': '文档前缀', 'Dimension': '向量维度',
    'My Persona Templates': '我的角色模板', 'Generate Templates from Chat History': '从聊天记录生成模板',
    'Analysis Model': '分析模型', 'Sticker Collection': '表情收藏', 'Deletion Failed': '删除失败',
    'Latest Generation Input': '最近一次生成输入', 'Block Content': '内容块详情', 'Generation History': '生成历史',
    'Read Failed': '读取失败', 'Save Failed': '保存失败', 'Clear Failed': '清除失败',
    'Template = Persona speaking style (summary, traits, habits) + Vector library (real response examples & tags). A template package is a .zip file. Once imported, it can be applied in "Instructions & Persona".':
        '模板包含角色说话风格（摘要、特点和习惯）及真实回复示例库。模板包是 .zip 文件，导入后可在“人设与指令”中应用。',
    'Select private chat JSON exported by QQChatExporter, check speakers to learn from, and generate a template for each.':
        '选择 QQChatExporter 导出的私聊 JSON，勾选需要学习的发言者，为各发言者生成模板。',
    'Only shows messages stored locally for this friend': '只显示当前好友保存在本机的消息',
    'Local Chat History': '本地聊天记录', 'Unable to Confirm Bridge Status': '暂时无法确认桥接状态',
    'Clear Local Chat History': '清除本地聊天记录', 'Bridge is running; please stop it before clearing.': '桥接正在运行，请先停止服务再清除记录。',
    'Refresh Mood': '刷新心情', 'React emotionally when continuously ignored': '连续未获回复时表达情绪变化',
    'Continuously Ignored Behavior': '连续未获回复时的表现', 'After ignored': '连续未获回复', 'Mood Records': '心情记录',
    'When proactive chats, continuations, or care messages are ignored consecutively, persona feels slightly disappointed (per character style), speaks more reservedly without blaming or begging for replies, and logs it in mood records below. When friend returns, the first sentence reflects slight emotion before recovering.':
        '主动聊天、续聊或关怀消息连续未获回复时，角色会按人设表现轻微失落、适当收敛表达，不责怪或催求回复，并记录心情变化。好友回来时可自然表达情绪，再恢复交流。',
    'Saved; takes effect on next continuation or proactive evaluation.': '已保存，将在下次续聊或主动联系判断时生效。',
    'Current: Friend Uncertain · Persona Calm\nNo mood detection records yet': '当前：好友情绪不确定，角色平静\n尚无心情判断记录',
    'Lock new rows (protected from background consolidation)': '锁定新增条目，防止后台整理修改',
    'Persistent Memory': '长期记忆', 'Memory Entries': '记忆条目',
    'Persistent memory has unsaved changes. Please save before reloading.': '长期记忆尚有未保存的修改，请先保存再重新加载。',
    'Please select a memory entry in the table first.': '请先在表格中选择一条记忆。',
    'Text above has unsaved changes. Please save or reload first.': '上方文本尚有未保存的修改，请先保存或重新加载。',
    'Retire Memory': '停用记忆', 'This memory will no longer be used for generation, but will be preserved in "Entries & Sources".':
        '这条记忆将不再用于生成回复，但仍保留在“条目与来源”中供核查。',
    'Failed to Modify Memory': '修改记忆失败', 'Failed to Save Memory': '保存记忆失败',
    'Persistent memory saved; takes effect on next message generation.': '长期记忆已保存，下次生成回复时生效。',
    'Persistent memory reset for this friend; chat and knowledge base preserved.': '已重置当前好友的长期记忆，聊天记录和知识库已保留。',
    'Please save memory edits before rolling back version.': '请先保存记忆修改，再回滚版本。',
    'Restore previous version of persistent memory for this friend? Current version will be backed up.': '恢复当前好友的上一版长期记忆？当前版本会先备份。',
    'Persistent memory rolled back; chat history preserved.': '长期记忆已回滚，聊天记录已保留。',
    'Please start the bridge before rescheduling.': '请先启动桥接，再重新排班。',
    'A proactive chat preview is already generating.': '正在生成主动聊天预览，请稍候。',
    'Unable to Generate Preview': '无法生成预览', 'Send Real Proactive Chat Test': '发送真实主动聊天测试',
    'Test round submitted, awaiting bridge dispatch.': '测试轮次已提交，等待桥接发送。',
    'Requested re-evaluation; preserved limits, cooldown, and unreplied count.': '已请求重新评估，保留原有上限、冷却时间和未回复计数。',
    'A reschedule request is already pending bridge processing.': '已有排班请求等待桥接处理。',
    'Requested reschedule with saved settings; preserved completed rounds, cooldown, and unreplied count.':
        '已按保存的设置请求重新排班，保留已完成轮次、冷却时间和未回复计数。',
    'Preview uses current page topics and proactive style saved in Persona & Style; does not auto-save settings.':
        '预览使用当前页面的话题，以及“人设与回复方式”中已保存的主动聊天风格；不会自动保存当前设置。',
    'Persistent memory was updated by background process; please reload before editing.': '后台已更新长期记忆，请重新加载后再编辑。',
    'To clear persistent memory, please use the dedicated Reset button.': '如需清空长期记忆，请使用专门的“重置”按钮。',
    'fact': '事实', 'preference': '偏好', 'plan': '计划', 'observation': '观察', 'imported': '导入记录',
    'other': '其他', 'days': '近况', 'Consolidatable': '可整理',
})


ZH_CN.update({
    'API sources send queries and candidate texts to the configured service; local sources read local models only. Falls back to keyword/BM25 if unconfigured or failed. Vector cache is rebuilt when switching embedding models; rerank scores relevance without faking program scores as model scores.':
        '远程接口会发送查询及候选文本，本地模式只读取本机模型；未配置或不可用时降级为关键词检索。切换向量模型后会重建对应索引；程序检索分与模型重排分会分别标明。',
    'Each API saves keys independently. Enabled APIs become tools available to CLI and chat models. GET parameters are query params, POST parameters form JSON body; return payload is treated as data.':
        '各接口分别保存密钥；启用后可供智能体及聊天模型调用。GET 参数放在查询中，POST 参数组成 JSON 请求正文；返回内容只作数据。',
    'Retry Reply': '重试回复', 'Save failed': '保存失败', 'Basic Info': '好友资料',
    'Topic Knowledge': '话题知识库', 'API Settings': '接口设置', 'Base URL': '接口基础地址',
    'Template deleted.': '模板已删除。', 'Drafts discarded.': '草稿已丢弃。',
    'Also remove from bot QQ favorites': '同时从当前 QQ 账号的收藏中移除',
    'Generate Missing Meaning Cards via Codex': '使用 Codex 补全缺失的表情释义',
    'Meaning card saved.': '表情释义已保存。', 'Please select export file and check speakers to learn from.': '请选择导出的聊天文件，并勾选需要学习的发言者。',
    'Remove from Favorites': '从收藏中移除',
    'Remove this image from sticker collection (meaning card will be deleted and no longer suggested in replies).':
        '从表情收藏中移除此图片？对应释义卡也会删除，之后不再推荐用于回复。',
    'No earlier record of this type to compare.': '没有更早的同类记录可供比较。',
    'No generation records yet (only shows inputs after this version).': '尚无生成记录；这里只展示当前版本开始记录的输入。',
    'No templates yet: import a template package or generate from chat history below.': '尚无模板，可导入模板包，或通过下方聊天记录生成。',
    'Export Package': '导出模板包', 'Export Failed': '导出失败', 'Import Failed': '导入失败',
    'Each sticker has a meaning card (text, meaning, emotion, use cases). When replying, relevant stickers are retrieved for the model to choose from. Newly saved stickers get draft cards automatically from Codex; cards you edit and save will not be overwritten.':
        '每张表情都有释义卡，记录文字、含义、情绪和适用场景。回复时按需检索供模型选择；新收藏的表情会由 Codex 生成释义草稿，手动编辑保存的内容不会被自动覆盖。',
    '(Preview missing)': '（暂无预览）', 'Delete Sticker': '删除表情', 'Rewrite via Codex': '使用 Codex 重新生成',
    'Save Changes': '保存修改', 'Failed to Generate Meaning Cards': '生成表情释义失败',
    'Replies, proactive chats, scheduled care, and proactive triggers are composed in a fixed sequence. Within the same session, unchanged prefix blocks are cached, so "Actual Prompt" is usually shorter than "Full Input".':
        '回复、主动聊天、定时关怀与主动触发按固定顺序组织。同一会话会复用未变化的前文，所以“实际提示词”通常比“完整输入”短。',
    'Select a row to view the raw text of that block.': '选择一行，查看对应内容块的原文。',
    'Each generation records: input blocks with fingerprints, prompt versions, actual prompt, retrieval hits and scores, model output, duration, and delivered message count. Keeps the latest 300 runs.':
        '每次生成都会记录内容块及指纹、提示词版本、实际输入、检索命中与评分、模型输出、用时和已投递消息数；保留最近 300 次。',
    'Reset only persistent memory, memory versions, and phrasing candidates for this friend. Chat history, friend settings, and knowledge base are preserved. Reset cannot be undone; future consolidation will only process new messages.':
        '仅重置当前好友的长期记忆、记忆版本和候选措辞；聊天记录、好友设置及知识库会保留。重置无法撤销，之后只从新消息继续整理记忆。',
    'One memory per line. Checked: newly added lines are locked and protected. Existing entries are locked individually below. Resetting memory is separate from clearing chat history.':
        '每行填写一条记忆。勾选后新增条目会锁定保护；已有条目可在下方分别锁定。重置记忆不会清除聊天记录。',
    'Pinned: included in every prompt (user persona, relationship, recent status blocks, each with character limit). Other entries are recalled when relevant to chat context, or queried directly by the model. Status and plans expire automatically when due; retired entries are archived in "Entries & Sources".':
        '置顶条目会进入每次提示词中的好友画像、关系或近况区，各区有长度限制；其他条目按语境检索，或由模型主动查询。近况及计划到期后不再生效，停用条目保留在“条目与来源”中。',
    'Local LLM (Requires mounting in Engine page)': '本地模型（需先在模型引擎页面加载）',
    'Reset': '重置', 'Send': '发送', 'Copy': '复制', 'Apply': '应用', 'Edit': '编辑', 'Delete': '删除',
    'Import': '导入', 'Export': '导出', 'Stop': '停止', 'Start': '启动', 'Enabled': '已启用',
    'Disabled': '已停用', 'Unavailable': '不可用', 'Unknown': '未知', 'None': '无',
    'Clear': '清除', 'Settings': '设置', 'Entries & Sources': '条目与来源',
    'Friend Memory Entries & Sources': '好友记忆条目与来源', 'No persistent memories recorded': '尚无长期记忆记录',
    'Agent 开发工作台': '智能体工作台', 'Agent 协作网络': '智能体协作网络',
    '🔄 Engine': '🔄 模型引擎', '👥 Team Orchestration': '👥 协作编排', '🧠 Reasoning': '🧠 思考强度',
    '➕ New Chat': '➕ 新建对话', '📦 Model': '📦 模型', '⚡ Fast 模式': '⚡ 快速模式',
    '🧠 Long-Term Memory': '🧠 长期记忆', '📚 Topic Knowledge Library': '📚 话题知识库',
    'Adjust Reasoning Effort': '调整思考强度', 'Start Empty Chat Session': '创建空白对话',
    'Switch Active Model (Linked with CLI Engine)': '切换当前智能体使用的模型',
    'Toggle Fast Mode Acceleration': '切换快速模式', 'Slot': '位置', 'Block': '内容块', 'Type': '类型',
    'Chars': '字数', 'Yes': '是', 'Other': '其他', 'SendFailed': '发送失败', 'Send (Enter)': '发送（回车）',
    'Uncertain': '不确定', 'uncertain': '不确定', 'Calm': '平静', 'calm': '平静',
    'happy': '开心', 'surprised': '惊讶', 'sad': '难过', 'angry': '生气', 'annoyed': '烦躁',
    'reply': '回复', 'normal reply': '普通回复', 'continuation': '续聊', 'proactivedays': '主动聊天', 'research': '研究',
    'timing': '主动联系判断', 'task_notice': '任务通知', 'rule': '规则判断', 'model': '模型判断',
    'Mood history is recorded starting from this version; previous versions stored current state only.': '心情历史从当前版本开始记录；旧版只保存当前状态。',
    'Generation Comparison': '生成结果比较',
})


def bridge_status_text(text: str) -> str:
    import re
    value = str(text).strip()
    if value.lower() in ('not running', 'not running.'):
        return '消息桥接未启动'
    if value.lower().startswith(('running', 'online')):
        match = re.search(r'PID\s*[:：]?\s*(\d+)', value, re.I)
        return '消息桥接运行中' + (f'（进程 {match.group(1)}）' if match else '')
    if value.lower() == 'unable to read status':
        return '暂时无法读取消息桥接状态'
    return value


ZH_CN.update({
    'Off (Off)': '关闭思考', 'Off (不支持思考)': '不支持调整思考强度',
    'Low (低)': '低', 'Medium (中)': '中', 'High (高)': '高',
    'Extra (超高)': '超高', 'Max (最大)': '最高', 'Ultra (极致)': '极高',
    'Connect QQ': '连接 QQ',
    'Backend Bridge: Starting QQ link and backend...': '后端连接：正在启动 QQ 直连和后端…',
    'QQ Link': 'QQ 直连',
})


def tr(text: str) -> str:
    """Return the Chinese presentation label unless English is requested."""
    lang = os.environ.get('OPENQQ_UI_LANG', 'zh-CN').strip().lower().replace('_', '-')
    if lang.startswith('zh'):
        if text in ZH_CN:
            return ZH_CN[text]
        import re
        tool = re.fullmatch(r'Tool \[(.+)\] requesting sensitive operation execution', text)
        if tool:
            return f'工具「{tool.group(1)}」请求执行需要确认的操作'
        prefixes = {
            'Failed to read proactive status: ': '读取主动任务状态失败：',
            'Failed to read chat history: ': '读取聊天记录失败：',
            'Task completed: ': '任务已完成：', 'Task failed: ': '任务失败：',
            'Task needs your review: ': '任务需要确认：', 'Goal Status Update': '长期目标进展',
            'Plan Long-term Goal: ': '规划长期目标：',
            'State transition -> ': '状态变更：',
            'Task created and enqueued ': '任务已创建并加入队列 ',
            'Execution failed: ': '执行失败：',
        }
        for prefix, translated in prefixes.items():
            if text.startswith(prefix):
                remainder = text[len(prefix):]
                if prefix == 'State transition -> ':
                    labels = {'PENDING': '等待执行', 'RUNNING': '正在执行', 'VERIFYING': '正在验收',
                              'COMPLETED': '已完成', 'FAILED': '失败', 'CANCELLED': '已取消',
                              'PAUSED': '已暂停', 'WAITING_APPROVAL': '等待确认', 'WAITING_RESOURCE': '等待资源'}
                    code, separator, suffix = remainder.partition(' ')
                    remainder = labels.get(code, code) + separator + suffix
                return translated + remainder
        return text
    return text
