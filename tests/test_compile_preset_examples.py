import copy
import hashlib
import json
from pathlib import Path

import pytest

from tools.compile_preset_examples import compile_examples


@pytest.mark.parametrize('corruption', ['other_combination', 'private_identifier', 'stale_prompt'])
def test_invalid_native_dialogue_is_rejected_before_compilation(tmp_path, corruption):
    root = Path(__file__).resolve().parents[1]
    asset = json.loads((root / 'assets/quick_setup_examples.json').read_text('utf-8'))
    prompts = json.loads((root / 'assets/quick_setup_prompts.json').read_text('utf-8'))
    key = '温柔陪伴_普通朋友_女_女_关闭主动'
    examples = asset['presets'][key]
    messages = [message for item in examples for message in
                ({'speaker': 'A', 'text': item['user']}, {'speaker': 'B', 'text': item['target'][0]})]
    value = {'schema': 'native-preset-dialogue-v1', 'id': 'p0001', 'prepared_key': key,
             'selection': dict(zip(('persona', 'relation', 'friend_gender', 'assistant_gender', 'frequency'), key.split('_'))),
             'model': 'gpt-6-luna', 'effort': 'high', 'messages': copy.deepcopy(messages),
             'prompt_sha256': hashlib.sha256(prompts[key].encode()).hexdigest(),
             'generation_request_sha256': 'a' * 64}
    path = tmp_path / 'p0001.json'
    path.write_text(json.dumps(value), 'utf-8')
    bank, report = compile_examples(tmp_path, {key: prompts[key]})
    assert report['reply_examples'] == 12 and bank['presets'][key][1]['user'] == messages[2]['text']
    if corruption == 'other_combination':
        value['selection']['friend_gender'] = '男'
    elif corruption == 'private_identifier':
        value['messages'][0]['text'] = '我的电话是13800138000'
    else:
        value['prompt_sha256'] = '0' * 64
    path.write_text(json.dumps(value), 'utf-8')
    with pytest.raises(ValueError):
        compile_examples(tmp_path, {key: prompts[key]})
