"""Execute mobile controls with a small DOM; assert interaction rather than source strings."""
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


@pytest.mark.skipif(not shutil.which('node'), reason='Node is needed to execute browser control code')
def test_mobile_learning_import_search_and_plain_text_rendering():
    html = (Path(__file__).resolve().parents[1] / 'house_web.html').read_text('utf-8')
    function = re.search(r'async function studyPage\(qq\) \{.*?\n\}', html, re.S).group()
    script = r'''
const assert = require('node:assert/strict');
const source = JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const nodes=[],calls=[];
function el(tag,attrs,...children) {const n={tag,...attrs,children,value:attrs?.value||''};nodes.push(n);return n;}
function input(attrs){return el('input',attrs);}
function button(text,work){return el('button',{onclick:work},text);}
const card=(...children)=>el('section',{},...children),show=()=>{};
async function api(path,body){calls.push({path,body}); return path.includes('search')?
  {items:[{path:'lesson.md',locator:'正文',id:'mat_test:0',text:'<img src=x onerror=alert(1)>叶绿体'}]}:
  body?{status:'资料已导入'}:{materials:[{path:'lesson.md'}],records:[]};}
const page=new Function('el','input','button','card','show','api',source+';return studyPage;')(el,input,button,card,show,api);
(async()=>{
  await page('10001');
  nodes.find(n=>n.placeholder?.includes('相对路径')).value='lesson.md';
  await nodes.find(n=>n.tag==='button'&&n.children.includes('导入资料')).onclick();
  assert.equal(calls.find(c=>c.body).body.path,'lesson.md');
  nodes.find(n=>n.placeholder==='要查找的知识点').value='光合作用';
  await nodes.find(n=>n.tag==='button'&&n.children.includes('检索原文')).onclick();
  const output=nodes.find(n=>n.textContent?.includes('叶绿体'));
  assert.ok(output.textContent.includes('<img'));
  assert.equal(output.innerHTML,undefined);
  assert.ok(calls.every(c=>c.path.includes('/10001/')));
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run(['node', '-e', script], input=json.dumps(function), text=True,
                            encoding='utf-8', capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
