"""Browser-model tool loop, without launching a browser."""
import json
import web_agent


def test_parse_tool_and_final():
    kind, name, args = web_agent.parse_action('<tool_call>{"tool":"desk_read_file","args":{"path":"a.txt"}}</tool_call>')
    assert (kind, name, args) == ('tool', 'desk_read_file', {'path': 'a.txt'})
    kind, text, _ = web_agent.parse_action('TASK_COMPLETE\n完成')
    assert kind == 'final' and text == '完成'
    kind, _, _ = web_agent.parse_action('just talking')
    assert kind is None


def test_run_task_loops_tools_then_final(monkeypatch, tmp_path):
    tool = {'name': 'desk_read_file', 'description': 'read', 'inputSchema': {'type': 'object'}}
    monkeypatch.setattr(web_agent.workspace_tools, 'tools', lambda directory: [tool])
    monkeypatch.setattr(web_agent.workspace_tools, 'call',
                        lambda directory, name, args: {'path': args['path'], 'text': 'hello'})
    replies = iter([
        '<tool_call>{"tool":"desk_read_file","args":{"path":"a.txt"}}</tool_call>',
        'TASK_COMPLETE\n读完了',
    ])
    monkeypatch.setattr(web_agent.web_chat, 'ask', lambda *args, **kwargs: next(replies))
    result = web_agent.run_task(str(tmp_path), '读 a.txt', 'deepseek')
    assert result['ok'] is True and result['answer'] == '读完了' and result['steps'] == 2


def test_run_task_rejects_without_tools(monkeypatch, tmp_path):
    monkeypatch.setattr(web_agent.workspace_tools, 'tools', lambda directory: [])
    try:
        web_agent.run_task(str(tmp_path), '读文件', 'deepseek')
    except RuntimeError as exc:
        assert '权限' in str(exc)
    else:
        raise AssertionError('expected RuntimeError')
