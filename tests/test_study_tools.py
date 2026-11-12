import json
import threading
from unittest.mock import patch
import zipfile

import pytest

import study_tools
from topic_library import available_tools, call_tool


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv('QQ_BRIDGE_HOME', str(tmp_path))
    monkeypatch.delenv('QQ_TASK_WORKSPACE', raising=False)
    friend, work = tmp_path / 'friends' / '10001', tmp_path / 'project'
    friend.mkdir(parents=True)
    work.mkdir()
    profile = {'qq_id': '10001', 'computer_tools': {'enabled': True, 'commands': False, 'project_path': str(work)}}
    (friend / 'friend.json').write_text(json.dumps(profile), 'utf-8')
    (work / 'lesson.md').write_text('光合作用将光能转化为化学能。叶绿体是主要场所。\nPlants store chemical energy.', 'utf-8')
    return friend, work, profile


def invoke(project, operation, **args):
    return call_tool(project[0], 'study_read' if operation in ('list', 'search', 'read', 'papers', 'review', 'progress') else 'study_write',
                     {'operation': operation, **args})


def reference(project):
    invoke(project, 'import', path='lesson.md')
    return invoke(project, 'search', query='光合作用')['items'][0]['id']


def quiz(project, kind='exact'):
    ref = reference(project)
    return invoke(project, 'quiz', title='学习练习', questions=[
        {'question': '光合作用主要发生在哪里？', 'answer': '叶绿体', 'kind': kind, 'references': [ref]}])['id']


def test_read_empty_library_does_not_create_database(project):
    assert invoke(project, 'list')['items'] == []
    assert not (project[0] / 'learning').exists()


def test_common_tools_available_to_api_and_mcp(project, monkeypatch):
    import api_backend
    import task_runtime
    from topic_library import model_tools, tool_annotations
    study = {'study_read', 'study_write'}
    assert study <= {tool['name'] for tool in available_tools(project[0])}  # callable from the desktop/phone pages
    # About 1.9k characters of definitions: a QQ chat reply or an unrelated task is not offered them.
    assert not study & {tool['name'] for tool in api_backend._tools(project[0])}
    with patch.object(task_runtime, 'study_task', return_value=True):
        assert study <= {tool['name'] for tool in api_backend._tools(project[0])}
    monkeypatch.setenv('QQ_STUDY_TASK', '1')  # a study task's own MCP tool server
    assert study <= {tool['name'] for tool in model_tools(project[0])}
    assert tool_annotations('study_read')['readOnlyHint']
    assert not tool_annotations('study_write')['readOnlyHint']


def test_study_task_is_recognised_from_its_template():
    from types import SimpleNamespace
    import task_runtime
    for template, expected in (('study_material', True), ('study_path', True), ('docs', False), (None, False)):
        task = SimpleNamespace(checkpoint={'template': template} if template else {})
        execution = SimpleNamespace(task_id='t', manager=SimpleNamespace(get_task=lambda _id, task=task: task))
        token = task_runtime.current_execution.set(execution)
        try:
            assert task_runtime.study_task() is expected
        finally:
            task_runtime.current_execution.reset(token)


def test_permissions_and_read_only_visibility(project):
    friend, _, profile = project
    profile['computer_tools']['read_only'] = True
    (friend / 'friend.json').write_text(json.dumps(profile), 'utf-8')
    assert [tool['name'] for tool in study_tools.tools(friend)] == ['study_read']
    with pytest.raises(ValueError):
        invoke(project, 'import', path='lesson.md')
    profile['computer_tools']['enabled'] = False
    (friend / 'friend.json').write_text(json.dumps(profile), 'utf-8')
    assert study_tools.tools(friend) == []


def test_import_search_citations_and_duplicate_import(project):
    first = invoke(project, 'import', path='lesson.md')
    again = invoke(project, 'import', path='lesson.md')
    assert first['id'] == again['id']
    assert len(invoke(project, 'list')['materials']) == 1
    hit = invoke(project, 'search', query='光合作用')['items'][0]
    assert hit['path'] == 'lesson.md' and hit['digest'] == first['digest']
    assert '叶绿体' in hit['text'] and hit['locator']
    assert invoke(project, 'read', id=hit['id'])['untrusted_data'] is True
    assert invoke(project, 'search', query='zzzznotpresent')['items'] == []


