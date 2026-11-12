import io
import os
import wave
import base64

import numpy as np
import pytest

import voice_acoustics
import voice_tts


def tone(freq=440.0, seconds=0.5, rate=24000, amp=0.2):
    t = np.arange(int(rate * seconds), dtype=np.float32) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def band_amplitude(samples, rate, freq):
    spectrum = np.abs(np.fft.rfft(samples))
    index = int(round(freq * len(samples) / rate))
    return float(spectrum[max(0, index - 2):index + 3].max()) if spectrum.size else 0.0


def test_dry_is_a_true_bypass():
    samples = tone()
    assert np.array_equal(voice_acoustics.apply(samples, 24000, {'preset': 'dry'}), samples)
    assert np.array_equal(voice_acoustics.apply(samples, 24000, None), samples)


def test_every_preset_keeps_exact_length_and_peak_limit():
    samples = tone(seconds=1.0)
    ceiling = 10 ** (-1.0 / 20.0)
    for preset in voice_acoustics.PRESETS:
        result = voice_acoustics.apply(samples, 24000, {'preset': preset})
        assert result.dtype == np.float32
        assert len(result) == len(samples)
        assert np.max(np.abs(result)) <= ceiling + 1e-5
        assert np.all(np.isfinite(result))


def test_phone_near_attenuates_low_and_high_frequencies():
    # Measure the band-limiting stage itself: the final RMS normalization would
    # otherwise rescale both tones and hide the filter response.
    import voice_acoustics as va
    value = va.describe({'preset': 'phone_near'})
    filters = [va._biquad('highpass', value['highpass'], 24000, 0.707),
               va._biquad('lowpass', value['lowpass'], 24000, 0.707)]
    low_input = tone(60)
    high_input = tone(12000)
    low = va._fft_filter(low_input, 24000, filters)
    high = va._fft_filter(high_input, 24000, filters)
    assert band_amplitude(low, 24000, 60) < band_amplitude(low_input, 24000, 60) * 0.5
    assert band_amplitude(high, 24000, 12000) < band_amplitude(high_input, 24000, 12000) * 0.5


def test_deterministic_without_time_seed():
    samples = tone(seconds=0.5)
    one = voice_acoustics.apply(samples, 24000, {'preset': 'room'}, seed='friend-1')
    two = voice_acoustics.apply(samples, 24000, {'preset': 'room'}, seed='friend-1')
    other = voice_acoustics.apply(samples, 24000, {'preset': 'room'}, seed='friend-2')
    assert np.array_equal(one, two)
    assert not np.array_equal(one, other)


def test_clamps_are_applied():
    value = voice_acoustics.describe({'preset': 'phone_near', 'reverb_wet': 0.9, 'noise_db': -30})
    assert value['reverb_wet'] == 0.15
    assert value['noise_db'] == -45.0
    assert value['reverb_rt60'] <= 0.35
    assert voice_acoustics.describe({'preset': 'dry'})['preset'] == 'dry'


def test_silence_stays_finite_and_does_not_explode():
    silence = np.zeros(24000, dtype=np.float32)
    for preset in voice_acoustics.PRESETS:
        result = voice_acoustics.apply(silence, 24000, {'preset': preset})
        assert len(result) == len(silence)
        assert np.all(np.isfinite(result))
        assert np.max(np.abs(result)) <= 1.0


def test_environment_forces_dry(monkeypatch):
    monkeypatch.setenv('QQDOT_VOICE_DRY', '1')
    samples = tone()
    result = voice_acoustics.apply(samples, 24000, {'preset': 'room'}, seed='x')
    assert np.array_equal(result, samples)


def test_voice_settings_include_dry_atmosphere_by_default():
    value = voice_tts.settings({})
    assert value['atmosphere']['preset'] == 'dry'
    value = voice_tts.settings({'voice_reply': {'atmosphere': {'preset': 'telephone', 'noise_db': -30}}})
    assert value['atmosphere']['preset'] == 'telephone'
    assert value['atmosphere']['noise_db'] == -45.0


def test_record_segment_wav_round_trip_with_effect(monkeypatch):
    samples = tone(seconds=0.3)
    monkeypatch.setattr(voice_tts, 'synthesize', lambda *args, **kwargs: (samples, 24000))
    part = voice_tts.record_segment('测试', {'voice_reply': {
        'mode': 'always', 'tier': 'small', 'atmosphere': {'preset': 'phone_near'}}},
        directory=None)
    raw = base64.b64decode(part['data']['file'][9:])
    with wave.open(io.BytesIO(raw)) as reader:
        assert reader.getnframes() == len(samples)
        assert reader.getframerate() == voice_tts.QQ_SAMPLE_RATE
