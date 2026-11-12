import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


@unittest.skipUnless(importlib.util.find_spec('PySide6') and importlib.util.find_spec('qfluentwidgets'), 'PySide6/Fluent UI dependencies not installed')
class ReleaseUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtGui import QFont, QFontDatabase
        from PySide6.QtWidgets import QApplication
        from qfluentwidgets import Theme, setTheme, setThemeColor
        from house_qt.common import ACCENT, init_ui_thread
        cls.app = QApplication.instance() or QApplication([])
        # The offscreen Windows platform has no system font enumeration.
        for name in ('msyh.ttc', 'segoeui.ttf'):
            font = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Fonts' / name
            if font.is_file():
                QFontDatabase.addApplicationFont(str(font))
        cls.app.setFont(QFont('Microsoft YaHei'))
        setTheme(Theme.LIGHT)
        setThemeColor(ACCENT)
        init_ui_thread()

    def setUp(self):
        from PySide6.QtWidgets import QMainWindow
        from house_qt.pages_friend import ModelPage
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.profile = self.directory / 'friend.json'
        self.profile.write_text(json.dumps({'model_api': {'provider': 'codex'}}), encoding='utf-8')
        self.house = QMainWindow()
        self.house.friend_dir = self.directory
        self.house.profile_path = self.profile
        self.house.current_qq = '555000001'
        self.house.records = {}
        self.patch_root = patch('house_qt.pages_friend.ROOT', self.directory)
        self.patch_root.start()
        self.toast = patch('house_qt.pages_friend.toast').start()
        self.page = ModelPage(self.house)
        self.house.setCentralWidget(self.page)
        self.house.resize(1160, 1000)
        self.page.load()
        self.house.show()
        self.app.processEvents()

    def tearDown(self):
        self.house.close()
        self.app.processEvents()
        patch.stopall()
        self.temp.cleanup()

    def test_each_cli_and_each_api_section_switches_independently(self):
        page = self.page
        page.switch_model_section('sources')
        for button, card in ((page.codex, page.codex_card), (page.claude, page.claude_card),
                             (page.antigravity, page.ag_card)):
            button.setChecked(True)
            for source in (page.dsh, page.codex, page.claude, page.antigravity, page.deepseek):
                if source is not button:
                    source.setChecked(False)
            self.assertFalse(card.isHidden())
            self.assertTrue(page.api_nav.isHidden())
        page.deepseek.setChecked(True)
        page.switch_model_section('api')
        for source in (page.dsh, page.codex, page.claude, page.antigravity):
            source.setChecked(False)
        for button, card in ((page.api_model_section, page.chat_api_card),
                             (page.api_retrieval_section, page.service_apis.retrieval),
                             (page.api_function_section, page.service_apis.function)):
            button.setChecked(True)
            self.assertFalse(card.isHidden())
            self.assertTrue(page.codex_card.isHidden())

    def test_multiple_model_apis_and_separate_retrieval_and_function_keys(self):
        import api_backend
        page = self.page
        page.deepseek.setChecked(True)
        page.base_url.setText('https://example.invalid/v1')
        page.deepseek_model.setText('synthetic-model-one')
        page.key.setText('synthetic-ui-one')
        page.save()
        page.api_profiles['second'] = {'label': 'Second API', 'api_key_id': 'second'}
        page.profile_ids.append('second')
        page.api_profile.addItem('Second API')
        page.api_profile.setCurrentIndex(1)
        page.base_url.setText('https://second.invalid/v1')
        page.deepseek_model.setText('synthetic-model-two')
        page.key.setText('synthetic-ui-two')
        page.save()
        self.assertEqual(api_backend.load_key(self.directory, 'chat'), 'synthetic-ui-one')
        self.assertEqual(api_backend.load_key(self.directory, 'second'), 'synthetic-ui-two')
        page.api_retrieval_section.setChecked(True)
        enabled, url, model, dimension, key = page.service_apis.endpoints['embedding']
        enabled.setChecked(True)
        url.setText('https://retrieval.invalid/v1')
        model.setText('synthetic-vector')
        dimension.setText('2')
        key.setText('synthetic-ui-vector')
        page.save()
        self.assertEqual(api_backend.load_key(self.directory, 'embedding'), 'synthetic-ui-vector')
        page.api_function_section.setChecked(True)
        page.service_apis.enabled.setChecked(True)
        page.service_apis.name.setText('api_weather')
        page.service_apis.url.setText('https://example.invalid/weather')
        page.service_apis.key.setText('synthetic-ui-function')
        page.save()
        saved = json.loads(self.profile.read_text('utf-8'))
        self.assertEqual(saved['model_api']['model'], 'synthetic-model-two')
        self.assertEqual(len(saved['api_profiles']), 2)
        self.assertEqual(saved['function_apis'][0]['name'], 'api_weather')
        self.assertEqual(api_backend.load_key(self.directory, 'function-api_weather'), 'synthetic-ui-function')

    def test_small_model_presets_keep_dimensions_pooling_and_e5_prefixes(self):
        controls = self.page.service_apis
        controls.local_options['embedding']['preset'].setCurrentText('intfloat/multilingual-e5-small')
        self.assertEqual(controls.endpoints['embedding'][3].text(), '384')
        self.assertEqual(controls.local_options['embedding']['pooling'].currentData(), 'mean')
        self.assertIn('聚合', controls.local_options['embedding']['pooling'].currentText())
        self.assertEqual(controls.local_options['embedding']['query_prefix'].text(), 'query: ')
        self.assertEqual(controls.local_options['embedding']['document_prefix'].text(), 'passage: ')

    def test_per_friend_computer_switch_persists(self):
        self.page.computer_enabled.setChecked(True)
        self.page.computer_commands.setChecked(False)
        self.page.save_computer_tools()
        self.assertEqual(json.loads(self.profile.read_text('utf-8'))['computer_tools'],
                         {'enabled': True, 'commands': False, 'shell': 'pwsh'})

    def test_local_hardware_selectors_include_mps_and_auto_default(self):
        self.assertIn('mps',[self.page.local_device.itemData(i) for i in range(self.page.local_device.count())])
        for kind in ('embedding','rerank'):
            selector=self.page.service_apis.local_options[kind]['device']
            self.assertEqual([selector.itemData(i) for i in range(selector.count())],['auto','cuda','mps','cpu'])
            self.assertEqual(selector.itemText(0), '自动选择')

    def test_manual_device_applies_and_preserves_gguf_tuning(self):
        import model_downloader
        import house_settings
        runtime=self.directory/'build/message-classifier-runtime/runtime.json'
        runtime.parent.mkdir(parents=True)
        previous={'server':'bin/custom-server','cpu_server':'cpu/custom-server','ctx':8192,'slots':2,
                  'server_args':['-fa','on'],'model':'previous.gguf'}
        runtime.write_text(json.dumps(previous),'utf-8')
        self.page.active_llm.clear()
        self.page.active_llm.addItem('synthetic local model',userData='small')
        with patch.object(model_downloader,'project_root',return_value=self.directory):
            for device in ('cpu','cuda','mps'):
                for i in range(self.page.local_device.count()):
                    if self.page.local_device.itemData(i)==device:
                        self.page.local_device.setCurrentIndex(i)
                self.page.apply_active_models()
                saved=json.loads(runtime.read_text('utf-8'))
                self.assertEqual(saved['device'],device)
                self.assertEqual(house_settings.load(self.directory)['inference_device'],device)
                self.assertEqual(json.loads(self.profile.read_text('utf-8'))['local_model_device'],device)
                for key in ('server','cpu_server','ctx','slots','server_args'):
                    self.assertEqual(saved[key],previous[key])


if __name__ == '__main__':
    unittest.main()
