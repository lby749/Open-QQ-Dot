"""What the agents are doing together, for the desktop "协作网络" page and the phone "智能体协作" page
(2026-10-26 user: "see directly which agents are active and what interactions they've had").

Read-only snapshot of the agent mailbox (agent_messaging.py) joined with the task database:
- agents: who is registered, which model is behind it, and whether it is working right now
  (its collaboration task is running, it has a live mailbox session, or mail is waiting for it);
- threads: each collaboration (one per orchestrated task, see agent_messaging.Collaboration) or
  conversation, with its messages in order: who handed what to whom, the result, and whether each
  recipient has received and acknowledged it.
Message bodies are untrusted agent output: only a short excerpt is returned and pages show it as plain text.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from ui_dashboard_data import _read

PROVIDERS = {'codex': 'Codex', 'claude': 'Claude Code', 'antigravity': 'Antigravity', 'api': '自定义 API',
             'dsh': 'DeepSeek', 'deepseek': 'DeepSeek', 'orchestrator': '编排主管', 'local': '本地模型',
             'mcp': 'MCP 客户端', 'owner': '主人'}
KINDS = {'handoff': '交接任务', 'response': '交回结果', 'finding': '未验证结果', 'request': '请求',
         'review': '请求审查', 'note': '备注'}
STATES = {'recorded': '已记录', 'pending': '待投递', 'delivered': '已送达', 'acked': '已处理', 'refused': '被拒收', 'expired': '已过期',
          'failed': '投递失败'}
RUNNING = ('RUNNING', 'VERIFYING')
TASK_STATES = {'COMPLETED': '已完成', 'FAILED': '失败', 'CANCELLED': '已取消', 'WAITING_APPROVAL': '待授权',
               'WAITING_REVIEW': '待验收', 'WAITING_RESOURCE': '等待资源', 'PAUSED': '已暂停', 'PENDING': '待执行',
               'BLOCKED': '受阻'}
# Node titles in the tree stay narrow (2026-10-26 user: four or five characters, then "still too wide"): one
# character for the step, then a three-letter name; the full names are in the tooltip and the opened message.
SHORT = {'codex': 'Cdx', 'claude': 'Cld', 'antigravity': 'Agy', 'api': 'API', 'dsh': 'DS', 'deepseek': 'DS',
         'orchestrator': '主管', 'supervisor': '主管', 'local': '本地', 'mcp': 'MCP', 'owner': '主人'}
# The state under each node, two characters (2026-10-26 user: "this whole thing is too wide").
TWO = {'进行中': '进行', '已完成': '完成', '已取消': '取消', '待授权': '待批', '待验收': '待验', '等待资源': '等资',
       '已暂停': '暂停', '待执行': '待办'}
KIND_TAGS = {'handoff': '派', 'response': '回', 'finding': '回', 'request': '问', 'review': '审', 'note': '注'}
RECENT_SECONDS = 120  # "just active": touched the mailbox in the last two minutes
EXCERPT = 240


def _label(agent_id: str, provider: str = '') -> str:
    name = agent_id.rsplit('/', 1)[-1]
    if name == 'supervisor':
        return '编排主管'
    if name in PROVIDERS or not provider:  # a collaboration worker is named after its backend
        return PROVIDERS.get(name, name)
    return f'{name}（{PROVIDERS.get(provider, provider)}）'  # a named mailbox agent keeps its own name


def _fit(text: str, width: float = 5) -> str:
    """`text` cut to `width` Chinese characters (a Latin letter counts half), with … when cut."""
    used, out = 0.0, ''
    for char in text:
        used += 1 if ord(char) > 0x2E7F else 0.5
        if used > width:
            return out.rstrip() + '…'
        out += char
    return out


def _short(agent_id: str) -> str:
    name = agent_id.rsplit('/', 1)[-1]
    return SHORT.get(name) or _fit(name, 2)


def _mark(receipts):
    """One receipt mark for a message: √ all handled, × any failed, … any waiting, • recorded/delivered."""
    states = {r['state'] for r in receipts}
    if states & {'refused', 'expired', 'failed'}:
        return '×'
    if states & {'pending'}:
        return '…'
    if states and states <= {'acked'}:
        return '√'
    return '•' if states else ''


def tree(title: str, status: str, items: list[dict], lead: str = '', lead_provider: str = '') -> dict:
    """A collaboration as a top-down tree (2026-10-26 user: "a vertically arranged tree with nodes").

    task -> lead (the supervisor, named after the model that planned) -> each handoff to a worker -> what came
    back. Replies hang under the message they answer (reply_to); other messages hang under their sender's
    node when it is the lead, else under the task. Every message node keeps its index in thread['messages']."""
    root = {'type': 'task', 'label': title, 'short': _fit(title, 4), 'detail': '', 'agent_id': '',
            'children': []}  # state: its brief
    lead_id = next((item['from'] for item in items if item['from'].endswith('/supervisor')), None)
    lead_node = None
    if lead_id:
        lead_node = {'type': 'agent', 'label': lead or '编排主管', 'short': '主管', 'detail': '',
                     'agent_id': lead_id, 'provider': lead_provider or 'orchestrator', 'children': []}
        root['children'].append(lead_node)
    nodes = {}
    for index, item in enumerate(items):
        workers = [r for r in item['to'] if not r['agent_id'].endswith('/supervisor')] or item['to']
        if item['kind'] in ('handoff', 'request', 'review'):
            agent_id = workers[0]['agent_id'] if workers else item['from']
            label = '、'.join(r['label'] for r in workers) or item['from_label']
        else:
            agent_id, label = item['from'], item['from_label']
        node = {'type': 'message', 'index': index, 'kind': item['kind'], 'kind_label': item['kind_label'],
                'label': label, 'tag': KIND_TAGS.get(item['kind'], '·'), 'short': _short(agent_id),
                'detail': item['subject'] or item['excerpt'][:60], 'agent_id': agent_id,
                'mark': _mark(item['to']), 'at': item['at'], 'children': []}
        synthesis = re.match(r'Synthesis by \w+: (\d+)/(\d+) subtasks verified', item['excerpt'])
        if item['kind'] == 'note' and synthesis:  # the orchestrator's closing note
            node.update(kind_label='汇总', tag='汇', detail=f'{synthesis[1]}/{synthesis[2]} 个子任务通过验证，已汇总成最终回复')
        if lead_node and agent_id == lead_id:
            node['provider'] = lead_node['provider']  # the lead's own steps carry its model's colour
        nodes[item['message_id']] = node
        parent = nodes.get(item['reply_to'])
        if parent is None:
            parent = lead_node if lead_node and item['from'] == lead_id else root
        parent['children'].append(node)
        node['brief'] = _brief(node, synthesis)
        if item['kind'] == 'note' and item['subject'].startswith('模型已返回 · '):
            node.update(kind='returned', kind_label='模型已返回', tag='回', brief='返回')
    root['brief'] = TWO.get(status, status[:2]) if status else '对话'
    if lead_node:
        lead_node['brief'] = '规划'
    for node in nodes.values():  # what came back changes how a handoff reads
        replies = {child['kind'] for child in node['children']}
        if node['kind'] == 'handoff':
            node['brief'] = ('交回' if replies & {'response', 'returned'} else '未过' if 'finding' in replies
                             else node['brief'])
        elif node['kind'] in ('request', 'review') and replies & {'response', 'finding'}:
            node['brief'] = '已复'
    return root


def _brief(node: dict, synthesis=None) -> str:
    """Two characters under a node saying where that step stands (2026-10-26 user)."""
    kind, mark = node['kind'], node['mark']
    if mark == '×':
        return '失败'
    if kind == 'handoff':
        return {'√': '处理', '…': '等待'}.get(mark, '已派')
    if kind in ('request', 'review'):
        return '待复' if mark != '√' else '处理'
    if kind == 'response':
        return '通过'
    if kind == 'finding':
        return '未过'
    if synthesis:  # verified/total, e.g. 2/2
        text = f'{synthesis[1]}/{synthesis[2]}'
        return text if len(text) <= 5 else '汇总'
    return '备注'


def _mail_snapshot(base: Path, owner: str | None = None, threads: int = 30, now: float | None = None) -> dict:
    """{'agents': [...], 'threads': [...], 'summary': {...}, 'warnings': [...]}; owner limits it to one friend."""
    base = Path(base)
    now = time.time() if now is None else now
    warnings = []

    def run(db, sql, params=()):
        rows, error = _read(base / db, sql, params)
        if error:
            warnings.append(f'{db}: {error}')
        return rows

    where, params = ('WHERE owner_id=?', (owner,)) if owner else ('', ())
    agents = run('agent_mail.sqlite3', f'''SELECT agent_id, owner_id, project_id, name, provider, capabilities,
        description, last_active_at FROM agents {where} ORDER BY last_active_at DESC LIMIT 200''', params)
    result = {'agents': [], 'threads': [], 'warnings': warnings,
              'summary': {'agents': 0, 'active': 0, 'threads': 0, 'messages_24h': 0, 'waiting': 0, 'failed': 0}}
    if agents is None:
        return result
    ids = [a['agent_id'] for a in agents]
    marks = ','.join('?' * len(ids)) or "''"
    sessions = {r['agent_id']: r['live'] for r in run('agent_mail.sqlite3', f'''SELECT agent_id, COUNT(*) AS live
        FROM agent_sessions WHERE expires_at>? AND agent_id IN ({marks}) GROUP BY agent_id''', (now, *ids)) or []}
    inbox = {}
    for r in run('agent_mail.sqlite3', f'''SELECT agent_id, state, COUNT(*) AS n FROM recipients
            WHERE agent_id IN ({marks}) GROUP BY agent_id, state''', ids) or []:
        inbox.setdefault(r['agent_id'], {})[r['state']] = r['n']
    sent = {r['from_agent']: r['n'] for r in run('agent_mail.sqlite3', f'''SELECT from_agent, COUNT(*) AS n
        FROM messages WHERE from_agent IN ({marks}) GROUP BY from_agent''', ids) or []}
    projects = sorted({a['project_id'] for a in agents})
    pmarks = ','.join('?' * len(projects)) or "''"
    tasks = {r['task_id']: r for r in run('tasks.sqlite3', f'''SELECT task_id, title, status, category, updated_at
        FROM tasks WHERE task_id IN ({pmarks})''', projects) or []}
    for a in agents:
        if a['project_id'].startswith('flow_') and a['name'] == 'supervisor':
            rows = run('agent_mail.sqlite3', '''SELECT substr(body,1,200) AS body FROM messages
                WHERE from_agent=? AND subject='流程执行状态' ORDER BY created_at DESC LIMIT 1''', (a['agent_id'],)) or []
            if rows:
                try:
                    state = json.loads(rows[0]['body'])['flow_status']
                except (ValueError, KeyError, TypeError):
                    continue
                if state in ('RUNNING', 'FINISHED', 'FAILED'):
                    tasks[a['project_id']] = {'title': '协作流程', 'status': {'RUNNING': 'RUNNING', 'FINISHED': 'COMPLETED', 'FAILED': 'FAILED'}[state]}

    for a in agents:
        task = tasks.get(a['project_id'])
        box = inbox.get(a['agent_id'], {})
        # In a supervised collaboration the orchestrator runs the workers itself; their mailbox copies are the
        # audit record and stay unread, so "waiting" would be misleading there. The task's state is the truth.
        waiting = 0 if task else box.get('pending', 0) + box.get('delivered', 0)
        if task and task['status'] in RUNNING:
            status, detail = 'working', '协作任务进行中'
        elif task:
            status, detail = 'idle', '协作已结束（任务' + TASK_STATES.get(task['status'], task['status']) + '）'
        elif sessions.get(a['agent_id']):
            status, detail = 'working', '有在线会话'
        elif now - float(a['last_active_at'] or 0) < RECENT_SECONDS:
            status, detail = 'working', '刚刚活动'
        elif waiting:
            status, detail = 'waiting', f'{waiting} 条消息待处理'
        else:
            status, detail = 'idle', '空闲'
        if task and task['status'] == 'RUNNING' and a['project_id'].startswith('flow_'):
            phase = run('agent_mail.sqlite3', '''SELECT body FROM messages WHERE from_agent=?
                AND subject='节点调用状态' ORDER BY created_at DESC LIMIT 1''', (a['agent_id'],)) or []
            status, detail = ('working', '模型调用中') if phase and phase[0]['body'] == '调用中' else ('idle', '等待分工或交回')
        try:
            capabilities = json.loads(a['capabilities'] or '[]')
        except ValueError:
            capabilities = []
        result['agents'].append({
            'agent_id': a['agent_id'], 'owner': a['owner_id'], 'project': a['project_id'], 'name': a['name'],
            'provider': a['provider'], 'label': _label(a['agent_id'], a['provider']),
            'task_title': task['title'] if task else '', 'task_status': task['status'] if task else '',
            'capabilities': capabilities[:8], 'status': status, 'detail': detail,
            'last_active': float(a['last_active_at'] or 0), 'sent': sent.get(a['agent_id'], 0),
            'received': sum(box.values()), 'waiting': waiting,
            'failed': sum(box.get(state, 0) for state in ('refused', 'expired', 'failed'))})

    order = {'working': 0, 'waiting': 1, 'idle': 2}
    result['agents'].sort(key=lambda item: (order[item['status']], -item['last_active']))
    known = {a['agent_id']: a for a in result['agents']}
    visibility = (f'(from_agent IN ({marks}) OR message_id IN '
                  f'(SELECT message_id FROM recipients WHERE agent_id IN ({marks})))')
    visible_params = (*ids, *ids)
    rows = run('agent_mail.sqlite3', f'''SELECT thread_id, MIN(created_at) AS started, MAX(created_at) AS last,
        COUNT(*) AS count FROM messages WHERE {visibility} GROUP BY thread_id
        ORDER BY last DESC LIMIT ?''', (*visible_params, int(threads))) or []
    leads, lead_models = {}, {}
    for thread in rows:
        messages = run('agent_mail.sqlite3', f'''SELECT message_id, reply_to, from_agent, kind, subject,
            substr(body, 1, ?) AS excerpt, length(body) AS size, artifacts, created_at
            FROM messages WHERE thread_id=? AND {visibility} ORDER BY created_at, rowid LIMIT 120''',
                       (EXCERPT + 1, thread['thread_id'], *visible_params)) or []
        receipts = {}
        for r in run('agent_mail.sqlite3', '''SELECT r.message_id, r.agent_id, r.state, r.reason FROM recipients r
                JOIN messages m ON m.message_id=r.message_id WHERE m.thread_id=?''', (thread['thread_id'],)) or []:
            receipts.setdefault(r['message_id'], []).append(
                {'agent_id': r['agent_id'], 'label': _label(r['agent_id'], (known.get(r['agent_id']) or {}).get('provider', '')),
                 'state': r['state'], 'state_label': STATES.get(r['state'], r['state']), 'reason': r['reason']})
        participants = []
        items = []
        for m in messages:
            to = receipts.get(m['message_id'], [])
            for agent_id in [m['from_agent'], *(r['agent_id'] for r in to)]:
                if agent_id not in participants:
                    participants.append(agent_id)
            excerpt = m['excerpt'] or ''
            try:
                artifacts = json.loads(m['artifacts'] or '[]')
            except ValueError:
                artifacts = []
            items.append({'message_id': m['message_id'], 'reply_to': m['reply_to'], 'from': m['from_agent'],
                          'from_label': _label(m['from_agent'], (known.get(m['from_agent']) or {}).get('provider', '')),
                          'to': to, 'kind': m['kind'], 'kind_label': KINDS.get(m['kind'], m['kind']),
                          'subject': m['subject'], 'excerpt': excerpt[:EXCERPT] + ('…' if m['size'] > EXCERPT else ''),
                          'artifacts': artifacts[:5], 'at': float(m['created_at'])})
        project = (known.get(messages[0]['from_agent']) or {}).get('project', '') if messages else ''
        task = tasks.get(project)
        if task:  # supervised collaboration: the orchestrator passes results in-process, the mailbox copy is a record
            for item in items:
                for receipt in item['to']:
                    if receipt['state'] == 'pending':
                        receipt.update(state='recorded', state_label=STATES['recorded'])
        lead = next((re.match(r'Synthesis by (\w+)', item['excerpt']) for item in reversed(items)
                     if item['kind'] == 'note' and item['excerpt'].startswith('Synthesis by ')), None)
        if lead:  # the supervisor is a role; name the model that planned and summarised
            name = '编排主管 · ' + PROVIDERS.get(lead.group(1), lead.group(1))
            for item in items:
                if item['from'].endswith('/supervisor'):
                    item['from_label'] = name
                for receipt in item['to']:
                    if receipt['agent_id'].endswith('/supervisor'):
                        receipt['label'] = name
            leads[project] = name
            lead_models[project] = lead.group(1)
        title = (task['title'] if task else '') or (items[0]['subject'] if items else '') or thread['thread_id']
        status = TASK_STATES.get(task['status'], '进行中') if task else ''
        result['threads'].append({
            'thread_id': thread['thread_id'], 'project': project, 'title': title,
            'tree': tree(title, status, items, leads.get(project, ''), lead.group(1) if lead else ''),
            'task_status': task['status'] if task else '', 'started': float(thread['started']),
            'last': float(thread['last']), 'count': thread['count'],
            'participants': [{'agent_id': p, 'label': leads[project] if p.endswith('/supervisor') and project in leads
                              else _label(p, (known.get(p) or {}).get('provider', ''))} for p in participants],
            'messages': items})

    for agent in result['agents']:
        if agent['name'] == 'supervisor' and agent['project'] in leads:
            agent['label'] = leads[agent['project']]
            agent['lead_provider'] = lead_models[agent['project']]
    summary = result['summary']
    summary['agents'] = len(result['agents'])
    summary['active'] = sum(a['status'] == 'working' for a in result['agents'])
    summary['threads'] = len(result['threads'])
    summary['messages_24h'] = sum(1 for t in result['threads'] for m in t['messages'] if now - m['at'] < 86400)
    summary['waiting'] = sum(a['waiting'] for a in result['agents'])
    summary['failed'] = sum(a['failed'] for a in result['agents'])
    return result


GROUP_STATES = {'RUNNING': '进行中', 'FINISHED': '已完成', 'FAILED': '失败', 'PARTIAL': '部分完成',
                'WAITING_DELIVERY': '待送达', 'INTERRUPTED': '已中断'}
DELIVERY_STATES = {'SENT': '已送达', 'PENDING': '待发送', 'SENDING': '发送中',
                   'UNCERTAIN': '送达未确认', 'REVOKED': '已撤销'}


def _group_snapshot(base, owner, limit):
    """Project real execution and transport receipts, without writing/migrating live databases."""
    path = Path(base) / 'qq_group_team.sqlite3'
    where, params = ('WHERE owner=?', (str(owner),)) if owner else ('', ())
    runs, error = _read(path, f'SELECT * FROM runs {where} ORDER BY created DESC LIMIT ?', (*params, limit))
    result = {'agents': [], 'threads': [], 'aliases': {}, 'covered_mail': [],
              'warnings': [f'群协作记录：{error}'] if error else []}
    if not runs:
        return result
    tables, _ = _read(path, "SELECT name FROM sqlite_master WHERE type='table' AND name='events'")
    for run in runs:
        import agent_group
        try:
            room = agent_group.load_room(base, run['owner'], run['room'])
        except (OSError, ValueError):
            room = {}
        members = {m['id']: m for m in room.get('members', [])}
        host = members.get(room.get('host_id'), {'id': 'supervisor', 'name': '主智能体', 'provider': ''})
        project = 'qq_group:' + run['id']
        def identity(member):
            return f"qq_group:{run['owner']}:{run['room']}/" + member['id']
        config = room.get('qq_group') or {}
        if config.get('self_id') and config.get('group_id'):
            mail_project = f"qq_group:{config['self_id']}:{config['group_id']}:{run['room']}"
            result['aliases'].update({f"{run['owner']}/{mail_project}/{m['id']}": identity(m) for m in members.values()})
        lead = identity(host)
        items, events = [], []
        def add(member, kind, subject, body, brief, *, parent='', at=None, receipt='', target=None):
            identifier = project + ':' + str(len(items))
            items.append({'message_id': identifier, 'reply_to': parent, 'from': identity(member),
                'from_label': member.get('name', member['id']), 'to': ([{'agent_id': identity(target),
                    'label': target.get('name', target['id']), 'state': 'recorded'}] if target else []), 'kind': kind,
                'kind_label': subject, 'subject': subject, 'excerpt': str(body)[:EXCERPT],
                'body': str(body)[:12000], 'brief': brief, 'provider': (target or member).get('provider', ''),
                'receipt': receipt, 'artifacts': [], 'at': float(run['created'] if at is None else at)})
            return identifier
        # Historical records retain their actual evidence. A plan is not a completed model call.
        if run.get('dispatch'):
            add(host, 'note', '群内分工', run['dispatch'],
                '送达' if run.get('dispatch_receipt') else '已记', receipt=run.get('dispatch_receipt', ''))
        if tables:
            events, read_error = _read(path, 'SELECT * FROM (SELECT * FROM events WHERE run=? ORDER BY id DESC LIMIT 120) ORDER BY id', (run['id'],))
            if read_error:
                result['warnings'].append('群执行事件：' + read_error)
        active, last_calls, handovers = {}, {}, {}
        for event in events or []:
            try:
                info = json.loads(event['data'])
            except (ValueError, TypeError):
                continue
            member = dict(members.get(event['member']) or {}, id=event['member'])
            member.update({k: info[k] for k in ('name', 'provider') if k in info})
            members[event['member']] = member
            if event['phase'] == 'dispatched':
                for child in info.get('assignments') or []:
                    members[child['id']] = child
                    route = {'group': '群内', 'private': '私聊主账号', 'mailbox': '智能体邮箱'}.get(child.get('report', 'group'), '自动')
                    handovers[child['id']] = add(member, 'handoff', '分派分支任务',
                        member.get('name', '上级') + ' → ' + child['name'] + '\n' +
                        f"模型：{child.get('model') or '默认'}；汇报：{route}\n" + child['assignment'], '已派',
                        parent=last_calls.get(event['member'], ''), at=event['at'], receipt=info.get('receipt', ''), target=child)
                continue
            if event['phase'] not in ('started', 'returned', 'failed'):
                continue
            efforts = {'low': '低', 'medium': '中', 'high': '高', 'xhigh': '超高', 'max': '最高',
                       'ultra': '极高', 'off': '关闭', 'none': '关闭'}
            effort = info.get('effort') or '默认'
            choice = f"模型：{info.get('model') or '好友默认'}；思考强度：{efforts.get(effort, effort)}"
            if event['phase'] == 'started':
                body = choice + '\n' + str(info.get('assignment') or info.get('purpose') or '执行')
                last_calls[event['member']] = add(member, 'request', '模型调用开始', body, '调用', at=event['at'],
                                                  parent=handovers.get(event['member'], ''))
                active[event['member']] = True
            else:
                ok = event['phase'] == 'returned'
                add(member, 'response' if ok else 'finding', '模型已返回' if ok else '模型调用失败',
                    choice + '\n' + str(info.get('output') if ok else info.get('error', '失败')),
                    '返回' if ok else '失败', parent=last_calls.get(event['member'], ''), at=event['at'])
                active[event['member']] = False
        deliveries, read_error = _read(path, 'SELECT * FROM outbox WHERE run=? ORDER BY rowid LIMIT 64', (run['id'],))
        if read_error:
            result['warnings'].append('群投递记录：' + read_error)
        waiting, failed = {}, {}
        for row in deliveries or []:
            if row.get('route') == 'mailbox' and row.get('receipt'):
                result['covered_mail'].append(row['receipt'])
            member = members.get(row['member'], {'id': row['member'], 'name': '群成员', 'provider': ''})
            sent = row['state'] == 'SENT' and bool(row.get('receipt'))
            label = DELIVERY_STATES.get(row['state'], '状态未知') if not (row['state'] == 'SENT' and not sent) else '送达未确认'
            route = {'group': '群内汇报', 'private': '私聊主账号', 'mailbox': '邮箱汇报'}.get(row.get('route', 'group'), '协作汇报')
            add(member, 'note', route + ' · ' + label, row['content'], '送达' if sent else '待送',
                parent=last_calls.get(member['id'], ''), receipt=row.get('receipt', ''))
            waiting[member['id']] = int(not sent)
            failed[member['id']] = int(row['state'] in ('UNCERTAIN', 'REVOKED'))
        returned_members = {row['member'] for row in deliveries or []}
        for item in items:
            if item['kind'] == 'handoff' and item['to']:
                child_id = item['to'][0]['agent_id'].rsplit('/', 1)[-1]
                item['brief'] = '交回' if child_id in returned_members else '待回'
        if run.get('summary'):
            add(host, 'note', '群内汇总', run['summary'],
                '送达' if run.get('summary_receipt') else '已记', receipt=run.get('summary_receipt', ''))
        status = GROUP_STATES.get(run['status'], '状态未知')
        # Always expose the persisted run, including planning/failure before any output existed.
        add(host, 'note', '执行状态 · ' + status, run.get('detail') or status,
            '进行' if run['status'] == 'RUNNING' else '失败' if run['status'] == 'FAILED' else status[:2])
        root = tree(room.get('title', 'QQ 群协作'), status, items)
        def annotate(node):
            if node['type'] == 'message':
                item = items[node['index']]
                label = item['to'][0]['label'] if item['kind'] == 'handoff' and item['to'] else item['from_label']
                node.update(brief=item['brief'], provider=item['provider'], short=_fit(label, 5))
            for child in node['children']:
                annotate(child)
        annotate(root)
        participants = list(dict.fromkeys(m['from'] for m in items))
        labels = {m['from']: m['from_label'] for m in items}
        last = max(m['at'] for m in items)
        for identifier in participants:
            member_id = identifier.rsplit('/', 1)[-1]
            working = run['status'] == 'RUNNING' and (active.get(member_id, False) or identifier == lead)
            member = members.get(member_id, host)
            result['agents'].append({'agent_id': identifier, 'owner': run['owner'], 'project': project,
                'name': member.get('name', member_id), 'provider': member.get('provider', ''), 'label': labels[identifier],
                'task_title': room.get('title', 'QQ 群协作'), 'task_status': run['status'], 'capabilities': [],
                'status': 'working' if working else 'waiting' if waiting.get(member_id) else 'idle',
                'detail': '群内执行中' if working else status, 'last_active': last,
                'sent': sum(m['from'] == identifier for m in items), 'received': 0,
                'waiting': waiting.get(member_id, 0), 'failed': failed.get(member_id, 0)})
        result['threads'].append({'thread_id': project, 'project': project,
            'title': (run.get('request') or room.get('title') or 'QQ 群协作')[:160], 'tree': root,
            'task_status': run['status'], 'status_label': status, 'source': 'qq_group',
            'started': run['created'], 'last': last, 'count': len(items),
            'participants': [{'agent_id': p, 'label': labels[p]} for p in participants], 'messages': items})
    return result


def snapshot(base: Path, owner: str | None = None, threads: int = 30, now: float | None = None) -> dict:
    """Merge execution stores. The 24-hour window affects only the activity counter, never visibility."""
    now = time.time() if now is None else now
    limit = max(1, min(100, int(threads)))
    result = _mail_snapshot(base, owner, limit, now)
    groups = _group_snapshot(base, owner, limit)
    result['warnings'].extend(groups['warnings'])
    # The QQ outbox already projects these exact mailbox receipts; keep independent mailbox work visible.
    covered = set(groups['covered_mail'])
    result['threads'] = [t for t in result['threads'] if not t['messages'] or
                         not all(m['message_id'] in covered for m in t['messages'])]
    aliases = groups['aliases']
    for thread in result['threads']:
        for participant in thread['participants']:
            participant['agent_id'] = aliases.get(participant['agent_id'], participant['agent_id'])
    result['threads'] = sorted(result['threads'] + groups['threads'], key=lambda t: t['last'], reverse=True)[:limit]
    visible = {p['agent_id'] for t in result['threads'] for p in t['participants']}
    agents = {}
    for agent in groups['agents'] + result['agents']:
        agent['agent_id'] = aliases.get(agent['agent_id'], agent['agent_id'])
        if (agent['agent_id'] in visible or not agent['project'].startswith('qq_group:')) and agent['agent_id'] not in agents:
            agents[agent['agent_id']] = agent
    result['agents'] = list(agents.values())
    result['summary'] = {'agents': len(result['agents']), 'active': sum(a['status'] == 'working' for a in result['agents']),
        'threads': len(result['threads']), 'messages_24h': sum(0 <= now-m['at'] < 86400 for t in result['threads'] for m in t['messages']),
        'waiting': sum(a['waiting'] for a in result['agents']), 'failed': sum(a['failed'] for a in result['agents'])}
    return result
