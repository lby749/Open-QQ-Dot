"""Native workflow editor; all runs still use TaskManager goals and approvals."""
import copy
from datetime import datetime

from PySide6.QtCore import QDateTime, QTimer
from PySide6.QtWidgets import QDateTimeEdit, QFormLayout, QSpinBox, QTableWidgetItem
from qfluentwidgets import BodyLabel, CheckBox, ComboBox, LineEdit, PlainTextEdit, PushButton, TableWidget

from .common import Card, Page, hint, row, toast
from .task_center import CATEGORY, SPIN_STYLE


class WorkflowPage(Page):
    def __init__(self, house):
        super().__init__(house,'workflowPage')
        self.identifier,self.version,self.owner,self.steps,self.stage_index='',0,'',[],-1
        self.selector=ComboBox()
        self.selector.currentIndexChanged.connect(self.choose)
        self.title,self.scope,self.criteria=LineEdit(),LineEdit(),LineEdit()
        self.correction=PlainTextEdit()
        self.correction.setFixedHeight(70)
        self.correction.setPlaceholderText('这次纠正了什么？会随版本保留。流程修改后需重新开启定时执行。')
        self.space=ComboBox()
        self.calls,self.tokens=QSpinBox(),QSpinBox()
        self.calls.setRange(1,200); self.calls.setValue(20)
        self.tokens.setRange(1000,2000000); self.tokens.setValue(100000)
        self.notify=CheckBox('通过 QQ 按当前好友人设汇报')
        self.auto_finish=CheckBox('全部阶段客观验收通过后自动完成整轮，允许长期连续运行')
        self.auto_finish.stateChanged.connect(self.change_review_policy)
        self.status=hint('从全部任务中选择已验收任务，点击「学习为工作流」；也可以新建。')
        self.add(Card('可学习工作流').add(row(self.selector, self.button('刷新',self.load),self.button('新建',self.new)),
            self.form([('名称',self.title),('授权范围',self.scope),('总体验收',self.criteria),('项目目录',self.space)]),
            row(BodyLabel('每轮最多调用'),self.calls,BodyLabel('每轮词元预算'),self.tokens,None),self.notify,self.auto_finish,
            hint('自动模式以各阶段配置的客观验收作为整轮标准；研究、通用任务或其他需人工审查的阶段不会自动确认。'),
            self.correction,row(self.button('保存新版本',self.save),self.button('执行一次',self.run_once)),self.status))
        self.stage=ComboBox()
        self.stage.currentIndexChanged.connect(self.choose_stage)
        self.stage_title,self.description=LineEdit(),PlainTextEdit()
        self.description.setFixedHeight(120)
        self.category,self.backend=ComboBox(),ComboBox()
        for key in ('general','code','file','data','research','browser','video'):
            self.category.addItem(CATEGORY[key],userData=key)
        self.files,self.test,self.keys,self.url,self.expected=LineEdit(),LineEdit(),LineEdit(),LineEdit(),LineEdit()
        self.actions=TableWidget(); self.actions.setColumnCount(3)
        self.actions.setHorizontalHeaderLabels(['操作','定位元素／方向／地址','填写内容／滚动像素'])
        self.actions.horizontalHeader().setStretchLastSection(True)
        self.actions.setMinimumHeight(160)
        self.stage_form = self.form([('阶段名称',self.stage_title),('任务类型',self.category),('执行智能体',self.backend),
            ('执行说明',self.description),('目标文件（逗号分隔）',self.files),('测试命令',self.test),
            ('数据字段（逗号分隔）',self.keys),('网页地址',self.url),('网页验收文字',self.expected)])
        self.action_hint = hint('录制的网页操作可直接在下表修改。操作填写：点击、输入、滚动、打开网页、等待、读取、结束。')
        self.add(Card('阶段与跨智能体交接').add(
            hint('按顺序执行，前序阶段验收通过后才交接。智能体只从当前好友已启用的引擎中选择；留空沿用当前编排。'),
            row(self.stage,self.button('添加阶段',self.add_stage),self.button('删除阶段',self.remove_stage)),
            self.stage_form, self.action_hint,self.actions))
        self.category.currentIndexChanged.connect(lambda: self.update_category(True))
        self.update_category()
        self.record_url=LineEdit(); self.record_url.setPlaceholderText('https://要示教的网页')
        self.recorder=None
        self.add(Card('浏览器鼠标键盘示教').add(
            hint('点击开始后打开独立浏览器，录制点击、文字填写和滚动；不会监听其他应用。'
                 '密码或令牌输入不录制；此版本请点击按钮提交，不支持回车提交。最多 20 步、10 分钟。'
                 '停止后先成为可编辑草稿，不会自动回放。系统全桌面操作录制尚未接入。'),
            self.record_url,row(self.button('打开网页并开始示教',self.start_recording),
                                self.button('停止示教并学习为流程',self.finish_recording))))
        self.interval,self.rounds=QSpinBox(),QSpinBox()
        self.interval.setRange(1,525600); self.interval.setValue(1440)
        self.rounds.setRange(1,10000); self.rounds.setValue(1)
        self.first=QDateTimeEdit(QDateTime.currentDateTime())
        self.first.setDisplayFormat('yyyy-MM-dd HH:mm')
        self.first.setCalendarPopup(True)
        self.add(Card('长期与周期执行').add(
            hint('使用本机时间。电脑和任务服务需保持运行；重启后跳过错过的轮次，只补一次。'
                 '上一轮未结束或等你验收时不重叠执行；失败或额度受阻时保留检查点。'),
            self.form([('首次执行时间',self.first),('重复间隔（分钟）',self.interval),('最多再执行几轮',self.rounds)]),
            row(self.button('保存并开启定时执行',self.enable),self.button('停止后续定时执行',self.disable)),
            hint('停止定时不会终止当前一轮；当前目标可在全部任务中暂停或终止。')))
        self.history=PlainTextEdit()
        self.history.setReadOnly(True)
        self.history.setMinimumHeight(160)
        self.add(Card('版本与运行记录').add(self.history))
        for spin in (self.calls,self.tokens,self.interval,self.rounds):
            spin.setStyleSheet(SPIN_STYLE)

    def button(self,title,action):
        button=PushButton(title)
        button.clicked.connect(action)
        return button

    def form(self,fields):
        from PySide6.QtWidgets import QWidget
        host=QWidget(); layout=QFormLayout(host)
        for label,field in fields:
            layout.addRow(BodyLabel(label),field)
        return host

    def store(self):
        from task_manager import TaskManager
        from task_workflows import WorkflowStore
        return WorkflowStore(TaskManager.get_instance())

    def change_review_policy(self,*_):
        enabled=self.auto_finish.isChecked()
        self.criteria.setEnabled(not enabled)
        if enabled:
            from task_workflows import AUTOMATIC_CRITERIA
            self.criteria.setText(AUTOMATIC_CRITERIA)

    def load(self,select=None):
        owner=self.house.current_qq
        if not owner:
            return
        from session_manager import list_workspaces
        from task_workflows import providers
        self.owner=owner
        self.space.clear()
        for space in list_workspaces(owner):
            self.space.addItem(space['title'],userData=space['id'])
        self.backend.clear(); self.backend.addItem('沿用当前智能体编排',userData='')
        from multi_agent_orchestrator import get_agent_name
        for provider in providers(self.house.friend_dir):
            self.backend.addItem(get_agent_name(provider),userData=provider)
        flows=self.store().list(owner)
        chosen=select or self.identifier
        self.selector.blockSignals(True); self.selector.clear()
        for flow in flows:
            self.selector.addItem(flow['title']+f'（第 {flow["version"]} 版）',userData=flow['workflow_id'])
        index=next((i for i in range(self.selector.count()) if self.selector.itemData(i)==chosen),0)
        self.selector.setCurrentIndex(index); self.selector.blockSignals(False)
        if flows:
            self.choose()
        else:
            self.new()

    shown=lambda self:self.load()

    def choose(self,*_):
        identifier=self.selector.currentData()
        if not identifier or self.owner!=self.house.current_qq:
            return
        flow=self.store().get(identifier,self.owner)
        self.identifier,self.version=identifier,flow['version']
        definition=flow['definition']
        self.title.setText(flow['title']); self.scope.setText(definition['scope']); self.criteria.setText(definition['criteria'])
        self.auto_finish.setChecked(definition.get('review_policy')=='verified_stages')
        self.change_review_policy()
        self.calls.setValue(definition['max_calls']); self.tokens.setValue(definition['token_budget'])
        self.notify.setChecked(bool(definition['defaults'].get('notify_qq')))
        self.space.setCurrentIndex(max(0,self.space.findData(definition['defaults'].get('workspace_id','default'))))
        self.steps=copy.deepcopy(definition['steps']); self.stage_index=-1; self.fill_stages()
        self.interval.setValue(max(1,flow['interval_seconds']//60)); self.rounds.setValue(max(1,flow['remaining_runs']))
        if flow['next_run']:
            self.first.setDateTime(QDateTime.fromSecsSinceEpoch(int(flow['next_run'])))
        self.correction.clear()
        self.status.setText(('定时执行已开启' if flow['enabled'] else '定时执行已关闭')+
                           f'；已启动 {flow["run_count"]} 轮。'+flow['error'])
        from .autonomy import STATUS
        self.history.setPlainText('\n'.join(
            [f'第 {v["version"]} 版：{v["correction"]}' for v in flow['versions']]+
            [f'{datetime.fromtimestamp(r["created_at"]):%m-%d %H:%M} · {STATUS.get(r["status"],r["status"])} · {r["goal_id"]}' for r in flow['runs']]))

    def new(self):
        self.identifier,self.version,self.stage_index='',0,-1
        self.title.clear(); self.scope.setText('仅在所选项目与当前工具权限内执行')
        self.criteria.setText('每个阶段通过验收，最终结果由我确认')
        self.auto_finish.setChecked(False); self.criteria.setEnabled(True)
        self.steps=[{'title':'第一阶段','description':'','category':'general','spec':{}}]
        self.fill_stages(); self.correction.clear(); self.history.clear()
        self.notify.setChecked(False)
        self.status.setText('填写并保存流程；保存不会自动执行。')

    def sync_stage(self):
        if not 0<=self.stage_index<len(self.steps):
            return
        from task_catalog import _files
        spec=copy.deepcopy(self.steps[self.stage_index].get('spec') or {})
        category = self.category.currentData()
        allowed = {'file': {'target_files','test_command'}, 'code': {'target_files','test_command'},
                   'data': {'expected_keys'}, 'browser': {'url','expected_text','browser_steps'},
                   'video': {'target_files'}}.get(category, set())
        for key in ('test_command','url','expected_text','target_files','expected_keys','browser_steps'):
            if key not in allowed: spec.pop(key, None)
        for key,edit in (('test_command',self.test),('url',self.url),('expected_text',self.expected)):
            spec.pop(key,None)
            if key in allowed and edit.text().strip(): spec[key]=edit.text().strip()
        spec.pop('target_files',None); spec.pop('expected_keys',None)
        if 'target_files' in allowed and self.files.text().strip(): spec['target_files']=_files(self.files.text())
        if 'expected_keys' in allowed and self.keys.text().strip(): spec['expected_keys']=[k.strip() for k in self.keys.text().split(',') if k.strip()]
        if category == 'browser' and (self.actions.rowCount() or 'browser_steps' in spec):
            names={'点击':'click','输入':'input','滚动':'scroll','打开网页':'navigate','等待':'wait','读取':'read','结束':'done'}
            actions=[]
            for index in range(self.actions.rowCount()):
                values=[self.actions.item(index,col).text() if self.actions.item(index,col) else '' for col in range(3)]
                if values[0] not in names: raise ValueError('录制操作名称无效')
                action=names[values[0]]; step={'action':action}
                if action in ('click','input','wait','read'): step['selector']=values[1]
                if action=='input': step['text']=values[2]
                if action=='navigate': step['url']=values[1]
                if action=='scroll': step.update(direction='up' if values[1]=='上' else 'down',pixels=int(values[2]))
                actions.append(step)
            spec['browser_steps']=actions
        self.steps[self.stage_index]={'title':self.stage_title.text().strip(),'description':self.description.toPlainText().strip(),
            'category':self.category.currentData(),'backend':self.backend.currentData() or '', 'spec':spec}

    def fill_stages(self):
        self.stage.blockSignals(True); self.stage.clear()
        for index,step in enumerate(self.steps): self.stage.addItem(f'{index+1}. {step["title"]}')
        self.stage.setCurrentIndex(0); self.stage.blockSignals(False)
        self.stage_index=-1; self.choose_stage()

    def choose_stage(self,*_):
        try:
            self.sync_stage()
        except Exception as error:
            self.stage.blockSignals(True); self.stage.setCurrentIndex(self.stage_index); self.stage.blockSignals(False)
            return toast(self.house,'error',str(error))
        self.stage_index=self.stage.currentIndex()
        if not 0<=self.stage_index<len(self.steps): return
        step=self.steps[self.stage_index]; spec=step.get('spec') or {}
        self.stage_title.setText(step['title']); self.description.setPlainText(step['description'])
        self.category.blockSignals(True)
        self.category.setCurrentIndex(max(0,self.category.findData(step['category'])))
        self.category.blockSignals(False)
        backend = step.get('backend','')
        index=self.backend.findData(backend)
        if index<0:
            self.backend.addItem(backend+'（当前未启用）' if backend else '沿用当前智能体编排',userData=backend); index=self.backend.count()-1
        self.backend.setCurrentIndex(index)
        self.files.setText(', '.join(spec.get('target_files',[]))); self.keys.setText(', '.join(spec.get('expected_keys',[])))
        for key,edit in (('test_command',self.test),('url',self.url),('expected_text',self.expected)): edit.setText(str(spec.get(key,'')))
        names={'click':'点击','input':'输入','scroll':'滚动','navigate':'打开网页','wait':'等待','read':'读取','done':'结束'}
        actions=spec.get('browser_steps') or []; self.actions.setRowCount(len(actions))
        for index,action in enumerate(actions):
            target=action.get('selector',action.get('url','上' if action.get('direction')=='up' else '下'))
            for col,text in enumerate((names.get(action['action'],action['action']),target,action.get('text',action.get('pixels','')))):
                self.actions.setItem(index,col,QTableWidgetItem(str(text)))
        self.update_category()

    def update_category(self, clear=False):
        category = self.category.currentData()
        for field, visible in ((self.files, category in ('code','file','video')),
                (self.test, category in ('code','file')), (self.keys, category == 'data'),
                (self.url, category == 'browser'), (self.expected, category == 'browser')):
            field.setVisible(visible)
            self.stage_form.layout().labelForField(field).setVisible(visible)
            if clear and not visible: field.clear()
        self.actions.setVisible(category == 'browser')
        self.action_hint.setVisible(category == 'browser')
        if clear and category != 'browser': self.actions.setRowCount(0)

    def add_stage(self):
        self.sync_stage(); self.steps.append({'title':'新阶段','description':'','category':'general','spec':{}})
        self.fill_stages(); self.stage.setCurrentIndex(len(self.steps)-1)

    def remove_stage(self):
        if len(self.steps)>1 and self.stage_index>=0:
            self.steps.pop(self.stage_index); self.stage_index=-1; self.fill_stages()

    def save(self):
        try:
            if self.owner!=self.house.current_qq: raise ValueError('好友已切换，请重新打开流程')
            self.sync_stage()
            identifier=self.store().save(self.owner,self.title.text(),{'scope':self.scope.text(),'criteria':self.criteria.text(),
                'steps':self.steps,'defaults':{'workspace_id':self.space.currentData() or 'default','notify_qq':self.notify.isChecked()},
                'review_policy':'verified_stages' if self.auto_finish.isChecked() else 'manual',
                'max_calls':self.calls.value(),'token_budget':self.tokens.value()},identifier=self.identifier,
                version=self.version,correction=self.correction.toPlainText())
            self.load(select=identifier)
            return True
        except Exception as error:
            toast(self.house,'error',str(error)); return False

    def run_once(self):
        if not self.save(): return
        try:
            store=self.store(); goal=store.run(self.identifier,self.owner)
            if not goal: raise ValueError('上一轮尚未结束，请在全部任务中处理')
            store.manager.start_worker(); self.load(select=self.identifier)
            toast(self.house,'success','流程已进入任务队列，可在全部任务中查看或暂停。')
        except Exception as error: toast(self.house,'error',str(error))

    def enable(self):
        interval,due,rounds=self.interval.value()*60,self.first.dateTime().toSecsSinceEpoch(),self.rounds.value()
        if not self.save(): return
        try:
            store=self.store()
            store.schedule(self.identifier,self.owner,enabled=True,interval_seconds=interval,
                next_run=due,remaining_runs=rounds)
            store.manager.start_worker(); self.load(select=self.identifier)
        except Exception as error: toast(self.house,'error',str(error))

    def disable(self):
        try:
            if self.owner!=self.house.current_qq: raise ValueError('好友已切换')
            self.store().schedule(self.identifier,self.owner,enabled=False)
            self.load(select=self.identifier)
        except Exception as error: toast(self.house,'error',str(error))

    def start_recording(self):
        try:
            if self.recorder: raise ValueError('请先停止当前示教')
            from browser_demonstration import DemonstrationRecorder
            import uuid
            base=self.house.friend_dir/'media/demonstrations'/uuid.uuid4().hex
            recorder=DemonstrationRecorder(self.house.current_qq,base,self.record_url.text().strip())
            recorder.start(); self.recorder=recorder
            toast(self.house,'info','正在打开独立示教浏览器；操作完毕后回到这里停止并保存。')
            def check_ready():
                if self.recorder is not recorder: return
                if not recorder._ready.is_set():
                    QTimer.singleShot(200,check_ready)
                elif recorder.error:
                    self.recorder=None
                    toast(self.house,'error',recorder.error,'示教浏览器未能打开')
            QTimer.singleShot(200,check_ready)
        except Exception as error: toast(self.house,'error',str(error))

    def finish_recording(self):
        recorder=self.recorder
        if not recorder: return toast(self.house,'warning','尚未开始示教')
        from .common import run_async
        store,owner=self.store(),self.house.current_qq
        def done(identifier):
            self.recorder=None
            if self.house.current_qq==owner: self.load(select=identifier)
            toast(self.house,'success','已学习为流程草稿，请检查录制步骤和验收标准后再执行。')
        def failed(error):
            self.recorder=None
            toast(self.house,'error',str(error),'示教未保存为流程')
        run_async(lambda _:recorder.save_workflow(store,owner),done,failed)
