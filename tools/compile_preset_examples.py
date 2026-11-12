"""Validate native synthetic dialogues and compile exact manually selected preset examples.

No model calls, generated topic labels, private chat imports or experience-store writes.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re


def compile_examples(folder, prompts):
    presets, provenance = {}, {}
    utterances, dialogues = Counter(), set()
    for path in sorted(Path(folder).glob('p*.json')):
        raw = path.read_bytes()
        value = json.loads(raw)
        key = value['prepared_key']
        selection = value['selection']
        selected = '_'.join(selection[name].replace('/', '或') for name in
                            ('persona', 'relation', 'friend_gender', 'assistant_gender', 'frequency'))
        expected_hash = hashlib.sha256(prompts.get(key, '').encode()).hexdigest()
        if (value.get('schema') != 'native-preset-dialogue-v1' or key not in prompts or key != selected
                or key in presets or value.get('prompt_sha256') != expected_hash
                or value.get('id') != path.stem or value.get('model') != 'gpt-6-luna'
                or value.get('effort') != 'high'
                or not re.fullmatch(r'[0-9a-f]{64}', value.get('generation_request_sha256', ''))):
            raise ValueError(f'{path.name}: 来源或手动组合不匹配')
        messages = value['messages']
        if len(messages) != 24:
            raise ValueError(f'{path.name}: 对话必须有24条消息')
        normalized = []
        for i, message in enumerate(messages):
            text = message.get('text', '')
            if (message.get('speaker') != ('A' if i % 2 == 0 else 'B') or not isinstance(text, str)
                    or not text.strip() or len(text) > 180
                    or re.search(r'https?://|\d{5,}|[A-Za-z]:[\\/]|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', text)):
                raise ValueError(f'{path.name}: 消息格式或隐私规则未通过')
            normalized.append(re.sub(r'\s+', '', text))
        digest = hashlib.sha256('\n'.join(normalized).encode()).hexdigest()
        if digest in dialogues:
            raise ValueError(f'{path.name}: 重复的完整对话')
        dialogues.add(digest)
        utterances.update(normalized)
        presets[key] = [{'source_id': f'{path.stem}:r{i // 2 + 1:02}',
                         'context': [('对方：' if n % 2 == 0 else '示例回复：') + messages[n]['text']
                                     for n in range(max(0, i - 4), i)],
                         'user': messages[i]['text'], 'target': [messages[i + 1]['text']]}
                        for i in range(0, 24, 2)]
        provenance[key] = {'id': path.stem, 'prompt_sha256': expected_hash,
                           'generation_request_sha256': value['generation_request_sha256'],
                           'source_sha256': hashlib.sha256(raw).hexdigest()}
    if presets.keys() != prompts.keys():
        raise ValueError('原生对话必须覆盖全部提示词组合，且不能多出其他组合')
    repeated = sum(1 for text, count in utterances.items() if len(text) >= 12 and count >= 5)
    if repeated:
        raise ValueError('存在高频重复长句，请先审查生成质量')
    return {'schema': 'quick-setup-examples-v2', 'synthetic': True,
            'generator': {'model': 'gpt-6-luna', 'effort': 'high', 'messages_per_dialogue': 24},
            'presets': presets, 'provenance': provenance}, {
                'combinations': len(presets), 'messages': len(presets) * 24,
                'reply_examples': len(presets) * 12, 'unique_utterances': len(utterances),
                'duplicate_dialogues': 0, 'repeated_long_utterances_5plus': repeated,
                'identifier_pattern_hits': 0, 'combination_bijection': True,
                'limitation': '来源、去重及规则检查通过不等于全部对话已完成人工语义审查。'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dialogues', type=Path, required=True)
    parser.add_argument('--prompts', type=Path, default=Path('assets/quick_setup_prompts.json'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    bank, report = compile_examples(args.dialogues, json.loads(args.prompts.read_text('utf-8')))
    data = json.dumps(bank, ensure_ascii=False, indent=1).encode('utf-8')
    args.output.write_bytes(data)
    report['asset_sha256'] = hashlib.sha256(data).hexdigest()
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
