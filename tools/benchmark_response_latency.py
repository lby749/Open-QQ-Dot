from pathlib import Path
import os, sys, tempfile, time, threading, statistics, logging
sys.path.insert(0,os.getcwd())
logging.disable(logging.CRITICAL)
from task_manager import TaskManager
from agent_messaging import AgentMailbox
from unittest.mock import patch
results={}
with tempfile.TemporaryDirectory() as d:
 root=Path(d)
 m=TaskManager(db_path=root/'tasks.db',workspace=root)
 fired={}; evt={}
 def execute(t):
  fired[t.task_id]=time.perf_counter()
  with m._db_lock, m._get_connection() as db:
   db.execute("UPDATE tasks SET status='COMPLETED' WHERE task_id=?",(t.task_id,))
  evt.setdefault(t.task_id,threading.Event()).set()
 m._process_single_task=execute
 import agenda
 with patch.object(agenda.ProactiveActionEngine,'get_instance') as get:
  get.return_value.ensure_builtin_triggers.return_value=None
  get.return_value.poll_events_and_act.return_value=None
  assert m.start_worker()
  timings=[]
  try:
   for i in range(18):
    time.sleep(.018 + (i%5)*.009)
    started=time.perf_counter()
    tid=m.enqueue_agent_mail_task('alice', 'alice/p/agent', 'agent')
    if not tid:raise RuntimeError('not enqueued')
    evt.setdefault(tid,threading.Event()).wait(3)
    assert tid in fired, tid
    timings.append(1000*(fired[tid]-started))
  finally:m.stop_worker()
  results['task_ms']=timings
with tempfile.TemporaryDirectory() as d:
 mail=AgentMailbox(Path(d)/'mail.db')
 a=mail.register_agent('alice','p','sender','bench')
 b=mail.register_agent('alice','p','receiver','bench')
 times=[]
 for i in range(14):
  started=threading.Event(); done={}
  def waiter():
   started.set(); received=mail.wait_for_inbox(b, timeout_seconds=2)
   done['t']=time.perf_counter();done['items']=received
  t=threading.Thread(target=waiter,daemon=True);t.start();started.wait();time.sleep(.027+(i%3)*.007)
  before=time.perf_counter();msg=mail.send(a,b,'ping '+str(i))
  t.join(timeout=3)
  assert not t.is_alive() and done.get('items') and done['items'][0].message_id==msg.message_id
  times.append(1000*(done['t']-before));mail.acknowledge(b,msg.message_id)
 results['mail_ms']=times
for key,values in results.items():
 print(key, 'median=%.1f mean=%.1f p90=%.1f max=%.1f'%(statistics.median(values),statistics.mean(values),sorted(values)[int(len(values)*.9)],max(values)))
