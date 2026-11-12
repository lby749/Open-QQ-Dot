"""Execute the real task dashboard function with a minimal DOM and mocked HTTP."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for dashboard interaction checks')
class GoalDashboard(unittest.TestCase):
    def test_create_propose_and_confirm_controls(self):
        html = (Path(__file__).resolve().parents[1]/'house_web.html').read_text('utf-8')
        function = re.search(r'async function openTaskDashboardModal\(qq\) \{.*?\n\}', html, re.S).group()
        function = re.search(r'const TASK_STATES = \{.*?\};', html, re.S).group() + '\n' + function
        script = r'''
const assert = require('node:assert/strict');
const source = JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const nodes = [], calls = [], messages = [];
function el(tag, attrs, ...children) {
  const node = {tag, ...attrs, children, style:{}, value:attrs.value || '',
    classList:{add(){},remove(){},toggle(){}},
    append(...items){this.children.push(...items)},
    prepend(...items){this.children.unshift(...items)},
    replaceChildren(...items){this.children=items},
    click(){return this.onclick && this.onclick()}};
  nodes.push(node); return node;
}
let taskList=[];
async function api(path, body) {
  calls.push({path,body});
  if (body) return {message:'ok',goal_id:'g',task_id:'p'};
  return path.endsWith('agent_tasks') ? {tasks:taskList} : {goals:[]};
}
const document = {body:{append(){},removeChild(){}}};
const shown = [];
const card = (...children) => ({tag:'div', children, append(){}, replaceChildren(){}});
const show = node => { shown.push(node); document.body.lastChild = node; };
const open = new Function('el','api','document','toast','prompt','confirm','show','card', source+';return openTaskDashboardModal;')(
  el,api,document,message=>messages.push(message),()=> 'checked',()=>true,show,card);
const findText = text => nodes.findLast(n=>n.tag==='button' && n.children.includes(text));
const findPlaceholder = text => nodes.findLast(n=>n.placeholder && n.placeholder.startsWith(text));
const tick = () => new Promise(resolve=>setImmediate(resolve));
(async()=>{
  await open('12345'); await tick();
  findText('新建目标').click(); await tick();
  findPlaceholder('长期目标').value='维护项目';
  findPlaceholder('授权范围').value='仅测试目录';
  findPlaceholder('总体验收').value='通过测试';
  findPlaceholder('阶段计划').value='检查 | 查找问题\n修复 | 修复问题';
  await findText('授权并创建目标').click(); await tick();
  const creation=calls.find(c=>c.body?.action==='create');
  assert.equal(creation.body.milestones.length,2);
  assert.equal(creation.body.scope,'仅测试目录');
  assert.equal(creation.body.max_calls,100);
  assert.ok(creation.path.includes('/12345/'));
  findText('新建目标').click(); await tick();
  findPlaceholder('长期目标').value='研究';
  findPlaceholder('授权范围').value='只读';
  findPlaceholder('总体验收').value='证据';
  await findText('先让 AI 生成阶段草案').click(); await tick();
  assert.ok(calls.some(c=>c.body?.action==='propose'));
  await findText('AI 规划并自动执行').click(); await tick();
  assert.ok(calls.some(c=>c.body?.action==='propose' && c.body.auto_start===true));
  taskList=[{task_id:'planner',title:'规划',category:'goal_plan',description:'范围',status:'WAITING_APPROVAL',result:'{"milestones":[]}',artifacts:[]}];
  findText('任务').click(); await tick();
  await findText('确认计划并创建目标').click(); await tick();
  assert.ok(calls.some(c=>c.body?.action==='accept_plan' && c.body.task_id==='planner'));
  assert.equal(messages.some(m=>m.includes('出错')),false);
  console.log('dashboard create/propose/confirm interactions passed');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        result = subprocess.run(['node', '-e', script], input=json.dumps(function),
                                capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)


if __name__ == '__main__':
    unittest.main()
