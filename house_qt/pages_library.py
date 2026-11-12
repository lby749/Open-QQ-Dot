"""Persona template, sticker collection and prompt input (Current Turn Input) pages."""
from ui_locale import tr

import json
from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QTableWidgetItem,
                               QVBoxLayout, QWidget)
from qfluentwidgets import (BodyLabel, CaptionLabel, CheckBox, ComboBox, LineEdit, MessageBoxBase, PlainTextEdit,
                            PrimaryPushButton, PushButton, SubtitleLabel, TableWidget)

from house_status import load_profile
import house_settings
from store import FriendStore

from .common import GREEN, LONG_POOL, Card, Page, TextDialog, body, confirm, hint, row, run_async, toast


class TemplatePage(Page):
    def __init__(self, house):
        super().__init__(house, 'templatePage')
        import_ = PrimaryPushButton(tr('Import Template Package'))
        import_.clicked.connect(self.import_package)
        self.mine = self.add(Card('My Persona Templates', actions=(import_,)).add(
            hint('Template = Persona speaking style (summary, traits, habits) + Vector library (real response examples & tags). '
                 'A template package is a .zip file. Once imported, it can be applied in "Instructions & Persona".')))

        self.rows = []

        self.file = LineEdit()
        self.file.setReadOnly(True)
        choose = PushButton(tr('Select File'))
        choose.clicked.connect(self.choose_file)
        self.speakers = QVBoxLayout()
        self.speaker_boxes = {}
        self.model = ComboBox()
        self.update_model_choices()
        start = PrimaryPushButton(tr('Start Generation'))
        start.clicked.connect(self.start_build)
        self.status = body()
        self.preview = PlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setMinimumHeight(280)
        keep, discard = PrimaryPushButton(tr('Save Templates')), PushButton(tr('Discard'))
        keep.clicked.connect(self.confirm_drafts)
        discard.clicked.connect(self.discard_drafts)
        self.add(Card('Generate Templates from Chat History').add(
            hint('选择本程序「聊天记录导出」生成的 JSON（openqqdot.transcript/1），勾选要学习的说话人；'
                 '也兼容旧版 QQChatExporter JSON。'),
            row(self.file, choose), self.speakers, row(BodyLabel(tr('Analysis Model')), self.model, None, start),
            self.status, self.preview, row(None, discard, keep)))
        self.drafts = []
        house_settings.subscribe_local_models(lambda _enabled: QTimer.singleShot(0, self.update_model_choices))

    def update_model_choices(self):
        from model_downloader import MODEL_CATALOG, is_installed
        current_data = self.model.currentData()
        self.model.blockSignals(True)
        self.model.clear()
        self.model.addItem(tr('Statistical Analysis (No Model Required)'), userData=False)

        installed_info = []
        for tier in ('large', 'base', 'small'):
            if is_installed('llm', tier):
                cfg = MODEL_CATALOG.get('llm', {}).get(tier, {})
                tier_short = cfg.get('tier_name', tier).split()[0]
                installed_info.append(tier_short)
        if installed_info:
            state = '全局已开启' if house_settings.local_models_enabled() else '全局已关闭'
            self.model.addItem(f"本地模型（{state}；已安装：{'/'.join(installed_info)}）", userData=True)
        else:
            self.model.addItem(tr('Local LLM (Requires mounting in Engine page)'), userData=True)

        self.model.addItem(tr('Cloud Model (Follow Current Friend)'), userData='cloud')

        idx = 0
        for i in range(self.model.count()):
            if self.model.itemData(i) == current_data:
                idx = i
                break
        self.model.setCurrentIndex(idx)
        self.model.blockSignals(False)

    def load(self):
        self.update_model_choices()
        self.refresh()

    def shown(self):
        self.update_model_choices()
        self.refresh()


    def refresh(self):
        from persona_template import library_size, list_templates
        templates = list_templates()
        for item in self.rows:
            item.deleteLater()
        self.rows = []
        if not templates:
            self.rows.append(hint('No templates yet: import a template package or generate from chat history below.'))
            self.mine.add(self.rows[-1])
        for template in templates:
            source, size = template['source'], library_size(template['id'])
            line = QWidget()
            layout = QHBoxLayout(line)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(BodyLabel(f"{template['name']}  ·  {source['speaker']}  ·  "
                                       f"{'使用中' if template['status'] == 'active' else '草稿'}  ·  "
                                       f"{f'向量库：{size} 条' if size else '暂无向量库（仅示例）'}"))
            layout.addStretch(1)
            export, delete = PushButton(tr('Export Package')), PushButton(tr('Delete'))
            export.clicked.connect(lambda _=False, t=template: self.export_package(t))
            delete.clicked.connect(lambda _=False, t=template: self.delete(t))
            layout.addWidget(export)
            layout.addWidget(delete)
            self.mine.add(line)
            self.rows.append(line)
        self.house.mark_stale('stylePage')  # its template list follows the library

    def delete(self, template):
        from persona_template import delete
        if not confirm(self.house, tr('Delete Template'), f"确定删除“{template['name']}”及其向量库？"
                       '正在使用它的好友将停止使用此模板，建议先导出备份。', yes='Delete'):
            return
        delete(template['id'])
        self.refresh()
        toast(self.house, 'success', 'Template deleted.')

    def export_package(self, template):
        from persona_template import export_package
        path, _ = QFileDialog.getSaveFileName(self.house, tr('Export Template Package'), f"{template['name']}.zip", 'Persona Template Package (*.zip)')
        if not path:
            return
        try:
            export_package(template['id'], path)
            toast(self.house, 'success', f'Exported: {path}')
        except Exception as exc:
            toast(self.house, 'error', str(exc), 'Export Failed')

    def import_package(self):
        from persona_template import import_package
        path, _ = QFileDialog.getOpenFileName(self.house, tr('Import Template Package'), '', 'Persona Template Package (*.zip)')
        if not path:
            return
        try:
            template = import_package(path)
        except Exception as exc:
            toast(self.house, 'error', str(exc), 'Import Failed')
            return
        self.refresh()
        toast(self.house, 'success', f"已导入：{template['name']}。请在列表中选中，然后点击“应用到当前好友”。")

    def choose_file(self):
        from persona_template import load_export, speakers
        path, _ = QFileDialog.getOpenFileName(self.house, 'Select Chat Export File', '', 'QQ Chat Export JSON (*.json)')
        if not path:
            return
        try:
            _, messages = load_export(path)
        except Exception as exc:
            toast(self.house, 'error', str(exc), 'Read Failed')
            return
        self.file.setText(path)
        for box in self.speaker_boxes.values():
            box.deleteLater()
        self.speaker_boxes = {}
        for speaker in speakers(messages):
            box = CheckBox(f"{speaker['name'] or '(No Nickname)'}  {speaker['uin']}  {speaker['count']} messages")
            box.setChecked(True)
            self.speakers.addWidget(box)
            self.speaker_boxes[speaker['uin']] = box

    def start_build(self):
        from persona_template import build_template
        path = self.file.text()
        chosen = [uin for uin, box in self.speaker_boxes.items() if box.isChecked()]
        if not path or not chosen:
            toast(self.house, 'warning', 'Please select export file and check speakers to learn from.')
            return
        model_data = self.model.currentData()
        if model_data is not None:
            model = model_data
        else:
            model = ('cloud' if self.model.currentText().startswith('Cloud') else
                     False if self.model.currentText().startswith('Statistical') else True)
        directory = self.house.friend_dir


        def done(templates):
            self.drafts = templates
            self.show_preview(templates)
            self.status.setText(f'已生成 {len(templates)} 个草稿，保存后即可应用到好友。')
        self.status.setText('Generating...')
        run_async(lambda progress: [build_template(path, uin, model, directory, progress=progress) for uin in chosen],
                  done, lambda exc: self.status.setText('Failed: ' + str(exc)), self.status.setText, pool=LONG_POOL)

    def show_preview(self, templates):
        from persona_template import style_text
        lines = []
        for template in templates:
            source = template['source']
            lines += [f"■ {template['name']} ({source['speaker']}, {source['messages']} 条消息，"
                      f"{source['pairs']} 组问答，分析模型：{template['model']})", 'Summary: ' + template['summary'], 'Traits:']
            lines += [f"  · {item['trait']} (Quote: {' / '.join(item['evidence'])})" for item in template['traits']]
            lines += ['Speaking Habits: ' + style_text(template['style']), f"回复示例：{len(template['examples'])}", '']
        self.preview.setPlainText('\n'.join(lines))

    def confirm_drafts(self):
        from persona_template import confirm as confirm_template
        for template in self.drafts:
            confirm_template(template['id'])
        if self.drafts:
            toast(self.house, 'success', f'已保存 {len(self.drafts)} 个模板，可应用到好友。')
        self.drafts = []
        self.refresh()

    def discard_drafts(self):
        from persona_template import delete
        for template in self.drafts:
            delete(template['id'])
        self.drafts = []
        self.show_preview([])
        self.status.setText(tr('Drafts discarded.'))


