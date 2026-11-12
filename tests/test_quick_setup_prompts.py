from house_qt.quick_setup import build_prompts


def test_prepared_prompt_pack_is_used_for_matching_combo():
    identity, style, preset = build_prompts('温柔陪伴', '好朋友', '女', '女', 'chat', '高')
    assert preset['prepared_key'] == '温柔陪伴_好朋友_女_女_高'
    assert '【角色设定加载】' in style
    assert preset['style_version'] == 3
    assert identity
