"""The default QQ style must stay natural, short, and free of assistant boilerplate."""
from qq_natural_style import NATURAL_QQ_STYLE, natural_style_block


def test_natural_style_contract():
    text = natural_style_block()
    assert text.startswith('[QQ_NATURAL_STYLE]')
    for phrase in ('2-3 条短气泡', '先接住', '客服腔', '作为 AI', 'emoji', '不冒充真人经历'):
        assert phrase in text
    assert 'QQ' in NATURAL_QQ_STYLE
    assert '2-3 条短气泡' in NATURAL_QQ_STYLE
