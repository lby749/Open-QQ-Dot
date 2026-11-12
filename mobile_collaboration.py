"""Paired-phone collaboration over the same friend-owned rooms as the desktop."""
import json
from pathlib import Path
import re

import agent_group


def owner_directory(root, owner):
    if not isinstance(owner, str) or not re.fullmatch(r'\d{5,12}', owner):
        raise ValueError('请选择有效好友或机器人会话')
    directory = Path(root) / 'friends' / owner
    if directory.is_symlink() or not directory.resolve().is_relative_to((Path(root)/'friends').resolve()):
        raise PermissionError('会话目录超出授权范围')
    profile = json.loads((directory/'friend.json').read_text('utf-8'))
    if str(profile.get('qq_id')) != owner:
        raise PermissionError('好友配置归属不匹配')
    return directory, profile


def available(root, owner):
    from task_workflows import providers
    directory, _ = owner_directory(root, owner)
    return providers(directory)


def action(root, owner, body):
    """Caller authenticates the device and owner before entering this function."""
    root = Path(root)
    directory, profile = owner_directory(root, owner)
    allowed = available(root, owner)
    base = root/'data'
    operation = body.get('action')
    if operation == 'list':
        return {'rooms': agent_group.list_rooms(base, owner), 'providers': allowed}
    if operation == 'create':
        title = body.get('title', '')
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 80:
            raise ValueError('请输入 1–80 字的协作名称')
        return {'room': agent_group.create_room(base, owner, title.strip())}
    room_id = str(body.get('room_id') or '')
    room = agent_group.load_room(base, owner, room_id)
    if operation == 'get':
        from task_manager import TaskManager
        manager = TaskManager.get_instance(root/'data/tasks.sqlite3', root)
        with manager._get_connection() as db:
            task = db.execute("SELECT task_id FROM tasks WHERE owner_id=? AND json_extract(checkpoint,'$.agent_group_room')=? ORDER BY created_at DESC LIMIT 1", (owner, room_id)).fetchone()
        detail = manager.get_task(task['task_id']).to_dict() if task else None
        checkpoint = (detail or {}).get('checkpoint') or {}
        return {'room': {**room, 'messages': room.get('messages', [])[-80:]},
                'routing': checkpoint.get('mother_plan') or {},
                'completed_members': [member['id'] for member in room['members']
                                      if member['id'] in (checkpoint.get('collaboration_results') or {})],
                'task': {key: detail.get(key) for key in ('task_id', 'status', 'error', 'result', 'verification_info')} if detail else None}
    if operation == 'set_host':
        member_id = str(body.get('member_id') or '')
        if member_id and not any(member['id'] == member_id for member in room['members']):
            raise ValueError('主智能体必须是当前协作群的成员')
        return {'room': agent_group.update_room(base, owner, room_id, host_id=member_id)}
    if operation == 'add':
        provider, role, name = body.get('provider'), body.get('role', ''), body.get('name', '')
        if provider not in allowed:
            raise PermissionError('该智能体尚未在当前好友下启用，请先在桌面配置')
        if len(room.get('members', [])) >= 6:
            raise ValueError('每轮最多 6 位成员，避免重复上下文和无界调用')
        if not isinstance(role, str) or not 1 <= len(role.strip()) <= 180 or not isinstance(name, str) or len(name) > 32:
            raise ValueError('成员名称最多 32 字，分工须为 1–180 字')
        agent_group.add_member(base, owner, room_id, provider, name, role)
        return {'room': agent_group.load_room(base, owner, room_id)}
    if operation == 'remove':
        member_id = str(body.get('member_id') or '')
        room['members'] = [member for member in room['members'] if member['id'] != member_id]
        if room.get('host_id') == member_id:
            room['host_id'] = ''
        return {'room': agent_group.save_room(base, owner, room)}
    if operation != 'run':
        raise ValueError('未知协作操作')
    text, request_id = body.get('text', ''), body.get('request_id', '')
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 4000 or not re.fullmatch(r'[A-Za-z0-9_-]{8,64}', str(request_id)):
        raise ValueError('任务须为 1–4000 字，并带有有效请求标识')
    members = room.get('members') or []
    if not 1 <= len(members) <= 6 or any(m['provider'] not in allowed for m in members):
        raise ValueError('请先添加 1–6 位已在当前好友启用的成员')
    if room.get('mode') != 'sequential':
        raise ValueError('手机持久化协作目前使用顺序交接，请在桌面将此群改为顺序发言')
    from task_manager import TaskManager
    manager = TaskManager.get_instance(root/'data/tasks.sqlite3', root)
    key = 'mobile_group:' + owner + ':' + room_id + ':' + str(request_id)
    with manager._db_lock, manager._get_connection() as db:
        prior = db.execute("SELECT task_id,checkpoint,status FROM tasks WHERE owner_id=? AND json_extract(checkpoint,'$.agent_group_room')=? ORDER BY created_at DESC LIMIT 20", (owner, room_id)).fetchall()
        for task in prior:
            if json.loads(task['checkpoint']).get('mobile_request_key') == key:
                return {'task_id': task['task_id'], 'duplicate': True}
        if any(task['status'] not in ('COMPLETED', 'CANCELLED') for task in prior):
            raise ValueError('此协作群上一轮尚未结束，请先在任务中心处理，避免重复执行')
        task_id = manager.create_task('手机协作：' + room['title'], text.strip(), category='data', owner_id=owner,
            max_retries=0, max_duration_seconds=600, max_calls=len(members)*6+2, token_budget=600000,
            idempotency_key=key, initial_checkpoint={'agent_group_room': room_id,
                'mobile_request_key': key, 'expected_keys': ['messages'],
                'host_id': room.get('host_id') or '',
                'member_config': members,
                'member_ids': [member['id'] for member in members]})
    manager.start_worker()
    return {'task_id': task_id, 'duplicate': False}


