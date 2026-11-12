"""Real (non-mocked) voice-path check: TTS -> record_segment -> WAV -> local ASR.

Run from the repository root with the project virtualenv:
    .venv\\Scripts\\python.exe -X utf8 tools/voice_live_check.py --preset phone_near
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--preset', default='phone_near', choices=('dry', 'phone_near', 'room', 'telephone'))
    parser.add_argument('--tier', default='small', choices=('small', 'base', 'large'))
    parser.add_argument('--text', default='真实链路测试：会议在下午三点开始，请带上电脑。')
    parser.add_argument('--output', default=str(ROOT / 'build' / 'voice_live'))
    args = parser.parse_args()

    import voice_acoustics
    import voice_input
    import voice_tts

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    profile = {'voice_reply': {'mode': 'always', 'tier': args.tier, 'speaker': 0, 'speed': 1.0,
                               'atmosphere': {'preset': args.preset}}}
    started = time.perf_counter()
    part = voice_tts.record_segment(args.text, profile, root=ROOT, directory=output)
    elapsed = (time.perf_counter() - started) * 1000
    raw = base64.b64decode(part['data']['file'][9:])
    target = output / f'live_{args.preset}.wav'
    target.write_bytes(raw)
    with wave.open(io.BytesIO(raw)) as reader:
        frames, rate = reader.getnframes(), reader.getframerate()
    transcript, mood = voice_input.transcribe_local(target, tier='small', root=ROOT)
    print(json.dumps({
        'preset': args.preset,
        'tier': args.tier,
        'part_type': part['type'],
        'samples': int(frames),
        'sample_rate': int(rate),
        'duration_sec': round(frames / rate, 3),
        'record_segment_ms': round(elapsed, 1),
        'wav': str(target),
        'wav_bytes': len(raw),
        'transcript': transcript,
        'mood': mood,
        'describe': voice_acoustics.describe({'preset': args.preset}),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
