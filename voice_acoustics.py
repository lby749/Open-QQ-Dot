"""Deterministic, dependency-free acoustic post-processing for QQ voice replies.

Only numpy is used.  The chain is deliberately conservative: a dry path returns the
input unchanged, and every parameter is clamped before it reaches the DSP.
"""
from __future__ import annotations

import os

import numpy as np

PRESETS = ('dry', 'phone_near', 'room', 'telephone')
DEFAULTS = {'preset': 'dry', 'noise_db': -52.0, 'reverb_wet': 0.08,
            'reverb_rt60': 0.20, 'breath': True, 'jitter': True}
CLAMPS = {
    'noise_db': (-60.0, -45.0),
    'reverb_wet': (0.0, 0.15),
    'reverb_rt60': (0.05, 0.35),
}
PRESET_VALUES = {
    'phone_near': {'noise_db': -52.0, 'reverb_wet': 0.08, 'reverb_rt60': 0.20,
                   'highpass': 90.0, 'lowpass': 8000.0, 'low_gain': 1.5,
                   'box_gain': -1.0, 'presence_gain': 1.5},
    'room': {'noise_db': -48.0, 'reverb_wet': 0.12, 'reverb_rt60': 0.24,
             'highpass': 70.0, 'lowpass': 10000.0, 'low_gain': 1.5,
             'box_gain': -1.0, 'presence_gain': 1.2},
    'telephone': {'noise_db': -45.0, 'reverb_wet': 0.0, 'reverb_rt60': 0.12,
                  'highpass': 300.0, 'lowpass': 3400.0, 'low_gain': 0.0,
                  'box_gain': -1.0, 'presence_gain': 1.0},
}


def _number(value, default, limits):
    try:
        text = float(value)
    except (TypeError, ValueError):
        return default
    if not np.isfinite(text):
        return default
    return max(limits[0], min(limits[1], text))


def normalize(settings=None):
    """Clamp the persisted atmosphere settings; never raises on bad input."""
    raw = settings if isinstance(settings, dict) else {}
    preset = str(raw.get('preset') or 'dry')
    if preset not in PRESETS:
        preset = 'dry'
    base = dict(DEFAULTS)
    base.update(PRESET_VALUES.get(preset, {}))
    value = {
        'preset': preset,
        'noise_db': _number(raw['noise_db'] if 'noise_db' in raw else base['noise_db'],
                            base['noise_db'], CLAMPS['noise_db']),
        'reverb_wet': _number(raw['reverb_wet'] if 'reverb_wet' in raw else base['reverb_wet'],
                              base['reverb_wet'], CLAMPS['reverb_wet']),
        'reverb_rt60': _number(raw['reverb_rt60'] if 'reverb_rt60' in raw else base['reverb_rt60'],
                               base['reverb_rt60'], CLAMPS['reverb_rt60']),
        'breath': bool(raw['breath']) if 'breath' in raw else bool(base['breath']),
        'jitter': bool(raw['jitter']) if 'jitter' in raw else bool(base['jitter']),
    }
    value.update({key: base[key] for key in ('highpass', 'lowpass', 'low_gain',
                                             'box_gain', 'presence_gain') if key in base})
    return value


def describe(settings=None):
    """Actual clamped parameters, including the preset's filter values."""
    value = normalize(settings)
    preset = value['preset']
    defaults = {'highpass': 90.0, 'lowpass': 8000.0, 'low_gain': 1.5,
                'box_gain': -1.0, 'presence_gain': 1.5}
    defaults.update(PRESET_VALUES.get(preset, {}))
    keys = ('highpass', 'lowpass', 'low_gain', 'box_gain', 'presence_gain')
    return {**value, **{key: defaults[key] for key in keys},
            'wet': value['reverb_wet'], 'rt60': value['reverb_rt60']}