def execute(task, directory, profile, manager, log):
    """Run a bounded persistent turn, sharing real replies rather than duplicated prompts."""
    from agent_model_selection import options, apply as selected_profile
    from multi_agent_orchestrator import call_single_agent
    from store import FriendStore
    from task_runtime import current_execution
    root, owner = manager.workspace, task.owner_id
    base, room_id = root/'data', task.checkpoint['agent_group_room']
    room = agent_group.load_room(base, owner, room_id)
    members = room.get('members') or []
    if [member['id'] for member in members] != task.checkpoint['member_ids']:
        raise ValueError('排队期间成员发生变化，已停止，请检查后重新提交')
    if (room.get('host_id') or '') != task.checkpoint.get('host_id', room.get('host_id') or ''):
        raise ValueError('排队期间主智能体发生变化，已停止，请检查后重新提交')
    expected_members = task.checkpoint.get('member_config', members)
    expected_host = room.get('host_id') or ''
    if members != expected_members:
        raise ValueError('排队期间成员模型或分工发生变化，已停止，请检查后重新提交')
    allowed = available(root, owner)
    if any(member['provider'] not in allowed for member in members):
        raise PermissionError('有成员已被当前好友禁用，未继续执行')
    store = FriendStore(directory)
    try:
        memory = store.memory()
    finally:
        store.close()
    checkpoint = dict(task.checkpoint)
    results = checkpoint.setdefault('collaboration_results', {})
    def save():
        current = manager.get_task(task.task_id).checkpoint
        owned_keys = {key for key in current.keys() | checkpoint.keys()
                      if key.startswith('collaboration_') or key == 'mother_plan'}
        for key in owned_keys:
            if key in checkpoint:
                current[key] = checkpoint[key]
            else:
                current.pop(key, None)
        manager.save_checkpoint(task.task_id, current)

    def call_once(key, member, prompt):
        execution = current_execution.get()
        if execution:
            execution.check()
        current = agent_group.load_room(base, owner, room_id)
        if (current['members'] != expected_members
                or (current.get('host_id') or '') != expected_host
                or member['provider'] not in available(root, owner)):
            raise PermissionError('执行期间成员、主智能体或授权发生变化，已停止')
        if key in results:
            return results[key]
        if checkpoint.get('collaboration_inflight'):
            raise InterruptedError('上次模型请求结果尚未确认，保留检查点；请先核查，不自动重发')
        value = selected_profile(profile, member['provider'], member.get('model', ''), member.get('effort', ''))
        if member.get('template'):
            value['persona_template'] = member['template']
        checkpoint['collaboration_inflight'] = key
        save()
        reply, _ = call_single_agent(member['provider'], prompt, directory, value,
                                     ephemeral=True, raise_on_error=True, max_retries=0, local_ok=False)
        if not str(reply or '').strip():
            raise RuntimeError(member['name'] + ' 没有返回结果，未标记成功')
        results[key] = str(reply)
        checkpoint.pop('collaboration_inflight', None)
        save()
        return str(reply)

    if not checkpoint.get('collaboration_user_written'):
        agent_group.append_message(base, owner, room_id, task.description, role='user', name='手机用户')
        checkpoint['collaboration_user_written'] = True
        save()
    shared = agent_group.shared_memory(base, owner, room, memory)
    host = next((member for member in members if member['id'] == room.get('host_id')), None)
    selected = members
    if host:
        plan = checkpoint.get('mother_plan')
        if not plan:
            catalog = options(profile, [member['provider'] for member in members], members=members)
            raw = call_once('mother_plan', host, agent_group.dispatch_prompt(room, host, task.description, catalog, memory=shared))
            plan = agent_group.parse_dispatch(raw, room, host, catalog)
            checkpoint['mother_plan'] = plan
            save()
        selected = plan['members']
        if not selected:
            if not checkpoint.get('collaboration_dispatch_written'):
                agent_group.append_message(base, owner, room_id, plan['direct_reply'], role='host', name=host['name'], provider=host['provider'])
                checkpoint['collaboration_dispatch_written'] = True
                save()
            return {'output': json.dumps({'messages': [plan['direct_reply']]}, ensure_ascii=False), 'usage_recorded': True}
        if not checkpoint.get('collaboration_dispatch_written'):
            dispatch = plan['summary'] + '\n' + '\n'.join(member['name'] + '：' + member['assignment'] for member in selected)
            agent_group.append_message(base, owner, room_id, dispatch, role='host', name=host['name'], provider=host['provider'])
            checkpoint['collaboration_dispatch_written'] = True
            save()
    written = checkpoint.setdefault('collaboration_written', [])
    for member in selected:
        provider = member['provider']
        room = agent_group.load_room(base, owner, room_id)
        shared = agent_group.shared_memory(base, owner, room, memory)
        prompt = agent_group.prompt_for(room, member, memory=shared,
            extra='你本轮的分派任务与验收要求：\n' + member['assignment'] if member.get('assignment') else '')
        log(member['name'] + ' 正在执行分工…')
        reply = call_once(member['id'], member, prompt)
        if member['id'] not in written:
            agent_group.append_message(base, owner, room_id, reply, name=member['name'], provider=provider)
            written.append(member['id'])
            save()
    if host and room.get('summary', True):
        room = agent_group.load_room(base, owner, room_id)
        summary = call_once('mother_summary', host, agent_group.summary_prompt(room, host, task.description,
                            memory=shared, routing=selected))
        if 'mother_summary' not in written:
            agent_group.append_message(base, owner, room_id, summary, role='host', name=host['name'], provider=host['provider'])
            written.append('mother_summary')
            save()
    room = agent_group.load_room(base, owner, room_id)
    return {'output': json.dumps({'messages': room['messages'][-(len(selected) + bool(host)):]}, ensure_ascii=False), 'usage_recorded': True}
