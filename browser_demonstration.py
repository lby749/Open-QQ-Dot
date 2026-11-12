"""Opt-in browser demonstration recording. No global keyboard hooks or model calls."""
import json
import os
from pathlib import Path
import threading
import time

SCRIPT = r'''
(() => {
  const selector = node => {
    if (node.id) return '#' + CSS.escape(node.id);
    const parts=[];
    for (let n=node; n && n.nodeType===1 && parts.length<8; n=n.parentElement) {
      if(n.id){parts.unshift('#'+CSS.escape(n.id));break;}
      let part=n.tagName.toLowerCase(), index=1;
      for(let p=n.previousElementSibling;p;p=p.previousElementSibling)if(p.tagName===n.tagName)index++;
      parts.unshift(part+':nth-of-type('+index+')');
    }
    return parts.join(' > ');
  };
  const send = data => window.__openqq_record(data).catch(()=>{});
  const sensitive = node => node.type==='password' || /pass|pwd|token|secret|auth|credit|cvv|\bpin\b|one.time.code/i.test(
    [node.name,node.id,node.autocomplete].join(' '));
  addEventListener('click',event=>{
    const node=event.target.closest('a,button,input,select,textarea,[role="button"]') || event.target;
    if(sensitive(node))return;
    send({action:'click',selector:selector(node)});
  },true);
  addEventListener('input',event=>{
    const node=event.target;
    if(sensitive(node)){send({action:'blocked',reason:'敏感输入未录制，请去除登录或密钥填写步骤后重新示教'});return;}
    if(node.matches('input,textarea'))send({action:'input',selector:selector(node),text:node.value});
  },true);
  addEventListener('change',event=>{
    if(event.target.matches('select'))send({action:'input',selector:selector(event.target),text:event.target.value});
  },true);
  addEventListener('keydown',event=>{
    if(event.key==='Enter' && event.target.closest('form'))
      send({action:'blocked',reason:'此示教版本请用鼠标点击提交按钮，不能录制回车提交'});
  },true);
  let last=0,scroll=scrollY;
  addEventListener('scroll',()=>{
    if(Date.now()-last<500)return;
    const delta=scrollY-scroll;
    if(Math.abs(delta)>30){send({action:'scroll',direction:delta>0?'down':'up',pixels:Math.min(3000,Math.abs(Math.round(delta)))});
      scroll=scrollY;last=Date.now();}
  });
})();
'''


class DemonstrationRecorder:
    def __init__(self, owner, base_dir, url, *, headless=False, allowed_local_origins=()):
        self.owner,self.base,self.url=str(owner),Path(base_dir),str(url)
        self.headless,self.allowed_local_origins=headless,list(allowed_local_origins)
        self.steps=[]; self.error=''; self._stop=threading.Event(); self._ready=threading.Event()
        self._lock=threading.Lock(); self._thread=None

    def accept(self, step):
        if self._stop.is_set(): return
        if not isinstance(step,dict): return
        action=step.get('action')
        if action=='blocked':
            self.error=str(step.get('reason') or '此操作无法录制')[:200]; self._stop.set(); return
        allowed={'click':{'action','selector'},'input':{'action','selector','text'},
                 'scroll':{'action','direction','pixels'}}
        if action not in allowed or set(step)-allowed[action] or len(json.dumps(step))>12000: return
        if action in ('click','input') and (not isinstance(step.get('selector'),str) or not step['selector']): return
        if action=='input' and not isinstance(step.get('text'),str): return
        with self._lock:
            if action=='input' and self.steps and self.steps[-1].get('action')=='input' and self.steps[-1]['selector']==step['selector']:
                self.steps[-1]=dict(step)
            elif len(self.steps)<20:
                self.steps.append(dict(step))
            else:
                self.error='达到 20 步上限，请拆分示教流程'; self._stop.set()

    def start(self):
        if self._thread: raise ValueError('示教已经开始')
        self._thread=threading.Thread(target=self._run,name='browser-demonstration',daemon=True)
        self._thread.start()

    def _run(self):
        try:
            from browser_automation import IsolatedBrowserSession
            from playwright.sync_api import sync_playwright
            guard=IsolatedBrowserSession(self.owner,base_dir=self.base/'guard',allow_local_test_origins=bool(self.allowed_local_origins),
                                        allowed_local_origins=self.allowed_local_origins)
            guard._validate_target_url(self.url)
            with sync_playwright() as p:
                options={'channel':'msedge'} if os.name=='nt' else {}
                context=p.chromium.launch_persistent_context(str(self.base/'profile'),headless=self.headless,
                    service_workers='block',accept_downloads=False,**options)
                def route(request):
                    try:
                        guard._validate_target_url(request.request.url)
                        request.continue_()
                    except Exception:
                        request.abort('blockedbyclient')
                context.route('**/*',route)
                context.expose_binding('__openqq_record',lambda source,step:
                    self.accept(step) if source['frame'].parent_frame is None else None)
                context.add_init_script(SCRIPT)
                page=context.pages[0] if context.pages else context.new_page()
                page.goto(self.url,wait_until='domcontentloaded',timeout=20000)
                self._ready.set()
                until=time.monotonic()+600
                while not self._stop.is_set() and time.monotonic()<until and not page.is_closed():
                    page.wait_for_timeout(100)
                if time.monotonic()>=until: self.error='示教达到 10 分钟上限，已停止'
                context.close()
        except Exception as error:
            self.error=str(error)[:500]
        finally:
            self._ready.set()
            self.base.mkdir(parents=True,exist_ok=True)
            (self.base/'recording.json').write_text(json.dumps({'url':self.url,'steps':self.steps,'error':self.error},
                ensure_ascii=False,indent=2),'utf-8')

    def finish(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=25)
            if self._thread.is_alive(): raise RuntimeError('浏览器仍在退出，请稍后再停止')
        if self.error: raise ValueError(self.error)
        if not self.steps: raise ValueError('没有录制到操作，未创建流程')
        return {'url':self.url,'steps':list(self.steps)}

    def save_workflow(self, store, owner):
        if str(owner)!=self.owner:
            try:
                self.finish()
            finally:
                raise PermissionError('示教好友已改变，已停止录制，不能保存到其他好友')
        recording=self.finish()
        definition={'scope':'仅在录制网页和当前项目权限内执行，不登录、不付款、不发布',
            'criteria':'回放步骤成功并留下真实截图；整体结果由我确认','max_calls':1,'token_budget':1000,
            'steps':[{'title':'回放网页示教','description':'按用户录制并确认的步骤操作网页，不扩大权限。',
                'category':'browser','spec':{'url':recording['url'],'browser_steps':recording['steps']}}]}
        return store.save(owner,'网页示教 '+time.strftime('%m-%d %H:%M'),definition,correction='从用户浏览器示教学习，尚未自动执行')