def _biquad(kind, frequency, sample_rate, q=0.707, gain_db=0.0):
    frequency = max(10.0, min(float(frequency), sample_rate * 0.45))
    omega = 2.0 * np.pi * frequency / sample_rate
    cosine, sine = np.cos(omega), np.sin(omega)
    alpha = sine / (2.0 * q)
    if kind == 'highpass':
        b0, b1, b2 = (1 + cosine) / 2, -(1 + cosine), (1 + cosine) / 2
        a0, a1, a2 = 1 + alpha, -2 * cosine, 1 - alpha
    elif kind == 'lowpass':
        b0, b1, b2 = (1 - cosine) / 2, 1 - cosine, (1 - cosine) / 2
        a0, a1, a2 = 1 + alpha, -2 * cosine, 1 - alpha
    else:
        amplitude = 10 ** (gain_db / 40.0)
        b0, b1, b2 = 1 + alpha * amplitude, -2 * cosine, 1 - alpha * amplitude
        a0, a1, a2 = 1 + alpha / amplitude, -2 * cosine, 1 - alpha / amplitude
    return np.array([b0 / a0, b1 / a0, b2 / a0]), np.array([1.0, a1 / a0, a2 / a0])


def _fft_filter(samples, sample_rate, filters):
    """Apply static biquads in the frequency domain; output length is preserved."""
    if not filters or samples.size < 4:
        return samples
    spectrum = np.fft.rfft(samples)
    frequencies = np.fft.rfftfreq(samples.size, 1.0 / sample_rate)
    z = np.exp(-2j * np.pi * frequencies / sample_rate).astype(np.complex64)
    numerator = np.array([1.0])
    denominator = np.array([1.0])
    for b, a in filters:
        numerator = np.convolve(numerator, b)
        denominator = np.convolve(denominator, a)
    response = np.polyval(numerator[::-1], z) / np.polyval(denominator[::-1], z)
    return np.fft.irfft(spectrum * response, n=samples.size).astype(np.float32)


def _rng(seed):
    # Stable across processes; never use time or global random state.
    import hashlib
    digest = hashlib.sha256(str(seed or 'open-qq-dot').encode('utf-8')).digest()
    return np.random.default_rng(int.from_bytes(digest[:8], 'little'))


def _pink_noise(size, sample_rate, rng):
    target = size
    # A two-second deterministic floor is enough; tiling avoids an FFT over a
    # long utterance while keeping the repeat far below audibility at <= -45 dB.
    size = min(size, max(4, int(sample_rate * 2)))
    white = rng.standard_normal(size).astype(np.float32)
    if size < 4:
        return white
    spectrum = np.fft.rfft(white)
    frequencies = np.fft.rfftfreq(size, 1.0 / sample_rate)
    scale = np.ones_like(frequencies)
    scale[1:] = 1.0 / np.sqrt(frequencies[1:])
    # Fold the 4 kHz low-pass into this same spectrum: one FFT round trip.
    b, a = _biquad('lowpass', min(4000.0, sample_rate * 0.45), sample_rate)
    z = np.exp(-2j * np.pi * frequencies / sample_rate)
    response = (b[0] + b[1] * z + b[2] * z * z) / (a[0] + a[1] * z + a[2] * z * z)
    pink = np.fft.irfft(spectrum * scale * response, n=size).astype(np.float32)
    rms = float(np.sqrt(np.mean(pink ** 2))) if pink.size else 0.0
    pink = pink / rms if rms > 1e-9 else pink
    if target != size:
        pink = np.resize(pink, target)
    return pink


def _early_reflections(samples, sample_rate, wet, rt60, rng):
    if wet <= 0 or samples.size < 8:
        return samples
    # Six sparse taps all inside the first 80 ms, decaying with the room RT60.
    delays = [int(sample_rate * value) for value in (0.006, 0.014, 0.023, 0.034, 0.052, 0.071)]
    out = samples.copy()
    for index, delay in enumerate(delays):
        if delay <= 0 or delay >= samples.size:
            continue
        decay = 10 ** (-3.0 * delay / max(1, sample_rate) / max(0.05, rt60))
        gain = wet * decay * (0.85 ** index) / max(1, len(delays) / 2)
        out[delay:] += samples[:-delay] * gain
    return out


