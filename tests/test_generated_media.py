"""Model-generated images/videos are limited to the friend's generated-media folder."""
from pathlib import Path

import pytest

from main import generated_media_path


def test_generated_image_and_video_resolve_inside_scope(tmp_path):
    root = tmp_path / 'media' / 'generated'
    root.mkdir(parents=True)
    from PIL import Image
    image = root / 'selfie.png'
    Image.new('RGB', (128, 128), (30, 100, 200)).save(image)
    video = root / 'clip.mp4'
    video.write_bytes(b'\x00\x00\x00\x18ftypisom' + b'\x00' * 2048)
    assert generated_media_path(tmp_path, 'LOCAL_IMAGE', 'media/generated/selfie.png') == image.resolve()
    assert generated_media_path(tmp_path, 'LOCAL_VIDEO', 'media/generated/clip.mp4') == video.resolve()


def test_generated_media_rejects_escape_and_wrong_type(tmp_path):
    root = tmp_path / 'media' / 'generated'
    root.mkdir(parents=True)
    (tmp_path / 'outside.png').write_bytes(b'x')
    (root / 'selfie.png').write_bytes(b'x')
    with pytest.raises(RuntimeError):
        generated_media_path(tmp_path, 'LOCAL_IMAGE', 'outside.png')
    with pytest.raises(RuntimeError):
        generated_media_path(tmp_path, 'LOCAL_VIDEO', 'media/generated/selfie.png')
    with pytest.raises(RuntimeError):
        generated_media_path(tmp_path, 'LOCAL_IMAGE', 'https://example.com/x.png')
    with pytest.raises(RuntimeError):
        generated_media_path(tmp_path, 'LOCAL_IMAGE', 'media/generated/missing.png')


def test_media_decision_yes_no_uncertain_probability(monkeypatch):
    from main import decide_media, normalize_media_decision, strip_media_markers
    assert normalize_media_decision(True) == 'yes'
    assert normalize_media_decision('不确定') == 'uncertain'
    off = {'media_reply': {'mode': 'off', 'ratio': 50}}
    assert decide_media(off, 'yes') is True
    assert decide_media(off, 'no') is False
    assert decide_media(off, 'uncertain') is False
    sometimes = {'media_reply': {'mode': 'sometimes', 'ratio': 50}}
    monkeypatch.setattr('main.random.random', lambda: 0.1)
    assert decide_media(sometimes, 'uncertain') is True
    monkeypatch.setattr('main.random.random', lambda: 0.9)
    assert decide_media(sometimes, 'uncertain') is False
    assert strip_media_markers(['看这个', '[[LOCAL_IMAGE:media/generated/x.png]]']) == ['看这个']


def test_per_bubble_media_decisions_and_priority(monkeypatch):
    from main import bubble_items, filter_bubble_media
    texts, meta = bubble_items([{'text': 'a', 'image': 'yes'}, {'text': 'b', 'image': 'uncertain'}])
    assert texts == ['a', 'b'] and meta[0]['image'] == 'yes'
    profile = {'media_reply': {'mode': 'sometimes', 'ratio': 100}}
    image = '[[LOCAL_IMAGE:media/generated/x.png]]'
    video = '[[LOCAL_VIDEO:media/generated/x.mp4]]'
    assert filter_bubble_media(image, {'image': 'yes'}, profile, sticker_yes=True) == image
    assert filter_bubble_media(image, {'image': 'uncertain'}, profile, sticker_yes=True) == ''
    assert filter_bubble_media(video, {'video': 'uncertain'}, profile) == ''
    assert filter_bubble_media(video, {'video': 'yes'}, profile) == video


def test_per_bubble_voice_decisions():
    from main import normalize_voice_decision
    assert normalize_voice_decision(True) == 'yes'
    assert normalize_voice_decision('不确定') == 'uncertain'
    assert normalize_voice_decision('no') == 'no'


def test_generated_image_must_be_readable(tmp_path):
    root = tmp_path / 'media' / 'generated'
    root.mkdir(parents=True)
    bad = root / 'bad.png'
    bad.write_bytes(b'not an image')
    with pytest.raises(RuntimeError, match='unreadable'):
        generated_media_path(tmp_path, 'LOCAL_IMAGE', 'media/generated/bad.png')
