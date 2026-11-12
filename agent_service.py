"""Agent Service layer providing core multi-agent execution, model catalog and thinking extraction.

Shared cleanly between the desktop Qt console (agent_console_widget.py)
and the mobile Web console (house_web.py / house_web.html).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

import codex_settings
import session_manager

ROOT = Path(os.environ.get('QQ_BRIDGE_HOME', Path(__file__).resolve().parent))


def send_friend_attachment(qq, path, kind='auto'):
    from qq_media import send
    return send(ROOT, str(qq or ''), path, kind)


def _friend_dir(qq: str) -> Path:
    return session_manager._friend_dir(qq)


def send_friend_message(qq, text, identity: str = 'friend'):
    """Shared native/mobile delivery; only an actual OneBot receipt records a send."""
    identity = 'model' if identity == 'model' else 'friend'
    from qq_link import QQClient
    from store import FriendStore
    qq = str(qq or '').strip()
    if not re.fullmatch(r'\d{5,12}', qq):
        raise ValueError('请先添加并选择一位好友')
    from delivery_claims import inspect as inspect_delivery_claims
    checked = inspect_delivery_claims(str(text or ''))
    if not checked.allowed:
        text = checked.sanitized
    registry = json.loads((ROOT / 'friends.json').read_text('utf-8'))
    record = registry.get('friends', {}).get(qq)
    if not record or not (ROOT / 'friends' / qq / 'friend.json').is_file():
        raise ValueError('该好友尚未绑定，请先在好友总览中添加')
    client = QQClient()
    try:
        login = client.call('get_login_info')
        bound_account = str(record.get('self_id') or '')
        if not isinstance(login, dict) or not bound_account.isdigit() or str(login.get('user_id')) != bound_account:
            raise PermissionError('当前 QQ 登录账号与好友绑定账号不同，已停止发送')
        receipt = client.send_private_msg(qq, text)
    finally:
        client.close()
    receipt = dict(receipt)
    receipt['local_saved'] = False
    try:
        store = FriendStore(_friend_dir(qq))
        try:
            store.add_message(str(receipt['message_id']), 'me', text, identity=identity)
        finally:
            store.close()
        receipt['local_saved'] = True
    except Exception:
        receipt['warning'] = 'QQ 已确认发送，但本地记录同步失败；请勿重复发送'
    return receipt


def extract_clean_reply(raw_text: str, agent_name: str = "") -> tuple[str, str]:
    """Strip all leaked prompt instructions, template dumps, and echoes. Return (clean_reply, thinking)."""
    if not raw_text:
        return "", ""

    text = raw_text.strip()
    thinking = ""

    # 1. Extract and remove <think>...</think>
    if '<think>' in text:
        m = re.search(r'<think>([\s\S]*?)</think>', text)
        if m:
            thinking = m.group(1).strip()
            text = text.replace(m.group(0), '').strip()
        else:
            parts = text.split('<think>')
            if len(parts) > 1:
                thinking = parts[1].strip()
                text = parts[0].strip()

    # 2. Strip any echoes of prompt sections/brackets
    leak_header_pattern = r'\[(?:Agent Workspace Session|Persona Template|Speaking Style|Role Configuration|Current User|External Facts|Long-Term Facts|Factual Memories|Topic Knowledge|Recent History|Core Requirements|Absolute Output Prohibition|Deep Reasoning Requirements|Fast Response Mode|Output Rules)[\s\S]*?\]'
    text = re.sub(leak_header_pattern, '', text, flags=re.MULTILINE).strip()

    # Strip SPEAK_AS_TA or TAG blocks
    text = re.sub(r'SPEAK_AS_TA:[\s\S]*?(?:STYLE_EXAMPLE|OPENER_EXAMPLE|\[TAG:[^\]]+\])', '', text).strip()
    text = re.sub(r'\[TAG:[^\]]+\][\s\S]*?(?=\n\n|\Z)', '', text).strip()

    # 3. If model echoed conversation history like "... \n\n "
    names = [re.escape(agent_name), r'Dot Agent', r'Agent Assistant', r'Agent', r'Assistant', r'AI']
    names_pattern = '|'.join(filter(None, names))
    split_regex = rf'(?:^|\n)(?:用户|User)[：:][\s\S]*?(?:{names_pattern})[：:]\s*'
    parts = re.split(split_regex, text)
    if len(parts) > 1 and parts[-1].strip():
        text = parts[-1].strip()

    # 4. Remove leftover role prefixes like "Dot " or ""
    text = re.sub(rf'^(?:{names_pattern})[:]\s*', '', text).strip()

    # 5. Remove polite meta preamble like ""
    meta_preamble_pattern = r'^(?:Okay|Understood|Sure)[,!]?(?:according to|in response to).*?(?:here is my reply|reply as follows)[:\n]\s*'
    text = re.sub(meta_preamble_pattern, '', text).strip()
    text = re.sub(r'^(?:Here is my (?:response|reply)[:\n]\s*)', '', text, flags=re.I).strip()

    return text.strip(), thinking.strip()


def get_provider_model_structure(actual_provider: str) -> tuple[str, list[tuple[str, str, str]], list[tuple[str, str]]]:
    from agent_tool_adapters import AGENT_BACKENDS
    if actual_provider in AGENT_BACKENDS:
        return '工具模型配置', [('沿用工具配置', '沿用工具配置', '')], []
    """Return (header_title, main_models, overflow_models) matching Codex and Claude Code client UX."""
    if actual_provider == 'claude':
        header_title = "Model · this session"
        main_models = [
            ('claude-opus-5-5', 'Opus 5.5', '1'),
            ('claude-sonnet-5-5', 'Sonnet 5.5', '2'),
            ('claude-fable-5-1', 'Fable 5.1  (Requires usage credits)', '3'),
            ('claude-haiku-5-5', 'Haiku 5.5', ''),
        ]
        overflow_models = [
            ('claude-haiku-4-5-20251001', 'Haiku 4.5'),
            ('claude-sonnet-5', 'Sonnet 5'),
            ('claude-opus-5', 'Opus 5'),
            ('claude-fable-5', 'Fable 5  (Requires usage credits)'),
            ('claude-opus-4-8', 'Opus 4.8'),
            ('claude-opus-4-7', 'Opus 4.7'),
            ('claude-opus-4-6', 'Opus 4.6'),
            ('claude-sonnet-4-6', 'Sonnet 4.6'),
        ]
        return header_title, main_models, overflow_models

    elif actual_provider == 'codex':
        header_title = "Model · this session"
        main_models = [
            ('gpt-6.1-sol', 'GPT-6.1 Sol', '1'),
            ('gpt-6-astra', 'GPT-6 Astra', '2'),
            ('gpt-6-sol', 'GPT-6 Sol', '3'),
            ('gpt-6-luna', 'GPT-6 Luna', ''),
        ]
        overflow_models = [
            ('gpt-5.6-sol', 'GPT-5.6 Sol'),
            ('gpt-5.6-terra', 'GPT-5.6 Terra'),
            ('gpt-5.6-luna', 'GPT-5.6 Luna'),
            ('gpt-4o', 'GPT-4o'),
        ]
        return header_title, main_models, overflow_models

    elif actual_provider == 'dsh':
        header_title = "DeepSeek Account"
        main_models = [
            ('DeepSeek-V41-Flash', 'DeepSeek-V41-Flash', ''),
            ('DeepSeek-V4-Pro', 'DeepSeek-V4-Pro', ''),
        ]
        overflow_models = []
        return header_title, main_models, overflow_models

    elif actual_provider == 'antigravity':
        header_title = "Model · this session"
        main_models = [
            ('gemini-3.8-flash-high', 'Gemini 3.8 Flash High', '1'),
            ('gemini-3.8-flash-medium', 'Gemini 3.8 Flash Medium', '2'),
            ('gemini-3.8-flash-low', 'Gemini 3.8 Flash Low', '3'),
        ]
        overflow_models = [
            ('claude-sonnet-4-6', 'Claude Sonnet 4.6'),
            ('claude-opus-4-6-thinking', 'Claude Opus 4.6 Thinking'),
            ('gpt-oss-120b-medium', 'GPT-OSS 120B Medium'),
        ]
        return header_title, main_models, overflow_models

    else:  # custom
        header_title = "API Service Account"
        main_models = [
            ('deepseek-chat', 'DeepSeek V3 (Chat)', ''),
            ('deepseek-reasoner', 'DeepSeek R1 (Reasoner)', ''),
            ('gpt-4o', 'GPT-4o', ''),
            ('claude-3-5-sonnet', 'Claude 3.5 Sonnet', ''),
        ]
        overflow_models = []
        return header_title, main_models, overflow_models


def get_model_efforts(actual_provider: str, model_id: str) -> list[tuple[str, str]]:
    from agent_tool_adapters import AGENT_BACKENDS
    if actual_provider in AGENT_BACKENDS:
        return [('沿用工具配置', '沿用工具配置')]
    """Return the precise reasoning efforts supported by the specific model according to official specs."""
    model_clean = (model_id or '').lower()

    # 1. Google Gemini (Antigravity): Absolutely NO Extra, NO Max!
    if actual_provider == 'antigravity' or 'gemini' in model_clean:
        if 'gemini' in model_clean or actual_provider == 'antigravity' and not ('claude' in model_clean or 'thinking' in model_clean):
            return [
                ('off', 'Off (关闭)'),
                ('low', 'Low (低)'),
                ('medium', 'Medium (中)'),
                ('high', 'High (高)'),
            ]
        elif 'opus-4-6' in model_clean or 'thinking' in model_clean:
            return [
                ('low', 'Low (低)'),
                ('medium', 'Medium (中)'),
                ('high', 'High (高)'),
                ('max', 'Max (最大)'),
            ]
        else:
            return [
                ('off', 'Off (关闭)'),
                ('low', 'Low (低)'),
                ('medium', 'Medium (中)'),
                ('high', 'High (高)'),
            ]

    # 2. Codex CLI (OpenAI)
    elif actual_provider == 'codex' or 'gpt' in model_clean:
        if 'gpt-4o' in model_clean or '4o' in model_clean:
            return [('none', 'Off (No reasoning)')]
        try:
            options = codex_settings.model_options()
            if model_id in options:
                raw_levels = options[model_id]
                level_names = {
                    'none': 'Off (关闭)',
                    'off': 'Off (关闭)',
                    'low': 'Low (低)',
                    'medium': 'Medium (中)',
                    'high': 'High (高)',
                    'xhigh': 'Extra (超高)',
                    'max': 'Max (最大)',
                    'ultra': 'Ultra (极致)',
                }
                return [(lvl, level_names.get(lvl, lvl.capitalize())) for lvl in raw_levels]
        except Exception:
            pass

        if any(m in model_clean for m in ('6.1-sol', '6-astra', '6-sol', '5.6-sol', '5.6-terra')):
            return [
                ('none', 'Off (关闭)'),
                ('low', 'Low (低)'),
                ('medium', 'Medium (中)'),
                ('high', 'High (高)'),
                ('xhigh', 'Extra (超高)'),
                ('max', 'Max (最大)'),
                ('ultra', 'Ultra (极致)'),
            ]
        elif any(m in model_clean for m in ('6-luna', '5.6-luna')):
            return [
                ('none', 'Off (关闭)'),
                ('low', 'Low (低)'),
                ('medium', 'Medium (中)'),
                ('high', 'High (高)'),
                ('xhigh', 'Extra (超高)'),
                ('max', 'Max (最大)'),
            ]
        else:
            return [
                ('none', 'Off (关闭)'),
                ('low', 'Low (低)'),
                ('medium', 'Medium (中)'),
                ('high', 'High (高)'),
            ]

    # 3. Claude Code (Anthropic)
    elif actual_provider == 'claude' or 'claude' in model_clean:
        if 'haiku-4-5' in model_clean:
            return [('off', 'Off (No reasoning)')]
        elif '4-6' in model_clean:
            return [
                ('off', 'Off (关闭)'),
                ('low', 'Low (低)'),
                ('medium', 'Medium (中)'),
                ('high', 'High (高)'),
                ('max', 'Max (最大)'),
            ]
        else:  # 5.5 and 5 series
            return [
                ('off', 'Off (关闭)'),
                ('low', 'Low (低)'),
                ('medium', 'Medium (中)'),
                ('high', 'High (高)'),
                ('max', 'Max (最大)'),
            ]

    # 4. DeepSeek
    elif actual_provider == 'dsh' or 'deepseek' in model_clean:
        if 'flash' in model_clean or 'v41' in model_clean:
            return [
                ('off', 'Off (关闭)'),
                ('low', 'Low (低)'),
                ('medium', 'Medium (中)'),
                ('high', 'High (高)'),
            ]
        else:  # Pro
            return [
                ('off', 'Off (关闭)'),
                ('low', 'Low (低)'),
                ('medium', 'Medium (中)'),
                ('high', 'High (高)'),
                ('max', 'Max (Deep Reasoning)'),
            ]

    # 5. Custom / Fallback
    return [
        ('none', 'Off (关闭)'),
        ('low', 'Low (低)'),
        ('medium', 'Medium (中)'),
        ('high', 'High (高)'),
    ]


def submit_workbench_task(qq, ws_id, sid, text, provider=None):
    """The native/mobile workbench submits to the same durable task engine as QQ."""
    import lexicon
    import task_spec
    from task_manager import TaskManager
    friend_dir = _friend_dir(qq)
    profile = json.loads((friend_dir / 'friend.json').read_text('utf-8'))
    if str(profile.get('qq_id')) != str(qq):
        raise PermissionError('任务所属好友不匹配')
    workspace = next((item for item in session_manager.list_workspaces(qq) if item['id'] == ws_id), None)
    if workspace is not None:
        # 2026-10-26 user: "他不会自己创建目录吗" — the friend's own workspace folder is ours to create.
        Path(workspace['path']).mkdir(parents=True, exist_ok=True)
    if workspace is None or not Path(workspace['path']).is_dir():
        raise ValueError('请先选择有效的项目目录')
    session_manager.load_session(qq, sid, ws_id)
    if not any(session['id'] == sid for session in session_manager.list_sessions(qq, ws_id)):
        raise ValueError('请选择有效的工作台会话')
    description = lexicon.EXPLICIT_TASK_STRIP.sub('', text).strip()
    if not description:
        raise ValueError('请填写要执行的任务')
    category = lexicon.task_category(description)
    spec = task_spec.build_task_spec(description, category, provider or (profile.get('model_api') or {}).get('provider', ''))
    if category == 'browser' and not spec.get('url'):
        raise ValueError('浏览器任务还需要网页地址')
    spec.update(input_source='workbench', spec_source='workbench', workspace_id=ws_id, session_id=sid, notify_qq=False)
    api = profile.get('model_api') or {}
    if api.get('multi_agent_enabled') or (api.get('multi_agent') or {}).get('enabled'):
        spec.pop('backend', None)
    if provider and provider != 'multi_agent' and not ((api.get('multi_agent') or {}).get('enabled')):
        spec['backend'] = provider
    manager = TaskManager.get_instance()
    task_id = manager.create_task(description[:60], description, category=category, owner_id=qq,
        initial_checkpoint=spec, max_duration_seconds=600, max_calls=20, token_budget=100000)
    manager.start_worker()
    return {'task_id': task_id, 'category': category,
            'reply': f'任务已提交。\n任务编号：{task_id}\n执行进度、结果和日志会保存在任务中心。'}


def execute_agent_turn(qq: str, ws_id: str, sid: str, text: str,
                       model: str | None = None, effort: str | None = None,
                       provider: str | None = None, mode: str = 'task') -> dict:
    """Execute a single agent conversation turn and store messages."""
    if mode not in ('task', 'chat'):
        raise ValueError('请选择任务或对话模式')
    from skill_registry import expand
    text = expand(text)
    from house_status import load_profile as _load_profile
    _friend = _friend_dir(qq)
    _profile = _load_profile(_friend / 'friend.json')
    _api = _profile.get('model_api') or {}
    _actual = provider or _api.get('provider') or 'codex'
    if str(_actual).startswith('webchat_'):
        _global_web = _api.get('web_agent') if isinstance(_api.get('web_agent'), dict) else {}
        _providers = _api.get('web_agent_providers') if isinstance(_api.get('web_agent_providers'), dict) else {}
        _web = _providers.get(str(_actual)) if isinstance(_providers.get(str(_actual)), dict) else _global_web
        if _web.get('enabled'):
            import web_agent
            _source = str(_actual)[len('webchat_'):]
            _models = _api.get('webchat_models') if isinstance(_api.get('webchat_models'), dict) else {}
            _efforts = _api.get('webchat_efforts') if isinstance(_api.get('webchat_efforts'), dict) else {}
            _result = web_agent.run_task(
                _friend, text, _source, model=str(model or _models.get(str(_actual)) or ''),
                effort=str(effort or _efforts.get(str(_actual)) or ''),
                max_steps=50,
                max_context_chars=int(_web.get('context_chars', 64_000) or 64_000),
                allow_config=bool(_profile.get('chat_config_edits', False)))
            session_manager.append_message(qq, sid, 'user', text, ws_id=ws_id, agent_id=str(_actual))
            _answer = str(_result.get('answer') or _result.get('error') or '网页 Agent 未返回内容')
            session_manager.append_message(qq, sid, 'assistant', _answer, ws_id=ws_id, agent_id=str(_actual))
            return {'task_id': f'web-agent-{int(__import__("time").time())}', 'category': 'web_agent',
                    'reply': _answer, 'web_agent': _result}
    if mode == 'task':
        result = submit_workbench_task(qq, ws_id, sid, text, provider)
        session_manager.append_message(qq, sid, 'user', text, ws_id=ws_id, agent_id=str(_actual))
        session_manager.append_message(qq, sid, 'assistant', result['reply'], ws_id=ws_id, agent_id=str(_actual))
        return result
    from house_status import load_profile
    from main import codex_text
    import persona_template
    from store import FriendStore

    friend_dir = _friend_dir(qq)
    profile_path = friend_dir / 'friend.json'
    profile = load_profile(profile_path)
    persona = profile.get('persona', '')
    basic = profile.get('basic_info', '')
    style = profile.get('style', '')

    workspaces = session_manager.list_workspaces(qq)
    ws_item = next((w for w in workspaces if w['id'] == ws_id), None)
    if not ws_item:
        raise ValueError('Friend does not have this workspace')
    from workspace_tools import workspace, enabled
    from tool_registry import validate_path_in_bounds
    authorized_workspace = workspace(friend_dir)
    cwd_dir = authorized_workspace if ws_item.get('is_default') else validate_path_in_bounds(
        Path(ws_item['path']), authorized_workspace, enabled(friend_dir))

    # Append user message
    u_msg = session_manager.append_message(qq, sid, 'user', text, ws_id=ws_id, agent_id=str(_actual))

    # Active persona template
    active_tpl = persona_template.active(profile)
    agent_role_name = "Dot Agent"
    template_instruction = ""
    if active_tpl:
        tpl_name = active_tpl.get('name', 'Untitled Template')
        speaker = active_tpl.get('source', {}).get('speaker', '')
        agent_role_name = speaker or tpl_name
        tpl_block = persona_template.prompt_block(profile, friend_dir, text, scene='workbench', kind='reply')
        template_instruction = (
            f"[Persona Template Reference: {tpl_name}]\n"
            f"Your speaking style and habits strictly follow template '{tpl_name}' (Role: {agent_role_name}).\n"
            f"Template Summary: {active_tpl.get('summary', '')}\n"
            f"{tpl_block}\n"
            f"[Core Requirement]: Reply in the voice of role '{agent_role_name}', referencing dialogue examples, tone, and traits."
        )

    # Reasoning effort prompt
    effort_prompt = ""
    if effort in ('high', 'xhigh', 'max', 'ultra'):
        effort_prompt = "[Deep Reasoning Requirement]: Expand logical deduction and code analysis, providing reasoning traces (may include <think>...</think>)."
    elif effort in ('none', 'off'):
        effort_prompt = "[Fast Response Mode]: Provide concise conclusions directly without verbose elaboration."

    prompt_parts = [
        "[Agent Workspace Session]",
        f"You are a professional autonomous AI agent (Role: {agent_role_name}).",
        f"Active workspace: {ws_item['title'] if ws_item else 'Default'} (Directory: {cwd_dir}).",
    ]
    if effort_prompt:
        prompt_parts.append(effort_prompt)
    if template_instruction:
        prompt_parts.append(template_instruction)
    elif persona:
        prompt_parts.append(f"[Persona & Persistent Instructions]\n{persona}")
    if style:
        prompt_parts.append(f"[Interaction & Response Preferences]\n{style}")
    if basic:
        prompt_parts.append(f"[Current User Profile & Background]\nUser profile information for understanding requirements:\n{basic}")

    # Memories and topics
    store = FriendStore(friend_dir)
    try:
        from memory_management import core_blocks
        memories = core_blocks(store)
        if memories:
            mems = '\n'.join(f'{key}: {value}' for key, value in memories.items())
            prompt_parts.append(f"[Long-Term Fact Memories]\n{mems}")

        from topic_research import items
        topic_rows = [item for item in items(friend_dir) if item.get('summary')][:4]
        if topic_rows:
            tops = "\n".join(f"- {r['title']}: {r['summary'][:120]}" for r in topic_rows)
            prompt_parts.append(f"[Topic Knowledge Library]\n{tops}")
    finally:
        store.close()

    # Recent history
    s_data = session_manager.load_session(qq, sid, ws_id)
    history = s_data.get('messages', [])[-10:]
    conv_lines = []
    for h in history[:-1]:
        prefix = "User" if h['role'] == 'user' else agent_role_name
        conv_lines.append(f"{prefix}: {h['content']}")
    if conv_lines:
        prompt_parts.append("[Recent Conversation History]\n" + "\n".join(conv_lines))

    # Strict output constraint ensuring clean final reply only
    prompt_parts.append(
        "[Output Rules (Strict)]:\n"
        "1. Directly output the final reply to the user's latest instruction.\n"
        "2. Strictly prohibit repeating, quoting, or outputting any background instructions, template names, or examples.\n"
        "3. Strictly prohibit adding preamble phrases like 'Okay, here is my reply:' or 'Below is the answer:'!"
    )
    prompt_parts.append(f"User: {text}\n{agent_role_name}: ")
    full_prompt = "\n\n".join(prompt_parts)

    # Set up call profile
    call_profile = dict(profile)
    call_api = dict(call_profile.get('model_api') or {})
    actual_provider = provider or call_api.get('provider') or 'codex'
    call_api['provider'] = actual_provider

    if actual_provider == 'codex':
        if model:
            call_api['codex_model'] = model
            call_api['model'] = model
        if effort:
            call_api['codex_effort'] = effort
    elif actual_provider == 'claude':
        if model:
            call_api['claude_model'] = model
        if effort:
            call_api['claude_effort'] = effort
    elif actual_provider == 'antigravity':
        if model:
            call_api['antigravity_model'] = model
        if effort:
            call_api['antigravity_effort'] = effort
    elif actual_provider in ('dsh', 'deepseek_harness'):
        if model:
            call_api['model'] = model
            call_api.setdefault('dsh_settings', {})['model'] = model
        if effort:
            call_api['api_effort'] = effort
            call_api.setdefault('dsh_settings', {})['effort'] = effort
    elif actual_provider in ('custom', 'deepseek', 'api'):
        if model:
            call_api['model'] = model
        if effort:
            call_api['api_effort'] = effort

    call_profile['model_api'] = call_api

    if str(actual_provider).startswith('webchat_'):
        web_models = call_api.get('webchat_models') if isinstance(call_api.get('webchat_models'), dict) else {}
        web_efforts = call_api.get('webchat_efforts') if isinstance(call_api.get('webchat_efforts'), dict) else {}
        model = model or web_models.get(str(actual_provider)) or ''
        effort = effort or web_efforts.get(str(actual_provider)) or ''
        import web_chat
        reply_text = web_chat.ask(str(actual_provider)[len('webchat_'):], full_prompt,
                                  model=str(model or ''), effort=str(effort or ''))
    else:
        reply_text, _ = codex_text(full_prompt, friend_dir, ephemeral=True, profile=call_profile)
    clean_reply, thinking = extract_clean_reply(reply_text or "", agent_role_name)

    a_msg = session_manager.append_message(qq, sid, 'assistant', clean_reply, thinking=thinking, ws_id=ws_id, agent_id=str(_actual))
    return {
        'reply': clean_reply,
        'thinking': thinking,
        'timestamp': a_msg.get('timestamp', ''),
        'id': a_msg.get('id', ''),
        'agent_name': agent_role_name
    }
