import json
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

import pytest
pytest.importorskip('playwright')

from browser_demonstration import DemonstrationRecorder


def test_recording_coalesces_typing_and_refuses_secret_fields_without_saving_values(tmp_path):
    recorder=DemonstrationRecorder('12345',tmp_path,'https://example.com')
    recorder.accept({'action':'input','selector':'#name','text':'a'})
    recorder.accept({'action':'input','selector':'#name','text':'ab'})
    assert recorder.steps==[{'action':'input','selector':'#name','text':'ab'}]
    recorder.accept({'action':'blocked','reason':'敏感输入未录制'})
    with pytest.raises(ValueError,match='敏感输入'): recorder.finish()


def test_switching_friend_stops_recording_instead_of_saving_to_another_owner(tmp_path):
    recorder=DemonstrationRecorder('12345',tmp_path,'https://example.com')
    recorder.accept({'action':'click','selector':'#button'})
    with pytest.raises(PermissionError,match='不能保存到其他好友'):
        recorder.save_workflow(None,'67890')
    assert recorder._stop.is_set()


def test_actual_browser_records_input_and_click_then_worker_replays_and_verifies(tmp_path,monkeypatch):
    monkeypatch.setenv('QQ_BRIDGE_HOME',str(tmp_path))
    from task_manager import TaskManager
    from task_workflows import WorkflowStore
    import browser_automation
    directory=tmp_path/'friends/12345'; directory.mkdir(parents=True)
    (directory/'friend.json').write_text('{"qq_id":"12345","model_api":{"provider":"codex"}}','utf-8')
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            trigger="""<script>setTimeout(()=>{let box=document.querySelector('#name');box.value='示教';
                box.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#apply').click()},500)</script>""" if self.path=='/capture' else ''
            body=('''<!doctype html><meta charset="utf-8"><input id="name"><button id="apply"
                onclick="document.querySelector('#result').textContent='REPLAY_OK:'+document.querySelector('#name').value">应用</button>
                <span id="result"></span>'''+trigger).encode()
            self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8')
            self.end_headers(); self.wfile.write(body)
        def log_message(self,*args): pass
    # Chrome blocks some OS-assigned ephemeral ports (for example 6668) as
    # unsafe. Use a normal local test port first, then fall back to an
    # ephemeral port only when the fixed ports are occupied.
    server = None
    for port in (8877, 8899, 0):
        try:
            server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
            break
        except OSError:
            continue
    assert server is not None
    threading.Thread(target=server.serve_forever,daemon=True).start()
    origin=f'http://127.0.0.1:{server.server_port}'
    recorder=DemonstrationRecorder('12345',tmp_path/'recording',origin+'/capture',headless=True,allowed_local_origins=[origin])
    try:
        recorder.start(); assert recorder._ready.wait(25)
        until=time.monotonic()+8
        while len(recorder.steps)<2 and not recorder.error and time.monotonic()<until:
            time.sleep(.1)
        recording=recorder.finish()
        assert [step['action'] for step in recording['steps']]==['input','click'],recorder.error
        assert recording['steps'][0]['text']=='示教'
        manager=TaskManager(tmp_path/'data/tasks.sqlite3',tmp_path)
        store=WorkflowStore(manager)
        identifier=recorder.save_workflow(store,'12345')
        definition=store.get(identifier,'12345')['definition']
        definition['steps'][0]['spec'].update(url=origin+'/',expected_text='REPLAY_OK:示教')
        store.save('12345','回放示教',definition,identifier=identifier,version=1,correction='使用不自动操作的测试页面验收')
        from browser_automation import IsolatedBrowserSession
        monkeypatch.setattr(browser_automation,'get_browser_session',lambda owner,**kw:IsolatedBrowserSession(owner,
            base_dir=kw['base_dir'],use_headless_chromium=True,allow_local_test_origins=True,allowed_local_origins=[origin]))
        import multi_agent_orchestrator
        monkeypatch.setattr(multi_agent_orchestrator,'call_single_agent',lambda *a,**kw:pytest.fail('录制回放不应调用模型'))
        goal=store.run(identifier,'12345')
        manager._process_single_task(manager._fetch_next_executable_task())
        result=manager.get_goal(goal)
        assert result['status']=='WAITING_REVIEW' and result['progress']==1, result['tasks']
        task=manager.get_task(result['tasks'][0]['task_id'])
        assert task.artifacts and all(__import__('pathlib').Path(path).is_file() for path in task.artifacts)
        assert json.loads(task.result)['page']['content'].find('REPLAY_OK:示教')>=0
        manager.stop_worker()
    finally:
        recorder._stop.set()
        if recorder._thread: recorder._thread.join(timeout=25)
        server.shutdown(); server.server_close()