class DeleteStickerDialog(MessageBoxBase):
    def __init__(self, parent):
        super().__init__(parent)
        self.viewLayout.addWidget(SubtitleLabel(tr('Remove from Favorites')))
        self.viewLayout.addWidget(BodyLabel(tr('Remove this image from sticker collection (meaning card will be deleted and no longer suggested in replies).')))
        self.qq = CheckBox(tr('Also remove from bot QQ favorites'))
        self.qq.setChecked(True)
        self.viewLayout.addWidget(self.qq)
        self.yesButton.setText(tr('Delete'))
        self.cancelButton.setText(tr('Cancel'))


class StickerPage(Page):
    def __init__(self, house):
        super().__init__(house, 'stickerPage')
        fill = PrimaryPushButton(tr('补全缺失的表情释义'))
        fill.clicked.connect(lambda: self.fill_cards())
        self.frequency = ComboBox()
        for label, value in (('关闭（0%）', 0), ('很少（10%）', 10), ('偶尔（30%）', 30),
                             ('经常（60%）', 60), ('每次都可以（100%）', 100)):
            self.frequency.addItem(label, userData=value)
        save_frequency = PrimaryPushButton(tr('Save Frequency'))
        save_frequency.clicked.connect(self.save_frequency)
        self.frequency_state = hint('模型先判断：确定 / 不确定 / 否。确定=使用，否=不用；只有“不确定”才按这里的概率决定。')
        self.add(Card('表情包使用频率', actions=(save_frequency,)).add(
            row(BodyLabel('模型不确定时使用表情的概率'), self.frequency, None), self.frequency_state))
        self.status = body()
        self.add(Card('Sticker Collection', actions=(fill,)).add(
            hint('Each sticker has a meaning card (text, meaning, emotion, use cases). When replying, relevant stickers are retrieved for the model to choose from. '
                 'Newly saved stickers get draft cards from the configured vision model; cards you edit and save will not be overwritten.'), self.status))
        self.cards, self.signature = [], None

    def load(self):
        from sticker_library import usage_frequency
        profile = load_profile(self.house.profile_path) if self.house.current_qq else {}
        value = usage_frequency(profile)
        index = self.frequency.findData(value)
        self.frequency.setCurrentIndex(index if index >= 0 else self.frequency.count() - 1)
        self.frequency_state.setText(f'当前：{self.frequency.currentText()}。模型会先按聊天语境和表情释义判断“确定 / 不确定 / 否”；只有不确定时才按这个概率。')
        self.refresh()

    def shown(self):
        self.refresh()

    def save_frequency(self):
        try:
            profile = load_profile(self.house.profile_path)
            profile['sticker_frequency'] = int(self.frequency.currentData() or 0)
            from house_status import write_profile
            write_profile(self.house.profile_path, profile)
            self.frequency_state.setText(f'已保存：{self.frequency.currentText()}。')
            toast(self.house, 'success', '表情包使用频率已保存；下一轮回复生效。')
        except Exception as exc:
            toast(self.house, 'error', str(exc), '保存失败')

    def refresh(self, force=False):
        from sticker_library import CARD_FIELDS, stickers
        house = self.house
        store = FriendStore(house.friend_dir)
        try:
            items = stickers(store)
        finally:
            store.close()
        signature = (house.current_qq, json.dumps(items, ensure_ascii=False, sort_keys=True))
        if not force and signature == self.signature:
            return
        self.signature = signature
        for card in self.cards:
            card.deleteLater()
        self.cards = []
        self.status.setText(f"共 {len(items)} 张表情，其中 {sum(1 for _, state in items if state.get('card'))} 张已有释义。")
        for key, state in items:
            card = state.get('card') or {}
            box = Card()
            line = QHBoxLayout()
            picture = QLabel()
            pixmap = QPixmap(str(house.friend_dir / 'media' / (key + '.preview.png')))
            if pixmap.isNull():
                picture.setText(tr('(Preview missing)'))
            else:
                picture.setPixmap(pixmap.scaled(150, 150, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            picture.setFixedWidth(160)
            picture.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
            line.addWidget(picture)
            fields = QVBoxLayout()
            labels = {'user': '手动编辑', 'codex': 'Codex 草稿', 'claude': 'Claude 草稿', 'antigravity': 'Antigravity 草稿', 'custom': '自定义模型草稿', 'deepseek': 'DeepSeek 草稿'}
            source = labels.get(card.get('by'), (str(card.get('by') or '模型') + ' 草稿') if card.get('by') else '尚未补全释义')
            qq_state = 'Saved in QQ Favorites' if state.get('qq_saved') else 'QQ Favorites unconfirmed'
            extra = '  ·  模型判断为普通图片，不推荐在回复中使用（保存修改后会恢复推荐）' if card.get('is_meme') is False else ''
            source_label = CaptionLabel(f'{source}  ·  {qq_state}{extra}')
            source_label.setWordWrap(True)
            fields.addWidget(source_label)
            edits = {}
            for name, title in CARD_FIELDS.items():
                edits[name] = LineEdit()
                edits[name].setText(card.get(name, ''))
                title_label = BodyLabel(title)
                title_label.setFixedWidth(110)
                fields.addLayout(row(title_label, edits[name]))
            note = CaptionLabel('')
            note.setTextColor(GREEN, GREEN)
            delete, rewrite, save = PushButton(tr('Delete Sticker')), PushButton('用当前模型重写'), PrimaryPushButton(tr('Save Changes'))
            save.clicked.connect(lambda _=False, k=key, e=edits, s=source_label, q=qq_state, n=note:
                                 self.save_card(k, e, s, q, n))
            rewrite.clicked.connect(lambda _=False, k=key: self.fill_cards([k]))
            delete.clicked.connect(lambda _=False, k=key, b=box, n=note: self.delete(k, b, n))
            fields.addLayout(row(note, None, delete, rewrite, save))
            line.addLayout(fields, 1)
            box.add(line)
            self.cards.append(self.add(box))

    def save_card(self, key, edits, source_label, qq_state, note):
        from sticker_library import save_card
        store = FriendStore(self.house.friend_dir)
        try:
            save_card(store, key, {name: edit.text() for name, edit in edits.items()}, 'user')
        except Exception as exc:
            toast(self.house, 'error', str(exc), 'Save Failed')
            return
        finally:
            store.close()
        source_label.setText(f'手动编辑 · {qq_state}')
        note.setText(f'✓ 已保存（{datetime.now():%H:%M:%S}），不会被自动覆盖')
        toast(self.house, 'success', 'Meaning card saved.')

    def delete(self, key, box, note):
        dialog = DeleteStickerDialog(self.house)
        if not dialog.exec():
            return
        from sticker_library import remove
        directory, with_qq = self.house.friend_dir, dialog.qq.isChecked()

        def job(_):
            from qq_link import QQClient
            client = QQClient() if with_qq else None
            store = FriendStore(directory)
            try:
                remove(store, directory, key, client)
            finally:
                store.close()
                if client is not None:
                    client.close()

        def done(_):
            self.cards.remove(box)
            box.deleteLater()
            self.status.setText(f'共 {len(self.cards)} stickers.')
            toast(self.house, 'success', 'Deleted 1 sticker' + (' (also removed from QQ favorites)' if with_qq else '') + '.')
        note.setText('Deleting...')
        run_async(job, done, lambda exc: (note.setText(''), toast(self.house, 'error', str(exc), 'Deletion Failed')), pool=LONG_POOL)

    def fill_cards(self, keys=None):
        from sticker_library import stickers, write_card_with_model
        directory, profile = self.house.friend_dir, load_profile(self.house.profile_path)

        def job(progress):
            store = FriendStore(directory)
            try:
                todo = keys or [key for key, state in stickers(store) if not state.get('card')]
                for index, key in enumerate(todo, 1):
                    progress(f'当前视觉模型正在分析表情 {index}/{len(todo)}...')
                    if keys:  # an explicit rewrite replaces a user card too
                        state = json.loads(store.get('sticker:' + key) or '{}')
                        state.pop('card', None)
                        store.set('sticker:' + key, json.dumps(state, ensure_ascii=False))
                    write_card_with_model(store, directory, key, profile)
                return len(todo)
            finally:
                store.close()

        def done(count):
            self.refresh(force=True)
            toast(self.house, 'success', f'已生成 {count} 张释义草稿，可编辑后保存。' if count else '没有缺少释义的表情。')
        run_async(job, done, lambda exc: toast(self.house, 'error', str(exc), 'Failed to Generate Meaning Cards'), self.status.setText,
                  pool=LONG_POOL)


class ContextPage(Page):
    """2026-10-10 user: what the model was given last time, block by block in the fixed order
    (prompt_context.SLOTS; like SillyTavern's per-prompt token counts and OpenClaw's context list)."""
    PLACES = {'intro': tr('0 Intro'), 'rules': tr('1 Rules'), 'persona': tr('2 Persona & Style'), 'counterpart': tr('3 Contact Profile'),
              'recent': tr('4 Recent Status'), 'facts': tr('5 Retrieved Knowledge'), 'examples': tr('6 Example Dialogues'),
              'chat': tr('7 Summary & Chat'), 'days': tr('7 History & Chat'), '': 'General', 'notes': tr('8 Author Notes'), 'task': tr('9 Current Task')}

    def __init__(self, house):
        super().__init__(house, 'contextPage')
        self.summary = body()
        refresh, full, sent = PushButton(tr('Refresh')), PushButton(tr('View Full Input')), PushButton(tr('View Actual Prompt'))
        refresh.clicked.connect(self.load)
        full.clicked.connect(lambda: self.show_text('full', 'Full Input'))
        sent.clicked.connect(lambda: self.show_text('prompt', 'Actual Prompt'))
        self.missing = hint('')
        self.conflicts = hint('')
        self.add(Card('Latest Generation Input').add(
            hint('Replies, proactive chats, scheduled care, and proactive triggers are composed in a fixed sequence. '
                 'Within the same session, unchanged prefix blocks are cached, so "Actual Prompt" is usually shorter than "Full Input".'),
            self.summary, self.missing, self.conflicts, row(refresh, full, sent, None)))
        self.table = TableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels([tr('Slot'), tr('Block'), tr('Source'), tr('Type'), tr('Chars')])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        for column, width in ((0, 150), (1, 170), (3, 80), (4, 70)):
            self.table.setColumnWidth(column, width)
        self.table.setMinimumHeight(330)
        self.table.itemSelectionChanged.connect(self.show_block)
        self.block = PlainTextEdit()
        self.block.setReadOnly(True)
        self.block.setMinimumHeight(260)
        self.add(Card('Block Content').add(hint('Select a row to view the raw text of that block.'), self.table, self.block))
        # 2026-10-10 Phase 4: every generation is kept (generation_log), so earlier inputs and outputs can be compared
        self.history = ComboBox()
        self.history.setMinimumWidth(460)
        open_, output, hits, diff = (PushButton(tr('View This Run')), PushButton(tr('Model Output')), PushButton(tr('Retrieval Hits')),
                                     PushButton(tr('Compare with Previous')))
        open_.clicked.connect(self.open_record)
        output.clicked.connect(lambda: self.show_record('output'))
        hits.clicked.connect(lambda: self.show_record('hits'))
        diff.clicked.connect(self.compare_record)
        self.add(Card('Generation History').add(
            hint('Each generation records: input blocks with fingerprints, prompt versions, actual prompt, retrieval hits and scores, '
                 'model output, duration, and delivered message count. Keeps the latest 300 runs.'),
            row(self.history, None), row(open_, output, hits, diff, None)))
        self.data, self.rows, self.records = {}, [], []

    def load(self):
        qq_id = self.house.current_qq

        def read(_):
            import generation_log
            store = FriendStore(self.house.friend_dir)
            try:
                return json.loads(store.get('last_generation_input') or '{}'), generation_log.recent(store, 100)
            finally:
                store.close()

        def show(result):
            if self.house.current_qq == qq_id:
                data, self.records = result
                self.fill(data)
                self.history.clear()
                self.history.addItems([
                    f"#{r['id']}  {r['time'][5:16].replace('T', ' ')}  {tr(r['mode'])}  {r['chars']} 字"
                    + (f"  {r['ms'] / 1000:.1f}秒" if r['ms'] else '') + f"  已投递 {r['delivered']}"
                    + ('  出错' if r['error'] else '') for r in self.records])
        run_async(read, show, lambda exc: toast(self.house, 'error', str(exc), 'Read Failed'))

    def _with_records(self, ids, then):
        """Read full generation records in the background (the UI's database reads share one worker thread)."""
        directory = self.house.friend_dir

        def read(_):
            import generation_log
            store = FriendStore(directory)
            try:
                return [generation_log.get(store, row_id) for row_id in ids]
            finally:
                store.close()
        run_async(read, lambda records: all(records) and then(*records),
                  lambda exc: toast(self.house, 'error', str(exc), 'Read Failed'))

    def _selected(self):
        index = self.history.currentIndex()
        return self.records[index] if 0 <= index < len(self.records) else None

    def open_record(self):
        selected = self._selected()
        if selected:
            self._with_records([selected['id']], lambda record: self.fill({
                'mode': record['mode'], 'time': record['time'], 'full': record['full'],
                'prompt': record['sent'] or record['full'], 'characters': len(record['sent'] or record['full'])}))

    def show_record(self, what):
        selected = self._selected()
        if selected:
            self._with_records([selected['id']], lambda record: self._show_record(record, what))

    def _show_record(self, record, what):
        if what == 'output':
            text = (record['response'] or '') + (('\n\nError: ' + record['error']) if record['error'] else '')
            TextDialog(self.house, tr('Model Output'), text or '本次运行没有记录模型输出。', f"#{record['id']} {tr(record['mode'])}").exec()
            return
        lines = []
        for search in json.loads(record['retrieval'] or '[]'):
            if search.get('tool'):  # the model's own lookup (search_friend_memory, get_today_chat)
                lines.append(f"[{search['label']}] 查询：{search['query']}（{search.get('status') or '状态未知'}）")
            else:
                lines.append(f"[{search['label'] or '检索'}] 查询：{search['query']}，候选数：{search['pool']}"
                             + ('，已重排' if search['reranked'] else '，未重排（重排模型不可用）')
                             + f"，低于阈值已过滤：{search['dropped']}")
            lines += [f"  {'—' if score is None else f'{score:.3f}'}  {text}" for text, score in search['hits']]
        TextDialog(self.house, tr('Retrieval Hits'), '\n'.join(lines) or '本次运行没有使用检索知识。', f"#{record['id']}").exec()

    def compare_record(self):
        import generation_log
        selected = self._selected()
        if not selected:
            return
        index = self.records.index(selected)
        previous = next((r for r in self.records[index + 1:] if r['mode'] == selected['mode']), None)
        if previous is None:
            toast(self.house, 'warning', 'No earlier record of this type to compare.')
            return
        self._with_records([previous['id'], selected['id']], self._show_compare)

    def _show_compare(self, old, new):
        import generation_log
        lines = [f"上次：#{old['id']} {old['time']} 提示词版本 {old['version']}",
                 f"本次：#{new['id']} {new['time']} 提示词版本 {new['version']}", '', '内容块：上次字数 → 本次字数']
        for key, before, after, changed in generation_log.compare(old, new):
            mark = '新增' if before is None else '已移除' if after is None else '有变化' if changed else '未变化'
            lines.append(f"{key}  {before or '-'} -> {after or '-'}  {mark}")
        lines += ['', '-- 上次输出 --', old['response'] or old['error'] or '（无）',
                  '', '-- 本次输出 --', new['response'] or new['error'] or '（无）']
        TextDialog(self.house, 'Generation Comparison', '\n'.join(lines), new['mode']).exec()

    def fill(self, data):
        from prompt_context import SLOTS, composition, prompt_conflicts
        self.data = data
        full = data.get('full') or data.get('prompt', '')
        self.rows = composition(full) if full else []
        warnings = prompt_conflicts(full) if full else []
        self.conflicts.setText('提示词冲突：' + '; '.join(warnings) if warnings else '')
        if not full:
            self.summary.setText(tr('No generation records yet (only shows inputs after this version).'))
        else:
            time = data.get('time', '')[:19].replace('T', ' ')
            self.summary.setText(f"{tr(data.get('mode', ''))} · {time} · 完整输入 {len(full)} 字 · "
                                 f"实际发送 {data.get('characters', len(full))} 字")
        present = {row_['slot'] for row_ in self.rows}
        absent = [self.PLACES.get(slot, slot) for slot in SLOTS if slot not in present and self.PLACES.get(slot, slot)]
        self.missing.setText('本轮未使用：' + ', '.join(absent) if full and absent else '')
        self.table.setRowCount(len(self.rows))
        for index, item in enumerate(self.rows):
            for column, text in enumerate((self.PLACES.get(item['slot'], item['slot']), item['title'], item['source'],
                                           item['kind'], str(item['chars']))):
                self.table.setItem(index, column, QTableWidgetItem(text))
        self.block.setPlainText('')

    def show_block(self):
        selected = self.table.selectedItems()
        if selected:
            self.block.setPlainText(self.rows[selected[0].row()]['text'])

    def show_text(self, key, title):
        text = self.data.get(key) or self.data.get('prompt', '')
        TextDialog(self.house, title, text or '尚无生成记录。', f"{len(text)} 字").exec()
