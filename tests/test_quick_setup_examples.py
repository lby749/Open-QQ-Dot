import persona_template
import quick_setup_examples
from house_qt.quick_setup import build_prompts


def profile(style='温柔陪伴', relation='好朋友'):
    identity, wording, preset = build_prompts(style, relation, '不透露', '中性', 'chat', '中')
    return {'persona': identity, 'style': wording, 'prompt_presets': preset}


def test_all_prepared_combinations_have_only_the_selected_style_and_relation():
    from house_qt.quick_setup import _prepared_prompts
    bank = quick_setup_examples._bank()
    assert len(bank) == 1080
    for key in _prepared_prompts():
        examples = quick_setup_examples.examples({'prompt_presets': {'prepared_key': key}})
        assert examples is bank[key]
        assert len(examples) == 12
        assert all(item['target'] and all(isinstance(s, str) for s in item['target']) for item in examples)
        assert len({item['source_id'].split(':')[0] for item in examples}) == 1


def test_gender_frequency_and_relation_never_fall_back_to_another_combination():
    bank = quick_setup_examples._bank()
    base = '温柔陪伴_好朋友_不透露_中性_中'
    for key in ('温柔陪伴_好朋友_男_中性_中', '温柔陪伴_好朋友_不透露_女_中',
                '温柔陪伴_好朋友_不透露_中性_低', '温柔陪伴_普通朋友_不透露_中性_中'):
        assert bank[base] is not bank[key]
        assert bank[base][0]['source_id'] != bank[key][0]['source_id']
    assert quick_setup_examples.examples({'prompt_presets': {'prepared_key': base + '_不存在'}}) == []


def test_previous_user_turn_is_not_reindexed_as_the_identity_of_later_replies():
    items = quick_setup_examples._bank()['温柔陪伴_普通朋友_女_女_关闭主动']
    first_turn = items[0]['user']
    assert first_turn in items[1]['context'][0]
    assert first_turn not in quick_setup_examples.retrieval_text(items[1])
    assert items[1]['context'][-1] in quick_setup_examples.retrieval_text(items[1])


def test_actual_template_prompt_uses_synthetic_examples_bounded_and_not_for_tasks(tmp_path, monkeypatch):
    monkeypatch.setattr('retrieval_core.rank_texts', lambda *_args, **_kw: [(2, 1), (0, .9)])
    value = profile()
    block = persona_template.prompt_block(value, tmp_path, '歇一会儿', 'unknown')
    assert block.count('【STYLE_EXAMPLE】') == 2 and '公开合成示例' in block
    assert 'TA_REAL_CHATS' not in block
    assert persona_template.prompt_block(value, tmp_path, '执行工作', 'workbench') == ''
    assert persona_template.prompt_block(value, tmp_path, '主动消息', 'unknown', kind='opener') == ''
    assert persona_template.prompt_block(value, tmp_path, '关闭示例', 'unknown', max_samples=0) == ''
    monkeypatch.setattr('task_runtime.work_task', lambda: True)
    assert persona_template.prompt_block(value, tmp_path, '工作任务', 'unknown') == ''


def test_disabled_or_unselected_preset_never_injects_examples(tmp_path):
    assert quick_setup_examples.examples({}) == []
    value = profile()
    value['prompt_presets']['examples_enabled'] = False
    assert persona_template.prompt_block(value, tmp_path, '聊聊', 'unknown') == ''


def test_explicit_persona_library_takes_precedence_over_prepared_examples(tmp_path, monkeypatch):
    monkeypatch.setattr(persona_template, 'active', lambda _: {'id': 'test', 'summary': ''})
    monkeypatch.setattr(persona_template, '_samples', lambda *_args: [])
    monkeypatch.setattr(persona_template, 'signature', lambda *_args: ([], [], []))
    monkeypatch.setattr(quick_setup_examples, 'prompt_block', lambda *_args, **_kw: (_ for _ in ()).throw(AssertionError('wrong preset fallback')))
    block = persona_template.prompt_block(profile(), tmp_path, '已有独立人格库', 'unknown')
    assert 'SPEAK_AS_TA' in block and 'quick_setup_examples' not in block


def test_retrieval_unavailable_uses_at_most_two_public_references(tmp_path, monkeypatch):
    monkeypatch.setattr('retrieval_core.rank_texts', lambda *_args, **_kw: [])
    block = quick_setup_examples.prompt_block(profile('冷静理性', '网友'), tmp_path, '你好')
    assert block.count('【STYLE_EXAMPLE】') == 2
    assert quick_setup_examples.examples(profile('温柔陪伴')) is not quick_setup_examples.examples(profile('冷静理性'))


def test_only_the_public_style_bank_uses_ranking_without_an_absolute_relevance_gate(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr('retrieval_core.rank_texts', lambda *_a, **kw: seen.append(kw) or [(0, .001)])
    block = quick_setup_examples.prompt_block(profile(), tmp_path, '请换一种说法')
    assert seen[0]['floor'] == 0 and seen[0]['limit'] == 2
    assert block.count('【STYLE_EXAMPLE】') == 1
    assert '不是真实经历' in block