def test_two_agent_profiles_share_the_same_project_resources(project):
    friend, _, profile = project
    ref = reference(project)
    for provider in ('codex', 'claude', 'antigravity'):
        profile['model_api'] = {'provider': provider}
        (friend / 'friend.json').write_text(json.dumps(profile), 'utf-8')
        assert invoke(project, 'read', id=ref)['id'] == ref


def test_friend_and_project_isolation(project, tmp_path, monkeypatch):
    ref = reference(project)
    other = tmp_path / 'friends' / '10002'
    other.mkdir()
    (other / 'friend.json').write_text(json.dumps(project[2]), 'utf-8')
    assert call_tool(other, 'study_read', {'operation': 'search', 'query': '光合作用'})['items'] == []
    with pytest.raises(ValueError):
        call_tool(other, 'study_read', {'operation': 'read', 'id': ref})
    second = tmp_path / 'second'
    second.mkdir()
    monkeypatch.setenv('QQ_TASK_WORKSPACE', str(second))
    assert invoke(project, 'search', query='光合作用')['items'] == []


def test_cannot_import_another_folder_even_with_full_computer_grant(project, tmp_path):
    friend, _, profile = project
    profile['computer_tools']['full_computer_access'] = True
    (friend / 'friend.json').write_text(json.dumps(profile), 'utf-8')
    outside = tmp_path / 'outside.txt'
    outside.write_text('private', 'utf-8')
    with pytest.raises(ValueError):
        invoke(project, 'import', path=str(outside))


def test_symlinked_library_is_not_read_or_written(project, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    try:
        (project[0] / 'learning').symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('Creating symlinks is not allowed on this host')
    with pytest.raises(ValueError):
        invoke(project, 'import', path='lesson.md')
    assert not list(outside.iterdir())


def test_notes_plans_and_repeated_saves_do_not_start_another_agent(project):
    ref = reference(project)
    saved = invoke(project, 'plan', title='学习路径', content='研读、练习、复习', references=[ref])
    again = invoke(project, 'plan', title='学习路径', content='研读、练习、复习', references=[ref])
    assert saved['id'] == again['id'] and saved['execution_started'] is False
    assert len(invoke(project, 'list')['records']) == 1
    assert invoke(project, 'read', id=saved['id'])['payload']['content'] == '研读、练习、复习'


def test_shared_paper_retrieval_does_not_invent_results_on_failure(project):
    from local_retrieval_engine import AcademicResearchPipeline
    with patch.object(AcademicResearchPipeline, 'search_arxiv_papers', return_value=[]) as fetch:
        result = invoke(project, 'papers', query='chloroplast energy')
    fetch.assert_called_once_with('chloroplast energy', max_results=5)
    assert result['items'] == [] and not result['independently_verified']
    assert not (project[0] / 'learning').exists()


def test_quiz_requires_real_citations(project):
    with pytest.raises(ValueError):
        invoke(project, 'quiz', title='错误引用', questions=[
            {'question': 'q', 'answer': 'a', 'kind': 'exact', 'references': ['nonexistent']}])
    with pytest.raises(ValueError):
        invoke(project, 'quiz', title='无引用', questions=[
            {'question': 'q', 'answer': 'a', 'kind': 'exact', 'references': []}])
    assert not invoke(project, 'list')['records']


def test_quiz_answers_hidden_and_duplicate_submission_not_double_counted(project):
    identifier = quiz(project)
    assert 'answer' not in invoke(project, 'read', id=identifier)['payload']['questions'][0]
    assert invoke(project, 'review', id=identifier)['items'] == []
    for _ in range(2):
        result = invoke(project, 'answer', id=identifier, question_index=0, answer='叶绿体')
        assert result['score'] == 100 and result['independently_verified'] is False
    assert len(invoke(project, 'progress')['items']) == 1
    assert invoke(project, 'review', id=identifier)['items'][0]['reference_question']['answer'] == '叶绿体'
    with pytest.raises(ValueError):
        invoke(project, 'feedback', id=identifier, question_index=0, score=75, feedback='不能覆盖匹配分')


def test_subjective_feedback_is_not_an_objective_grade(project):
    identifier = quiz(project, 'text')
    result = invoke(project, 'answer', id=identifier, question_index=0, answer='叶片中的叶绿体')
    assert result['score'] is None and result['status'] == 'awaiting_agent_review'
    result = invoke(project, 'feedback', id=identifier, question_index=0, score=90, feedback='核心概念正确')
    assert result['status'] == 'agent_reviewed' and not result['independently_verified']
    assert invoke(project, 'progress')['items'][0]['feedback'] == '核心概念正确'


def test_reimport_invalidates_old_evidence(project):
    identifier = quiz(project)
    (project[1] / 'lesson.md').write_text('新的资料版本。', 'utf-8')
    invoke(project, 'import', path='lesson.md')
    assert invoke(project, 'read', id=identifier)['stale_references'] is True
    with pytest.raises(ValueError):
        invoke(project, 'answer', id=identifier, question_index=0, answer='叶绿体')


def test_html_and_docx_extract_real_text(project):
    (project[1] / 'page.html').write_text('<h1>chloroplast</h1><script>hidden_payload</script>', 'utf-8')
    invoke(project, 'import', path='page.html')
    assert invoke(project, 'search', query='chloroplast')['items']
    assert not invoke(project, 'search', query='hidden_payload')['items']
    with zipfile.ZipFile(project[1] / 'lesson.docx', 'w') as doc:
        doc.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                     '<w:body><w:p><w:r><w:t>photosynthesis</w:t></w:r></w:p></w:body></w:document>')
    invoke(project, 'import', path='lesson.docx')
    hit = invoke(project, 'search', query='photosynthesis')['items'][0]
    assert hit['path'] == 'lesson.docx' and '第 1 段' in hit['locator']


