"""Input validation shared by the project's local MCP servers."""
import json


def protocol_version(params):
    version = params.get('protocolVersion')
    return version if version in ('2024-11-05', '2025-03-26', '2025-06-18') else '2024-11-05'


def requests(stream):
    for line in stream:
        try:
            request = json.loads(line)
        except ValueError:
            _error(None, -32700, '请求不是有效 JSON')
            continue
        if (not isinstance(request, dict) or request.get('jsonrpc') != '2.0'
                or not isinstance(request.get('method'), str)
                or ('id' in request and not (request['id'] is None or
                    type(request['id']) in (str, int)))):
            _error(None, -32600, '请求格式无效')
            continue
        if 'id' not in request:
            continue
        if not isinstance(request.get('params', {}), dict):
            _error(request['id'], -32602, '参数须为对象')
            continue
        yield request


def _error(identifier, code, message):
    print(json.dumps({'jsonrpc': '2.0', 'id': identifier,
                      'error': {'code': code, 'message': message}}, ensure_ascii=False), flush=True)
