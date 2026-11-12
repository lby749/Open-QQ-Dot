"""The large-file path uses several HTTP Range connections and merges them correctly."""
import http.server
import threading
from pathlib import Path

import model_downloader


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    payload = b'0123456789abcdef' * 4096

    def log_message(self, *args):
        pass

    def do_HEAD(self):
        self.send_response(200)
        self.send_header('Content-Length', str(len(self.payload)))
        self.send_header('Accept-Ranges', 'bytes')
        self.end_headers()

    def do_GET(self):
        raw = self.headers.get('Range', '')
        if not raw.startswith('bytes='):
            body = self.payload
            self.send_response(200)
        else:
            start_s, end_s = raw[6:].split('-', 1)
            start, end = int(start_s), int(end_s)
            body = self.payload[start:end + 1]
            self.send_response(206)
            self.send_header('Content-Range', f'bytes {start}-{end}/{len(self.payload)}')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Accept-Ranges', 'bytes')
        self.end_headers()
        self.wfile.write(body)


def test_range_download_merges_parallel_pieces(tmp_path, monkeypatch):
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _RangeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(model_downloader, 'RANGE_PARALLEL_THRESHOLD', 1024)
    try:
        url = f'http://127.0.0.1:{server.server_port}/model.bin'
        target = tmp_path / 'model.bin'
        progress = []
        model_downloader.download_file(url, target, progress_callback=lambda cur, total: progress.append((cur, total)))
        assert target.read_bytes() == _RangeHandler.payload
        assert progress and progress[-1][0] == len(_RangeHandler.payload)
        assert not list(tmp_path.glob('*.piece*'))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
