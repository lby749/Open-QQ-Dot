"""Minimal WSS voice stream for the vNext bridge.

Protocol (JSON control frames + binary PCM16/16kHz frames):

  -> {"type":"start", "sample_rate":16000}
  -> <binary PCM chunk>...
  -> {"type":"end"}
  <- {"type":"partial", "bytes":N}       (after start, optional)
  <- {"type":"final", "text":"...", "mood":"", "bytes":N}

This is intentionally an utterance stream, not a full duplex call: VAD, echo
cancellation and interruption are not implemented yet.  Audio is kept in memory
for one utterance and deleted after transcription.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import ssl
import tempfile
import wave
from pathlib import Path

from websockets.asyncio.server import serve

from tls_support import ensure_certificate

ROOT = Path(__file__).resolve().parent


def _wav(pcm: bytes, sample_rate: int = 16000) -> bytes:
    import io
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes(pcm)
    return buffer.getvalue()


async def _handler(ws):
    pcm = bytearray()
    sample_rate = 16000
    async for message in ws:
        if isinstance(message, bytes):
            if len(pcm) + len(message) > 8_000_000:
                await ws.send(json.dumps({'type': 'error', 'error': 'audio_too_large'}))
                pcm.clear()
                continue
            pcm.extend(message)
            continue
        try:
            control = json.loads(message)
        except ValueError:
            await ws.send(json.dumps({'type': 'error', 'error': 'invalid_json'}))
            continue
        kind = control.get('type')
        if kind == 'start':
            pcm.clear()
            sample_rate = int(control.get('sample_rate') or 16000)
            await ws.send(json.dumps({'type': 'ready', 'sample_rate': sample_rate}))
        elif kind == 'end':
            if len(pcm) < 1600:
                await ws.send(json.dumps({'type': 'error', 'error': 'audio_too_short'}))
                continue
            target = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
            target.write(_wav(bytes(pcm), sample_rate))
            target.close()
            try:
                import voice_input
                text, mood = await asyncio.to_thread(voice_input.transcribe_local, Path(target.name), 'small', ROOT)
            except Exception as exc:
                await ws.send(json.dumps({'type': 'error', 'error': f'ASR unavailable: {type(exc).__name__}'}))
                continue
            finally:
                Path(target.name).unlink(missing_ok=True)
            await ws.send(json.dumps({'type': 'final', 'text': text, 'mood': mood,
                                      'bytes': len(pcm)}, ensure_ascii=False))
            await ws.close()
        else:
            await ws.send(json.dumps({'type': 'error', 'error': 'unknown_type'}))


async def _serve(port: int, tls: bool) -> None:
    ssl_context = None
    if tls:
        cert, key = ensure_certificate(ROOT / 'data' / 'vnext_tls')
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        ssl_context.load_cert_chain(str(cert), str(key))
    async with serve(_handler, '127.0.0.1', port, ssl=ssl_context, max_size=8_000_000):
        scheme = 'wss' if tls else 'ws'
        print(json.dumps({'voice': f'{scheme}://127.0.0.1:{port}', 'tls': tls}), flush=True)
        await asyncio.Future()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8793)
    parser.add_argument('--tls', action='store_true')
    args = parser.parse_args()
    asyncio.run(_serve(args.port, args.tls))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
