import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from notification_manager import NotificationManager
from research_tracker import ResearchTracker
from task_manager import TaskManager, TaskStatus
import task_spec


class ResearchAgentWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        friend = self.root / 'friends/10001'
        friend.mkdir(parents=True)
        (friend / 'friend.json').write_text(json.dumps({'model_api': {'provider': 'codex'},
            'computer_tools': {'enabled': False, 'commands': False, 'project_path': str(self.root)}}))
        self.manager = TaskManager(self.root / 'tasks.db', self.root)
        self.tracker = ResearchTracker(self.root / 'data/research_projects.sqlite3')
        self.patches = [patch.object(ResearchTracker, 'get_instance', return_value=self.tracker),
            patch.object(NotificationManager, 'get_instance', return_value=NotificationManager(self.root / 'notices.db'))]
        for item in self.patches:
            item.start()
        self.papers = [{'title': 'Fixture research record', 'link': 'https://example.org/paper',
                        'summary': 'A supplied test fixture; never presented as live retrieval.'}]
        self.plan = {'hypothesis': '待检验的领域问题', 'scope': '仅上述文献摘要', 'experiment_code': '',
                     'report': '文献分析 [1]。' + '这是用于验证真实调用链的测试资料摘要，不能据此声称研究假设成立。' * 4}

    def tearDown(self):
        self.manager.stop_worker()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def run_research(self, papers=None):
        tid = self.manager.create_task('领域研究', '检索文献并提出可核验的问题', category='research',
            owner_id='10001', max_retries=0, initial_checkpoint=task_spec.build_task_spec('领域研究', 'research'))
        outputs = [(json.dumps({'search_query': 'research workflow', 'questions': ['研究问题']}), None),
                   (json.dumps(self.plan), None)]
        with patch('multi_agent_orchestrator.call_single_agent', side_effect=outputs), \
                patch('local_retrieval_engine.AcademicResearchPipeline.search_arxiv_papers', return_value=self.papers if papers is None else papers):
            self.manager._process_single_task(self.manager.get_task(tid))
        return self.manager.get_task(tid)

    def test_retry_keeps_query_and_sources_instead_of_requesting_them_again(self):
        tid=self.manager.create_task('研究重试','研究相同问题',category='research',owner_id='10001',max_retries=0,
                                     initial_checkpoint=task_spec.build_task_spec('研究相同问题','research'))
        answers=[(json.dumps({'search_query':'research workflow','questions':['问题']}),None),
                 RuntimeError('分析阶段暂时失败'),(json.dumps(self.plan),None)]
        with patch('multi_agent_orchestrator.call_single_agent',side_effect=answers) as model, \
                patch('local_retrieval_engine.AcademicResearchPipeline.search_arxiv_papers',return_value=self.papers) as search:
            self.manager._process_single_task(self.manager._fetch_next_executable_task())
            self.assertEqual(self.manager.get_task(tid).status,TaskStatus.FAILED)
            self.assertTrue(self.manager.resume_task(tid,owner_id='10001'))
            self.manager._process_single_task(self.manager._fetch_next_executable_task())
        self.assertEqual(model.call_count,3)
        self.assertEqual(search.call_count,1)
        self.assertEqual(self.manager.get_task(tid).status,TaskStatus.WAITING_APPROVAL)

    def test_general_research_delivers_cited_report_without_claiming_breakthrough(self):
        task = self.run_research()
        self.assertEqual(task.status, TaskStatus.WAITING_APPROVAL, task.error)
        self.assertEqual(len(task.artifacts), 2)
        self.assertIn('https://example.org/paper', Path(task.artifacts[0]).read_text('utf-8'))
        self.assertFalse(task.verification_info['success'])
        project = self.tracker.get_project(task.checkpoint['research_project_id'])
        self.assertEqual(project.owner_id, '10001')
        self.assertFalse(self.tracker.list_experiments(project.project_id))

    def test_no_retrieved_papers_fails_without_inventing_sources(self):
        task = self.run_research(papers=[])
        self.assertEqual(task.status, TaskStatus.FAILED)
        self.assertIn('retrieval_failed', task.error)
        self.assertFalse(task.artifacts)

    def test_fabricated_citation_is_rejected(self):
        self.plan['report'] += ' https://invalid.example/fabricated-paper'
        task = self.run_research()
        self.assertEqual(task.status, TaskStatus.FAILED)
        self.assertFalse(task.artifacts)


class ArxivQueryTests(unittest.TestCase):
    def test_every_term_is_required_and_ranked_by_relevance(self):
        # Live run (gpt-6-luna): "all:python 3.14 new features" ORed the words and, sorted by date, returned
        # the week's unrelated papers (solitons, dark matter) as sources for a Python question.
        from io import BytesIO
        from local_retrieval_engine import AcademicResearchPipeline
        seen = []
        feed = (b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Linear attention</title>'
                b'<summary>s</summary><id>http://arxiv.org/abs/1</id><published>2020</published></entry></feed>')

        def urlopen(request, timeout):
            seen.append(request.full_url)
            return BytesIO(feed)

        with patch('urllib.request.urlopen', side_effect=urlopen):
            papers = AcademicResearchPipeline(engine=None).search_arxiv_papers('linear attention "transformer"')
        self.assertEqual(papers[0]['title'], 'Linear attention')
        self.assertIn('search_query=all:linear+AND+all:attention+AND+all:transformer&', seen[0])
        self.assertIn('sortBy=relevance', seen[0])