def _jitter(samples, sample_rate, rng):
    if samples.size < 4:
        return samples
    delta = float(rng.uniform(-0.002, 0.002))
    positions = np.arange(samples.size, dtype=np.float64) * (1.0 + delta)
    positions = np.clip(positions, 0.0, samples.size - 1.0)
    return np.interp(positions, np.arange(samples.size), samples).astype(np.float32)


def _breaths(samples, sample_rate, rng):
    """Add at most two short breath-like noises inside real silences; never change length."""
    threshold = 0.008
    minimum = int(0.18 * sample_rate)
    burst_size = max(1, int(0.15 * sample_rate))
    silent = np.abs(samples) < threshold
    # Vectorised silence runs: avoid a 240k-iteration Python loop per utterance.
    flags = np.concatenate(([0], silent.astype(np.int8), [0]))
    edges = np.flatnonzero(np.diff(flags))
    runs = [(int(start), int(end)) for start, end in zip(edges[::2], edges[1::2])
            if end - start >= minimum]
    out = samples.copy()
    for start, end in runs[:2]:
        size = min(burst_size, end - start)
        if size < 8:
            continue
        noise = rng.standard_normal(size).astype(np.float32)
        low = _biquad('lowpass', min(3000.0, sample_rate * 0.45), sample_rate)
        noise = _fft_filter(noise, sample_rate, [low])
        window = np.sin(np.linspace(0, np.pi, size)) ** 2
        rms = float(np.sqrt(np.mean(noise ** 2))) or 1.0
        burst = noise / rms * (10 ** (-32.0 / 20.0)) * window
        out[start:start + size] += burst
    return out


def _limit(samples):
    samples = np.nan_to_num(samples, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)
    rms = float(np.sqrt(np.mean(samples ** 2))) if samples.size else 0.0
    if rms > 1e-7:
        gain = min(4.0, max(0.1, 10 ** (-20.0 / 20.0) / rms))
        samples = samples * gain
    ceiling = 10 ** (-1.0 / 20.0)
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    if peak > ceiling:
        samples = samples * (ceiling / peak)
    return np.clip(samples, -ceiling, ceiling).astype(np.float32)


def apply(samples, sample_rate, settings=None, seed=''):
    """Return float32 mono audio with exactly the input length."""
    source = np.asarray(samples, dtype=np.float32).reshape(-1)
    if os.environ.get('QQDOT_VOICE_DRY') == '1':
        return source.copy()
    value = describe(settings)
    if value['preset'] == 'dry':
        return source.copy()
    if source.size == 0:
        return source.copy()
    rate = int(sample_rate)
    if rate <= 0:
        return source.copy()
    try:
        filters = [
            _biquad('highpass', value['highpass'], rate, 0.707),
            _biquad('peaking', 250.0, rate, 1.0, value['low_gain']),
            _biquad('peaking', 900.0, rate, 1.2, value['box_gain']),
            _biquad('peaking', 3200.0, rate, 1.0, value['presence_gain']),
            _biquad('lowpass', value['lowpass'], rate, 0.707),
        ]
        out = _fft_filter(source, rate, filters)
        rng = _rng(seed)
        if value['jitter']:
            out = _jitter(out, rate, rng)
        out = _early_reflections(out, rate, value['wet'], value['rt60'], rng)
        if value['noise_db'] < 0:
            noise = _pink_noise(out.size, rate, rng)
            out += noise * (10 ** (value['noise_db'] / 20.0))
        if value['breath']:
            out = _breaths(out, rate, rng)
        return _limit(out)
    except Exception:
        # Never let an effect prevent a voice reply.
        return source.copy()
