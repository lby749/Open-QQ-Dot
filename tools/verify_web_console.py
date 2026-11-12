"""Optional real-browser console check with synthetic API fixtures, never sends QQ.

Run after installing Playwright; uses an existing Edge on Windows.
This validates DOM behavior, not a real model or QQ backend.
"""
import argparse,json,os
from pathlib import Path
from playwright.sync_api import sync_playwright
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--report', type=Path, default=Path('build/web-console-check.json'))
args=parser.parse_args()
html=(Path(__file__).resolve().parents[1]/'house_web.html').read_text('utf-8')
with sync_playwright() as p:
    browser=p.chromium.launch(**({'channel':'msedge'} if os.name=='nt' else {}))
    page=browser.new_page(viewport={'width':420,'height':820})
    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
    page.set_content(html)
    page.evaluate("""() => {
      friendQQ='12345';view={name:'friend',tab:'agent',qq:'12345'};
      agentConfigData={provider:'codex',model:'fixture-model',effort:'low',efforts:[]};
      currentAgentWs='default';currentAgentSid='__qq_stream__';
      window.messages=[{id:'m1',role:'user',content:'先前确认的消息'}];window.sends=[];
      api=async function(path,body){
        if(path.includes('/agent_session?')) return {is_qq:true,messages:window.messages};
        if(path.endsWith('/agent_send')) {window.sends.push(body);window.messages.push({id:'m2',role:'user',content:body.text});return {is_qq:true};}
        return {};
      };
      window.ws={active_id:'default',workspaces:[{id:'default',title:'默认项目'}]};
      window.sess={sessions:[{id:'main',title:'主会话'}]};
      return renderAgentConsole('12345',window.ws,window.sess);
    }""")
    box=page.locator('textarea[placeholder="向好友发送 QQ 消息…"]')
    box.fill('正在编辑的草稿')
    page.evaluate("window.messages.push({id:'m3',role:'assistant',content:'QQ端刚发出的消息'});")
    page.wait_for_function("document.querySelector('.agent-msg-list').textContent.includes('QQ端刚发出的消息')")
    draft_kept=box.input_value()=='正在编辑的草稿'
    box.fill('中文输入法尚在组词');box.dispatch_event('keydown', {'key':'Enter','isComposing':True});page.wait_for_timeout(50)
    composition_did_not_send=page.evaluate('window.sends.length')==0
    box.fill('浏览器回车发送测试');box.press('Enter')
    page.wait_for_function("window.sends.length===1")
    page.wait_for_timeout(150)
    sent_count=page.locator('.agent-msg-list').inner_text().count('浏览器回车发送测试')
    stale=page.evaluate("""async () => {
      let release;api=async function(path){if(path.includes('/agent_session?'))return new Promise(r=>release=r);return {};};
      const pending=renderAgentConsole('12345',window.ws,window.sess);
      friendQQ='67890';view={name:'friend',tab:'agent',qq:'67890'};
      document.querySelector('main').textContent='另一位好友的界面';
      release({is_qq:true,messages:[{role:'assistant',content:'上一位好友的迟到消息'}]});
      await pending;return document.querySelector('main').textContent;
    }""")
    report={'actual_browser':'existing Edge','backend':'controlled fixtures; real QQ validated separately','draft_preserved_on_poll':draft_kept,'enter_sends_once':page.evaluate('window.sends.length')==1,'confirmed_bubble_count':sent_count,'late_friend_result_ignored':stale=='另一位好友的界面','composition_enter_does_not_send':composition_did_not_send,'page_errors':errors}
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2));browser.close()
    assert all(report[k] for k in ('draft_preserved_on_poll','enter_sends_once','late_friend_result_ignored','composition_enter_does_not_send'))
    assert report['confirmed_bubble_count']==1 and not report['page_errors']
