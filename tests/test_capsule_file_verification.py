import sys
from task_verifier import TaskVerifier


def test_file_stage_must_not_ignore_its_content_acceptance_command(tmp_path):
    target = tmp_path / 'result.txt'
    target.write_text('WRONG_CONTENT', 'utf-8')
    command = '"' + sys.executable + '" -c "from pathlib import Path; assert Path(\'result.txt\').read_text() == \'RIGHT_CONTENT\'"'
    info = {'target_files': ['result.txt'], 'test_command': command}
    result = {'modified_files': ['result.txt']}
    failed = TaskVerifier.verify('file', info, result, tmp_path)
    assert not failed.success and failed.verifier_type == 'code_automated_test'
    target.write_text('RIGHT_CONTENT', 'utf-8')
    passed = TaskVerifier.verify('file', info, result, tmp_path)
    assert passed.success and passed.verifier_type == 'file_automated_test'


def test_qq_test_command_retains_semicolons_inside_quoted_program():
    from task_spec import build_task_spec
    command = 'python -c "from pathlib import Path; assert Path(\'result.txt\').read_text() == \'正确。\'"'
    text = '创建文件。测试: ' + command + '；文件: result.txt'
    spec = build_task_spec(text, 'file')
    assert spec['test_command'] == command
    assert spec['target_files'] == ['result.txt']
