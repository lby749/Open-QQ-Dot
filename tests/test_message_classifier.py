import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import message_classifier as mc

LABEL = {"intent": "share", "emotion": "happy", "tone": "casual", "initiative": "proactive"}

DETAIL = {"labels": LABEL, "needs_review": False, "reason": "clear"}


class ClassifierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        directory = self.root / "build/message-classifier-runtime"
        directory.mkdir(parents=True)
        (directory / "runtime.json").write_text(json.dumps({"server": "bin/server.exe", "model": "model.gguf"}), "utf-8")
        (directory / 'model.gguf').write_bytes(b'synthetic GGUF test fixture')
        self.probe = patch.object(mc.subprocess, 'run', return_value=Mock(stdout=b'CUDA0: synthetic', returncode=0))
        self.probe.start()
        self.addCleanup(self.probe.stop)
        self.classifier = mc.LocalMessageClassifier(self.root)

    def tearDown(self):
        self.classifier.close()
        singleton = mc._instances.pop(self.root.resolve(), None)
        if singleton is not None:
            singleton.close()
        self.temp.cleanup()

    def test_schema_and_enum_strict(self):
        self.assertEqual(mc._validate(LABEL), LABEL)
        for invalid in [dict(LABEL, extra="x"), dict(LABEL, intent="未知"), dict(LABEL, emotion=1), []]:
            with self.assertRaises(mc.ClassifierUnavailable):
                mc._validate(invalid)
        self.assertEqual(mc._validate_detail(DETAIL), DETAIL)
        uncertain = dict(DETAIL, needs_review=True, reason="tone_ambiguity")
        self.assertEqual(mc._validate_detail(uncertain), uncertain)
        for invalid in [dict(DETAIL, extra="x"), dict(DETAIL, needs_review=1),
                        dict(DETAIL, reason="自信90%"), dict(DETAIL, needs_review=True),
                        dict(DETAIL, reason="uncertain"), dict(DETAIL, labels=dict(LABEL, tone="未知"))]:
            with self.assertRaises(mc.ClassifierUnavailable):
                mc._validate_detail(invalid)
        self.assertFalse(mc.DETAIL_SCHEMA["additionalProperties"])
        self.assertEqual(mc.DETAIL_SCHEMA["properties"]["labels"], mc.SCHEMA)
        self.assertFalse(mc.SCHEMA["additionalProperties"])
        self.assertEqual(set(mc.SCHEMA["required"]), set(LABEL))

    def test_timing_file_without_root_logging_configuration(self):
        process = Mock()
        process.poll.return_value = None
        reply = {"choices": [{"message": {"content": json.dumps(DETAIL)}}]}
        with patch.object(mc.subprocess, "Popen", return_value=process), patch.object(
                self.classifier, "_request", side_effect=lambda path, *args, **kwargs: {"status": "ok"} if path == "/health" else reply):
            self.classifier.classify("synthetic secret message one")
            self.classifier.classify_batch(["synthetic secret message two"])
        log = (self.root / "build/message-classifier-runtime/timing.log").read_text("utf-8")
        self.assertEqual(log.count("load_seconds="), 1)
        self.assertEqual(log.count("single_seconds="), 2)
        self.assertEqual(log.count("batch_seconds="), 1)
        self.assertNotIn("secret", log)
        self.assertEqual(len(self.classifier._timing_logger.handlers), 1)

    def test_generation_roles_following_and_current_target(self):
        observed = []

        def request(path, payload):
            observed.append(payload)
            return {"choices": [{"message": {"content": json.dumps(DETAIL)}}]}

        history = [{"text": str(i) * 200, "direction": "in" if i % 2 else "out", "seq": i} for i in range(4)]
        after = [{"text": "following", "direction": "in", "seq": 5}, {"text": "extra following", "seq": 6}]
        with patch.object(self.classifier, "_ensure_loaded"), patch.object(self.classifier, "_request", side_effect=request):
            self.classifier.classify_batch(["current target"], directions=["out"], contexts=[history], following_contexts=[after])
            self.classifier.classify("compatible legacy input", context=["old previous"])
        payload = observed[0]
        body = json.loads(payload["messages"][1]["content"])
        self.assertEqual([entry["seq"] for entry in body["previous_context"]], [1, 2, 3])
        self.assertEqual(body["previous_context"][0]["direction"], "in")
        self.assertEqual(len(body["previous_context"][0]["text"]), 160)
        self.assertEqual(body["target_message"], {"text": "current target", "direction": "out"})
        self.assertEqual(body["following_context"], [after[0]])
        self.assertEqual(json.loads(observed[1]["messages"][1]["content"])["following_context"], [])
        for key, expected in mc.GENERATION.items():
            self.assertEqual(payload[key], expected)
        with self.assertRaises(ValueError):
            self.classifier.classify_batch(["target"], following_contexts=[])

    def test_review_apis_preserve_labels_and_single_resident_inference(self):
        process = Mock()
        process.poll.return_value = None
        uncertain = dict(DETAIL, needs_review=True, reason="very_short_message")
        requests = []

        def request(path, payload=None, **kwargs):
            if path == "/health":
                return {"status": "ok"}
            requests.append(payload)
            body = json.loads(payload["messages"][1]["content"])
            value = uncertain if body["target_message"]["text"] == "yeah" else DETAIL
            return {"choices": [{"message": {"content": json.dumps(value)}}]}

        with patch.object(mc.subprocess, "Popen", return_value=process) as launch, patch.object(
                self.classifier, "_request", side_effect=request):
            self.assertEqual(self.classifier.classify_with_review("yeah"), uncertain)
            self.assertEqual(self.classifier.classify_with_review("I am happy today!"), DETAIL)
            self.assertEqual(self.classifier.classify("yeah"), LABEL)
            self.assertEqual(self.classifier.classify_batch(["yeah", "I am happy today!"]), [LABEL, LABEL])
            self.assertEqual(self.classifier.classify_batch_with_review(["yeah", "I am happy today!"]), [uncertain, DETAIL])
            self.assertEqual(launch.call_count, 1)
            self.assertEqual(self.classifier.load_count, 1)
            self.assertEqual(len(requests), 7)
            self.assertTrue(all(p["response_format"]["json_schema"]["schema"] == mc.DETAIL_SCHEMA for p in requests))

    def test_short_without_history_review_rule_preserves_labels(self):
        reply = {"choices": [{"message": {"content": json.dumps(DETAIL)}}]}
        with patch.object(self.classifier, "_ensure_loaded"), patch.object(
                self.classifier, "_request", return_value=reply) as request:
            short = self.classifier.classify_with_review(" yeah ")
            contextual = self.classifier.classify_with_review("yeah", context=["Did you get notified?"])
            clear = self.classifier.classify_with_review("I passed my exam today, so happy!")
        self.assertEqual(short, dict(DETAIL, needs_review=True, reason="very_short_message"))
        self.assertEqual(short["labels"], LABEL)
        self.assertEqual(contextual, DETAIL)
        self.assertEqual(clear, DETAIL)
        self.assertEqual(request.call_count, 3)

    def test_cuda_unavailable_selects_explicit_cpu_once(self):
        self.classifier.runtime["cpu_server"] = "bin-cpu/server.exe"
        process = Mock()
        process.poll.return_value = None
        with patch.object(mc.subprocess, "run", return_value=Mock(stdout=b"Available devices: (none)", returncode=0)) as probe, patch.object(mc.subprocess, "Popen", return_value=process) as launch, patch.object(self.classifier, "_request", return_value={"status": "ok"}):
            self.classifier._ensure_loaded()
            self.classifier._ensure_loaded()
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(self.classifier.actual_device, "cpu")
        args = launch.call_args.args[0]
        self.assertEqual(args[args.index("-ngl") + 1], "0")
        self.assertEqual(args[args.index("--threads") + 1], "4")
        self.assertIn("bin-cpu", args[0])
        log = (self.root / "build/message-classifier-runtime/timing.log").read_text("utf-8")
        self.assertIn("actual_device=cpu fallback_reason=cuda_metal_unavailable", log)

    def test_singleton_and_load_once(self):
        self.assertIs(mc.get_classifier(self.root), mc.get_classifier(self.root))
        process = Mock()
        process.poll.return_value = None
        with patch.object(mc.subprocess, "Popen", return_value=process) as launch, patch.object(self.classifier, "_request", return_value={"status": "ok"}):
            self.classifier._ensure_loaded()
            self.classifier._ensure_loaded()
            self.assertEqual(launch.call_count, 1)
            self.assertEqual(self.classifier.load_count, 1)
            args = launch.call_args.args[0]
            self.assertEqual(args[args.index("-m") + 1], "model.gguf")
            self.assertIn("--cont-batching", args)
        self.classifier.close()
        process.terminate.assert_called_once()
        with self.assertRaises(mc.ClassifierUnavailable):
            self.classifier._ensure_loaded()

    def test_batch_order_and_bounds_and_timing(self):
        with patch.object(self.classifier, "_ensure_loaded"), patch.object(self.classifier, "classify_with_review", side_effect=lambda text, **kwargs: {"labels": text}), self.assertLogs(mc.LOG, level="INFO") as logs:
            self.assertEqual(self.classifier.classify_batch(["one", "two", "three"]), ["one", "two", "three"])
        self.assertIn("batch_seconds=", logs.output[0])
        with self.assertRaises(ValueError):
            self.classifier.classify_batch(["one"], directions=[])
        with self.assertRaises(ValueError):
            self.classifier.classify_batch(["one"] * 65)

    def test_failure_never_returns_default_or_restarts(self):
        process = Mock()
        process.poll.return_value = 1
        with patch.object(mc.subprocess, "Popen", return_value=process) as launch:
            for _ in range(2):
                with self.assertRaises(mc.ClassifierUnavailable):
                    self.classifier._ensure_loaded()
            self.assertEqual(launch.call_count, 1)
        with patch.object(self.classifier, "_ensure_loaded"), patch.object(self.classifier, "_request", return_value={"choices": [{"message": {"content": "bad"}}]}):
            with self.assertRaises(mc.ClassifierUnavailable):
                self.classifier.classify("synthetic test")

    def test_real_local_http_ignores_proxy_and_constrains_json(self):
        observed = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                observed.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                data = json.dumps({"choices": [{"message": {"content": json.dumps(DETAIL)}}]}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            self.classifier._url = f"http://127.0.0.1:{server.server_port}"
            with patch.object(self.classifier, "_ensure_loaded"), patch.dict("os.environ", {"HTTP_PROXY": "http://127.0.0.1:1", "NO_PROXY": ""}), self.assertLogs(mc.LOG, level="INFO") as logs:
                self.assertEqual(self.classifier.classify("synthetic test" * 400), LABEL)
            self.assertFalse(observed[0]["chat_template_kwargs"]["enable_thinking"])
            self.assertEqual(observed[0]["response_format"]["json_schema"]["schema"], mc.DETAIL_SCHEMA)
            self.assertIn("input_truncated=true", str(logs.output))
            self.assertNotIn("synthetic test", str(logs.output))
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_cuda_startup_oom_releases_before_cpu_fallback(self):
        launches = []
        failed, healthy = Mock(), Mock()
        failed.poll.return_value = 1
        healthy.poll.return_value = None

        def launch(args, **kwargs):
            launches.append(args)
            if len(launches) == 1:
                kwargs['stdout'].write(b'CUDA out of memory')
                kwargs['stdout'].flush()
                return failed
            return healthy

        with patch.object(mc.subprocess, 'Popen', side_effect=launch), patch.object(self.classifier, '_request', return_value={'status': 'ok'}):
            self.classifier._ensure_loaded()
        self.assertEqual(self.classifier.actual_device, 'cpu')
        self.assertEqual(len(launches), 2)
        self.assertEqual(launches[0][launches[0].index('-ngl') + 1], '99')
        self.assertEqual(launches[1][launches[1].index('-ngl') + 1], '0')
        self.assertTrue(failed.wait.called or failed.poll.called)

    def test_manual_cuda_startup_failure_never_switches_cpu(self):
        self.classifier.runtime['device'] = 'cuda'
        process = Mock()
        process.poll.return_value = 1

        def launch(args, **kwargs):
            kwargs['stdout'].write(b'CUDA out of memory')
            kwargs['stdout'].flush()
            return process

        with patch.object(mc.subprocess, 'Popen', side_effect=launch) as spawned:
            with self.assertRaises(mc.ClassifierUnavailable):
                self.classifier._ensure_loaded()
        self.assertEqual(spawned.call_count, 1)

    def test_bad_gguf_does_not_trigger_device_fallback(self):
        process = Mock()
        process.poll.return_value = 1

        def launch(args, **kwargs):
            kwargs['stdout'].write(b'invalid magic in model file')
            kwargs['stdout'].flush()
            return process

        with patch.object(mc.subprocess, 'Popen', side_effect=launch) as spawned:
            with self.assertRaises(mc.ClassifierUnavailable):
                self.classifier._ensure_loaded()
        self.assertEqual(spawned.call_count, 1)

    def test_metal_uses_gpu_layers_without_torch_mps_probe(self):
        process = Mock()
        process.poll.return_value = None
        with patch.object(mc.subprocess, 'run', return_value=Mock(stdout=b'Metal0: Apple M4', returncode=0)), patch.object(mc.subprocess, 'Popen', return_value=process) as launch, patch.object(self.classifier, '_request', return_value={'status': 'ok'}):
            self.classifier._ensure_loaded()
        self.assertEqual(self.classifier.actual_device, 'metal')
        args = launch.call_args.args[0]
        self.assertEqual(args[args.index('-ngl') + 1], '99')
        self.assertEqual(args[args.index('--device') + 1], 'Metal0')

    def test_idle_unload_clears_backend_failure_history(self):
        process = Mock()
        process.poll.return_value = None
        self.classifier._process = process
        self.classifier.runtime['idle_unload_seconds'] = .01
        self.classifier._last_used = 0
        self.classifier._failed_backends = {'cuda'}

        def stopped():
            self.classifier._closed = True

        with patch.object(mc.time, 'sleep'), patch.object(self.classifier, '_busy', return_value=False), patch.object(self.classifier, '_stop_process', side_effect=stopped) as stop:
            self.classifier._idle_watch()
        stop.assert_called_once()
        self.assertIsNone(self.classifier._process)
        self.assertEqual(self.classifier._failed_backends, set())

    def test_cuda_preserves_existing_explicit_gpu_pin(self):
        self.classifier.runtime['server_args'] = ['--device', 'CUDA1', '-fa', 'on']
        process = Mock()
        process.poll.return_value = None
        with patch.object(mc.subprocess, 'run', return_value=Mock(stdout=b'CUDA0: one\nCUDA1: two', returncode=0)), patch.object(mc.subprocess, 'Popen', return_value=process) as launch, patch.object(self.classifier, '_request', return_value={'status': 'ok'}):
            self.classifier._ensure_loaded()
        args = launch.call_args.args[0]
        self.assertEqual(args.count('--device'), 1)
        self.assertEqual(args[args.index('--device') + 1], 'CUDA1')
        self.assertIn('--cont-batching', args)

    def test_manual_cpu_does_not_attach_to_cuda_shared_service(self):
        self.classifier.runtime['device'] = 'cpu'
        self.classifier._state_path().write_text(json.dumps({'pid': 123, 'url': 'http://127.0.0.1:1',
            'device': 'cuda', 'configuration': self.classifier._configuration_signature()}), 'utf-8')
        with patch.object(self.classifier, '_request') as request:
            self.assertIsNone(self.classifier._shared_url())
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