def test_real_pdf_text_page_citation_and_scanned_pdf_refusal(project):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
                             NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
    stream = DecodedStreamObject()
    stream.set_data(b'BT /F1 12 Tf 20 250 Td (chloroplast energy evidence) Tj ET')
    page[NameObject('/Contents')] = writer._add_object(stream)
    with (project[1] / 'paper.pdf').open('wb') as out:
        writer.write(out)
    invoke(project, 'import', path='paper.pdf')
    hit = invoke(project, 'search', query='chloroplast')['items'][0]
    assert hit['path'] == 'paper.pdf' and '第 1 页' in hit['locator']
    blank = PdfWriter()
    blank.add_blank_page(width=100, height=100)
    blank.write(str(project[1] / 'scanned.pdf'))
    with pytest.raises(ValueError, match='OCR'):
        invoke(project, 'import', path='scanned.pdf')


def test_cancellation_and_revoked_permissions_do_not_commit(project):
    event = threading.Event()
    context = {'cancel_event': event, 'execution': None}
    original = study_tools._extract
    def stopped(path):
        value = original(path)
        event.set()
        return value
    with patch.object(study_tools, '_extract', side_effect=stopped), pytest.raises(InterruptedError):
        study_tools.call(project[0], 'study_write', {'operation': 'import', 'path': 'lesson.md'}, context)
    assert not (project[0] / 'learning').exists()
    event.clear()
    original_write = study_tools._write
    def revoke(*args):
        result = original_write(*args)
        config = project[2]
        config['computer_tools']['mode'] = 'read_only'
        (project[0] / 'friend.json').write_text(json.dumps(config), 'utf-8')
        return result
    with patch.object(study_tools, '_write', side_effect=revoke), pytest.raises(PermissionError):
        study_tools.call(project[0], 'study_write', {'operation': 'import', 'path': 'lesson.md'}, context)
    assert invoke(project, 'list')['materials'] == []


def test_plan_template_uses_existing_goal_worker(project, monkeypatch):
    import task_catalog
    from task_manager import TaskManager
    manager = TaskManager(project[0].parents[1] / 'data/tasks.sqlite3', project[0].parents[1])
    monkeypatch.setattr(TaskManager, 'get_instance', lambda: manager)
    monkeypatch.setattr(TaskManager, 'start_worker', lambda *_: None)
    try:
        result = task_catalog.create(project[0].parents[1], '10001', project[0], 'study_path',
                                     {'description': '学习光合作用'}, max_calls=6, auto_start=False)
        task = manager.get_task(result['task_id'])
        assert task.category == 'goal_plan'
        assert task.checkpoint['goal_request']['max_calls'] == 6
        assert task.checkpoint['auto_start'] is False
        assert 'study_read/study_write' in task.description
    finally:
        manager.stop_worker()
