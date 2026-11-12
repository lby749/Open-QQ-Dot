from delivery_claims import inspect


def test_voice_claim_without_actual_voice_is_sanitized():
    result = inspect('语音已经发给你了，听听看', actual_kinds=[])
    assert result.allowed is False
    assert 'voice' in result.claims
    assert '还没发出去' in result.sanitized
    assert '发给你了' not in result.sanitized


def test_claim_matches_actual_media():
    assert inspect('图片发你了', actual_kinds=['image']).allowed is True
    assert inspect('语音发你了', actual_kinds=['voice']).allowed is True
    assert inspect('语音发你了', actual_kinds=['image']).allowed is False


def test_generic_claim_requires_any_receipt():
    assert inspect('刚发给你了', actual_kinds=[]).allowed is False
    assert inspect('刚发给你了', recent=['file']).allowed is True


def test_ordinary_text_is_not_claimed():
    result = inspect('我发消息问你一下')
    assert result.allowed is True
    assert result.claims == ()


def test_recent_delivery_receipt_is_considered():
    import json, time
    import delivery_claims

    class DB:
        def execute(self, *args):
            return [{'value': json.dumps({'kind': 'voice', 'at': time.time()})}]

    class Store:
        db = DB()

    assert 'voice' in delivery_claims.recent_kinds(Store())
