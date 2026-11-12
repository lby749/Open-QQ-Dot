import time
from pathlib import Path

import voice_invites


def test_create_pending_and_accept(tmp_path):
    row = voice_invites.create(tmp_path, '12345')
    assert voice_invites.pending(tmp_path)[0]['id'] == row['id']
    answered = voice_invites.respond(tmp_path, row['id'], True)
    assert answered['status'] == 'accepted'
    assert voice_invites.pending(tmp_path) == []


def test_decline_and_unknown(tmp_path):
    row = voice_invites.create(tmp_path, '12345')
    assert voice_invites.respond(tmp_path, row['id'], False)['status'] == 'declined'
    try:
        voice_invites.respond(tmp_path, 'missing', True)
    except ValueError as exc:
        assert '不存在' in str(exc)
    else:
        raise AssertionError('missing invite accepted')
