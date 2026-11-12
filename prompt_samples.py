"""Fixed samples for comparing prompt versions (4stage; like a Langfuse dataset run, kept local).

Each sample is a short chat plus a mode, covering stickers / faces, a misunderstanding, unknown slang, sharing,
a follow-up after no reply and proactive chat. A temporary copy of one friend (its friend.json, empty history)
receives the sample chat, then the real prompt is assembled.

    .venv\\Scripts\\python.exe prompt_samples.py assemble OUT.json [QQ]   prompts only, no model call
    .venv\\Scripts\\python.exe prompt_samples.py run OUT.json [QQ]        also one Codex call per sample (uses quota)
    .venv\\Scripts\\python.exe prompt_samples.py compare OLD.json NEW.json

Run it before and after a prompt change and compare the two files."""
import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
MEMORY = [('养了一只叫团子的橘猫，经常拆家', 'other', ['cat', 'cat', '宠物', '拆家', '团子']),
          ('对芒果过敏', '禁忌', ['芒果', '水果', '过敏', '甜品', '蛋糕']),
          ('在杭州读大二，学计算机', '基本信息', ['大学', '杭州', '计算机', '写代码'])]
SAMPLES = [
    {'name': '原生表情', 'mode': 'reply', 'chat': [('friend', '今天考完试了'), ('me', '怎么样'),
                                                  ('friend', '[QQ原生表情：流泪，编号5]')]},
    {'name': '误会澄清', 'mode': 'reply', 'chat': [('friend', '滚木'), ('me', '啊？你要赶我走吗'),
                                                  ('friend', '不是，刚才是打错字了，我说的是棍母')]},
    {'name': '陌生说法', 'mode': 'reply', 'chat': [('friend', '你这穿搭也太city了吧')]},
    {'name': 'share', 'mode': 'reply', 'chat': [('friend', '我今天去海边看日落了，超级好看')]},
    {'name': '未回复续聊', 'mode': 'continuation', 'chat': [('friend', '我先去洗澡'), ('me', '好呀，洗完再聊')]},
    {'name': 'proactivedays', 'mode': 'proactive', 'chat': [('friend', '最近在追一部动漫'), ('me', '哪部呀'),
                                                     ('friend', '葬送的芙莉莲')]},
    # 2026-10-10 user: samples with long-term memory — a pinned  entry, and an other entry only recall can bring in
    {'name': '记忆-常驻禁忌', 'mode': 'reply', 'memory': MEMORY,
     'chat': [('friend', '这个芒果蛋糕看着好好吃')]},
    {'name': '记忆-自动预检索', 'mode': 'reply', 'memory': MEMORY,
     'chat': [('friend', '回家一看纸巾又被扯了一地，气死我了')]},
]


def _run_sample(sample, qq, call_model):
    import main
    import prompt_context
    from store import FriendStore
    with tempfile.TemporaryDirectory() as temp:
        folder = Path(temp) / qq
        folder.mkdir()
        shutil.copy2(ROOT / 'friends' / qq / 'friend.json', folder / 'friend.json')
        profile = json.loads((folder / 'friend.json').read_text('utf-8'))
        store = FriendStore(folder)
        prompts, now = [], datetime.now().astimezone()
        start = now - timedelta(minutes=len(sample['chat']) + 5)
        if sample.get('memory'):
            import memory_management
            with store.db:
                store.db.execute('BEGIN IMMEDIATE')
                for text, category, keywords in sample['memory']:
                    memory_management.insert(store, text, 'fact', keywords, [], False, category)
                memory_management.sync(store)
        for index, (direction, body) in enumerate(sample['chat']):
            at = (start + timedelta(minutes=index)).isoformat()
            if direction == 'friend':
                store.record_inbound(f's{index}', body, at)
            else:
                store.add_message(f's{index}', 'me', body, at)

        def model(prompt, *args, **kwargs):
            prompts.append(prompt)
            if call_model:
                return main.codex_text(prompt, folder, None, ephemeral=True, profile=profile, text_only=True,
                                       local_ok=False)
            fake = {'reply': {'messages': ['（未调用模型）']}, 'proactive': {'messages': ['（未调用模型）']},
                    'continuation': {'action': 'wait', 'assessment': 'uncertain', 'reason': '', 'messages': [],
                                     'next_check_at': (now + timedelta(minutes=10)).isoformat()}}[sample['mode']]
            return json.dumps(fake, ensure_ascii=False), None
        output, error = None, None
        try:
            with patch.object(main, 'handoff_if_due'), patch.object(main, 'refresh_summary'), \
                    patch.object(main, 'session_codex_text', lambda s, p, *a, **k: model(p)), \
                    patch.object(prompt_context, '_reply_runtime_context',  # friend-scoped store refuses a temp copy
                                 side_effect=lambda p, d, q, r, legacy, **k: (legacy, None)):
                if sample['mode'] == 'reply':
                    output = main.make_reply(store, profile, folder)
                elif sample['mode'] == 'proactive':
                    output = main.make_round(store, profile, folder, 2)
                else:
                    from chat_continuation import decide
                    store.set('chat_continuation', json.dumps({'state': 'pending', 'next_check_at': now.isoformat(),
                                                               'generation': store.get('inbound_generation', '0')}))
                    output = decide(store, profile, folder, now, lambda p, *a, **k: model(p))
        except Exception as exc:
            error = f'{type(exc).__name__}: {exc}'
        finally:
            store.close()
        return {'name': sample['name'], 'mode': sample['mode'], 'prompt': prompts[-1] if prompts else '',
                'chars': len(prompts[-1]) if prompts else 0, 'output': output, 'error': error}


def collect(out, qq, call_model):
    import prompt_context
    results = [_run_sample(sample, qq, call_model) for sample in SAMPLES]
    Path(out).write_text(json.dumps({'version': getattr(prompt_context, 'PROMPT_VERSION', '旧版'), 'friend': qq, 'model_called': call_model,
                                     'time': datetime.now().astimezone().isoformat(timespec='seconds'),
                                     'samples': results}, ensure_ascii=False, indent=1), 'utf-8')
    for item in results:
        print(f"{item['name']}  {item['chars']} chars  {item['error'] or json.dumps(item['output'], ensure_ascii=False)[:80]}")


def compare(old_path, new_path):
    old, new = (json.loads(Path(path).read_text('utf-8')) for path in (old_path, new_path))
    print(f"Old {old['version']} {old['time']}  →  New {new['version']} {new['time']}")
    before = {item['name']: item for item in old['samples']}
    for item in new['samples']:
        previous = before.get(item['name'], {})
        print(f"\n{item['name']} {previous.get('chars', '-')} chars → {item['chars']} chars")
        print('  旧输出：' + json.dumps(previous.get('output') or previous.get('error'), ensure_ascii=False)[:200])
        print('  新输出：' + json.dumps(item['output'] or item['error'], ensure_ascii=False)[:200])


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    command = sys.argv[1]
    if command == 'compare':
        compare(sys.argv[2], sys.argv[3])
    else:
        collect(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else '555000001', command == 'run')

