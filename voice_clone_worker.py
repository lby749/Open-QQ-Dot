"""Voice-cloning worker: runs VoxCPM2 inside its own Python environment (see requirements-voice-clone.txt).

voice_tts.CloneRuntime starts it and speaks JSON lines: one request per stdin line, one reply per stdout line.
It imports only voxcpm and numpy so the app's own environment never needs VoxCPM's dependencies.

    request: {"id": 1, "text": "...", "reference_wav": "C:/.../ref.wav", "prompt_text": "...", "cfg": 2.0, "steps": 10}
    reply:   {"id": 1, "ok": true, "wav": "C:/.../out.wav", "sample_rate": 48000}
"""
import argparse
import json
import os
import sys
import tempfile
import traceback
import wave


def write_wav(path, samples, rate):
    import numpy as np
    pcm = (np.clip(np.asarray(samples, dtype=np.float32).reshape(-1), -1.0, 1.0) * 32767).astype('<i2')
    with wave.open(path, 'wb') as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(pcm.tobytes())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', required=True)
    parser.add_argument('--device', default='auto')
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    # The protocol owns the real stdout; library chatter goes to stderr.
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), 'w', encoding='utf-8', buffering=1)
    sys.stdout = sys.stderr

    def reply(value):
        protocol.write(json.dumps(value, ensure_ascii=False) + '\n')
        protocol.flush()

    try:
        from voxcpm import VoxCPM
        model = VoxCPM(voxcpm_model_path=args.model, enable_denoiser=False, optimize=False,
                       device=None if args.device == 'auto' else args.device)
        rate = int(model.tts_model.sample_rate)
    except Exception as exc:
        traceback.print_exc()
        reply({'ready': False, 'error': f'{type(exc).__name__}: {exc}'})
        return 1
    reply({'ready': True, 'sample_rate': rate, 'device': str(getattr(model.tts_model, 'device', args.device))})
    for line in sys.stdin:
        if not line.strip():
            continue
        request = {}
        try:
            request = json.loads(line)
            reference = request.get('reference_wav') or None
            prompt_text = (request.get('prompt_text') or '').strip() or None
            samples = model.generate(
                text=request['text'], reference_wav_path=reference,
                # The reference clip plus its exact transcript gives the closest match.
                prompt_wav_path=reference if reference and prompt_text else None,
                prompt_text=prompt_text if reference else None,
                cfg_value=float(request.get('cfg', 2.0)), inference_timesteps=int(request.get('steps', 10)))
            handle, path = tempfile.mkstemp(suffix='.wav', dir=args.out)
            os.close(handle)
            write_wav(path, samples, rate)
            reply({'id': request.get('id'), 'ok': True, 'wav': path, 'sample_rate': rate})
        except Exception as exc:
            traceback.print_exc()
            reply({'id': request.get('id'), 'ok': False, 'error': f'{type(exc).__name__}: {exc}'})
    return 0


if __name__ == '__main__':
    sys.exit(main())
