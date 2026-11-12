"""Loopback-only, bearer-authenticated HTTP adapter for Open QQ Dot's agent mailbox.

Allows an existing CLI, IDE extension, local SDK or custom agent to use the SAME
persistent messages, grants, presence and handoffs as the MCP adapter. No model
or editor brand is assumed. The gateway does NOT launch foreign CLIs or provide
unrestricted filesystem access.

The trusted host registers agents and provisions short-lived tokens out of band.
Do not put tokens in URLs, command-line arguments, logs or repository files.
Access from other machines is intentionally NOT supported by this transport.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent_mailbox_mcp import TOOLS, call
from agent_messaging import AgentMailbox, MessagingError, verify_agent_token

MAX_HTTP_BYTES = 64 * 1024
LOGGER = logging.getLogger(__name__)
TOOL_SCHEMAS = {tool['name']: tool['inputSchema'] for tool in TOOLS}


def validate_arguments(name: str, arguments: dict) -> None:
    """Validate advertised tool arguments at the untrusted HTTP boundary."""
    def check(schema: dict, value, path: str):
        kind = schema.get('type')
        if kind == 'object':
            if not isinstance(value, dict):
                raise ValueError(f'{path} must be an object')
            fields = schema.get('properties', {})
            if schema.get('additionalProperties') is False and set(value) - set(fields):
                raise ValueError(f'{path} contains unsupported arguments')
            if any(key not in value for key in schema.get('required', [])):
                raise ValueError(f'{path} is missing required arguments')
            for key, child in value.items():
                if key in fields:
                    check(fields[key], child, f'{path}.{key}')
        elif kind == 'array':
            if not isinstance(value, list) or len(value) < schema.get('minItems', 0) or len(value) > schema.get('maxItems', 100000):
                raise ValueError(f'{path} must be an array with allowed length')
            for item in value:
                check(schema.get('items', {}), item, path + '[]')
        elif kind == 'string':
            if not isinstance(value, str) or len(value) < schema.get('minLength', 0) or len(value) > schema.get('maxLength', 100000):
                raise ValueError(f'{path} must be a string of allowed length')
        elif kind == 'integer':
            if type(value) is not int:
                raise ValueError(f'{path} must be an integer')
        elif kind == 'number':
            if type(value) not in (int, float):
                raise ValueError(f'{path} must be a number')
        elif kind == 'boolean' and type(value) is not bool:
            raise ValueError(f'{path} must be a boolean')
        if kind in ('integer', 'number') and type(value) in (int, float):
            if value < schema.get('minimum', float('-inf')) or value > schema.get('maximum', float('inf')):
                raise ValueError(f'{path} must be within allowed numeric bounds')
        if 'enum' in schema and value not in schema['enum']:
            raise ValueError(f'{path} contains an unsupported choice')
    check(TOOL_SCHEMAS[name], arguments, 'arguments')


class InteropServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], mailbox: AgentMailbox):
        # IP-literal only; DNS names and remote interfaces are intentionally excluded.
        if address[0] != '127.0.0.1':
            raise ValueError('Agent interop HTTP gateway must bind to 127.0.0.1')
        super().__init__(address, InteropHandler)
        self.mailbox = mailbox
        self.long_poll_slots = threading.BoundedSemaphore(32)


class InteropHandler(BaseHTTPRequestHandler):
    server: InteropServer
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        self.connection.settimeout(40)

    def log_message(self, fmt, *args):
        # Avoid requests/headers in logs (especially credential-bearing headers).
        LOGGER.debug('Agent interop HTTP request %s', self.address_string())

    def _json(self, status: int, obj: dict):
        encoded = json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _host_ok(self) -> bool:
        host = self.headers.get('Host', '').lower()
        return host in (f'127.0.0.1:{self.server.server_port}', '127.0.0.1')

    def _auth(self) -> str | None:
        # No browser-origin requests. Bearer authorization also prevents cookie-based CSRF.
        if not self._host_ok() or self.headers.get('Origin') is not None:
            self._json(403, {'error': 'host or browser origin refused'})
            return None
        value = self.headers.get('Authorization', '')
        if not value.startswith('Bearer '):
            self._json(401, {'error': 'bearer token required'})
            return None
        try:
            agent_id = verify_agent_token(value[7:].strip(), require_expiring=True)
            if self.server.mailbox.get_agent(agent_id) is None:
                raise PermissionError('unknown agent')
        except (PermissionError, ValueError):
            self._json(401, {'error': 'invalid or expired bearer token'})
            return None
        return agent_id

    def do_GET(self):
        if self.path == '/health':
            # Only liveness, no model/provider/agent metadata.
            if not self._host_ok():
                self._json(403, {'error': 'host refused'})
            else:
                self._json(200, {'status': 'live'})
            return
        agent_id = self._auth()
        if agent_id is None:
            return
        if self.path == '/v1/tools':
            self._json(200, {'tools': TOOLS, 'transport': 'http', 'identity': agent_id})
        elif self.path == '/v1/me':
            record = self.server.mailbox.get_agent(agent_id)
            self._json(200, {'agent': record.public(), 'sessions': self.server.mailbox.active_sessions(agent_id)})
        else:
            self._json(404, {'error': 'unknown path'})

    def do_POST(self):
        agent_id = self._auth()
        if agent_id is None:
            return
        if self.path != '/v1/call':
            self._json(404, {'error': 'unknown path'})
            return
        if self.headers.get('Content-Type', '').split(';', 1)[0].lower() != 'application/json':
            self._json(415, {'error': 'application/json required'})
            return
        if self.headers.get('Transfer-Encoding'):
            self._json(400, {'error': 'chunked request bodies not supported'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_HTTP_BYTES:
            self.close_connection = True
            self._json(413, {'error': 'request size exceeds allowed range'})
            return
        try:
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict) or set(payload) - {'name', 'arguments'}:
                raise ValueError('invalid payload')
            name, args = payload.get('name'), payload.get('arguments', {})
            if not isinstance(name, str) or name not in {tool['name'] for tool in TOOLS} or not isinstance(args, dict):
                raise ValueError('unknown tool or invalid arguments')
            validate_arguments(name, args)
            if name == 'wait_for_inbox':
                if not self.server.long_poll_slots.acquire(blocking=False):
                    self._json(429, {'error': 'too many concurrent waits'})
                    return
                try:
                    result = call(self.server.mailbox, agent_id, name, args)
                finally:
                    self.server.long_poll_slots.release()
            else:
                result = call(self.server.mailbox, agent_id, name, args)
            self._json(200, {'result': result})
        except (MessagingError, ValueError, TypeError, KeyError) as exc:
            self._json(400, {'error': str(exc)[:300]})
        except PermissionError:
            self._json(403, {'error': 'not authorized'})
        except Exception:
            LOGGER.exception('Agent interop call failed')
            self._json(500, {'error': 'internal mailbox error'})


def main() -> None:
    parser = argparse.ArgumentParser(description='Local agent mailbox bridge; no remote listener')
    parser.add_argument('--port', type=int, default=0, help='127.0.0.1 TCP port; 0 selects a free port')
    parser.add_argument('--db', type=Path, help='Optional alternate mailbox database for development/testing')
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error('invalid port')
    server = InteropServer(('127.0.0.1', args.port), AgentMailbox(args.db))
    print(f'AGENT_INTEROP_READY=127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
