"""Client for the local Qwen3.5-2B llama.cpp server, tuned for corpus annotation.

Why this is not just message_classifier.LocalMessageClassifier
-------------------------------------------------------------
The project's existing classifier is correct for its job (a fixed four-label
schema over chat messages) but it cannot serve this one:

* its system prompt bakes in one task and one enum, and this pipeline swaps the
  task per run;
* it truncates context to the last 3 entries of 160 characters, which would
  silently drop most of a multi-party MPDD dialogue;
* it writes timing lines into ``build/message-classifier-runtime/timing.log``,
  so a long corpus run would append a great deal to an existing project file.

The runtime itself is not duplicated: this client reads the same
``runtime.json`` manifest, launches the same hash-pinned llama-server binary and
the same Qwen_Qwen3.5-2B-Q4_K_M.gguf, and uses the loading strategy documented
in the project's own service. Only the per-task prompt/grammar/model handling is
new, and no new model or binary is downloaded.

Prompt caching note: llama.cpp reuses the longest common token prefix between
successive requests in a slot. Schema enum values are the raw dataset labels
while the Chinese glosses live in the system prompt, so the system prefix is
byte-identical for every utterance of a task and stays cacheable.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import sys
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
    import project_contract
else:
    from . import project_contract

DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_RELATIVE = Path("build") / "message-classifier-runtime"


class ModelUnavailable(RuntimeError):
    """The local model could not be started or did not answer usefully."""


class AnnotationClient:
    """Resident llama-server plus JSON-schema-constrained single-label calls."""

    def __init__(self, project_root=DEFAULT_PROJECT_ROOT, slots: int | None = None,
                 context_size: int = 16384, threads: int = 4, force_cpu: bool = False, device: str | None = None):
        self.project_root = Path(project_root).resolve()
        self.runtime_dir = self.project_root / RUNTIME_RELATIVE
        manifest_path = self.runtime_dir / "runtime.json"
        try:
            self.runtime = json.loads(manifest_path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise ModelUnavailable(
                f"model runtime manifest unreadable: {manifest_path}") from exc
        for key in ("server", "model"):
            if key not in self.runtime:
                raise ModelUnavailable(f"runtime.json lacks {key!r}")
        model = self.runtime["model"]
        model_path = Path(model)
        if not model.isascii() or model_path.is_absolute() or ".." in model_path.parts:
            raise ModelUnavailable("model path must be a plain ASCII relative path")
        if not (self.runtime_dir / model).is_file():
            raise ModelUnavailable(f"model weights missing: {self.runtime_dir / model}")
        self._requested_slots = slots
        self.context_size = context_size
        self.threads = threads
        self.force_cpu = force_cpu
        from inference_device import preference
        if force_cpu and device not in (None, 'auto', 'cpu'):
            raise ValueError('--cpu conflicts with the explicitly selected device')
        self.requested_device = 'cpu' if force_cpu else preference(device if device is not None else self.runtime.get('device'), 'classifier', self.project_root)
        self._failed_backends = set()
        self._process = None
        self._log_file = None
        self._closed = False
        self._failed = None
        self._lock = threading.Lock()
        # Bypass any system proxy for the loopback server.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.load_count = 0
        self.load_seconds = None
        self.actual_device = None
        self.slots = 0

    # ----------------------------------------------------------------- #
    # Lifecycle
    # ----------------------------------------------------------------- #
    def _choices(self):
        from inference_device import llama_devices
        return [choice for choice in llama_devices(self.runtime_dir,self.runtime,self.requested_device,
            project_root=self.project_root) if choice[1] not in self._failed_backends]

    def _pick_executable(self):
        executable,device,_ = self._choices()[0]
        return executable,device,'forced_cpu' if self.force_cpu else None

    def start(self) -> None:
        from inference_device import device_error
        with self._lock:
            if self._closed:
                raise ModelUnavailable('client closed')
            if self._failed is not None:
                raise ModelUnavailable(self._failed)
            if self._process is not None:
                if self._process.poll() is not None:
                    self._failed='llama-server exited'
                    raise ModelUnavailable(self._failed)
                return
            started=time.perf_counter()
            choices=self._choices()
            if not choices:
                raise ModelUnavailable('No remaining compatible llama.cpp backend')
            for index,(executable,device,device_args) in enumerate(choices):
                if not executable.is_file():
                    raise ModelUnavailable(f'server binary missing: {executable}')
                self.actual_device=device
                self.fallback_reason='backend_compatibility' if self._failed_backends else 'forced_cpu' if self.force_cpu else None
                self.slots=self._requested_slots or min(4,max(1,int(self.runtime.get('slots',4))))
                try:
                    with socket.socket() as reservation:
                        reservation.bind(('127.0.0.1',0));port=reservation.getsockname()[1]
                    self._url=f'http://127.0.0.1:{port}'
                    self.server_log_path=self.runtime_dir / f'annotation-server-{os.getpid()}-{device}.log'
                    self._log_file=self.server_log_path.open('wb')
                    command=[str(executable),'-m',self.runtime['model'],'--host','127.0.0.1','--port',str(port),
                        '-ngl','99' if device!='cpu' else '0','-c',str(self.context_size),'-np',str(self.slots),
                        '--cont-batching','--no-ui','--jinja','--reasoning','off','--temp','0','--top-k','1',
                        '--top-p','1','--seed','42','--samplers','top_k;temperature',*device_args]
                    if device=='cpu':
                        command+=['--threads',str(self.threads),'--threads-batch',str(self.threads)]
                    self._process=subprocess.Popen(command,cwd=self.runtime_dir,stdout=self._log_file,
                        stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    self.load_count+=1
                    deadline=time.monotonic()+180
                    while time.monotonic()<deadline:
                        if self._process.poll() is not None:
                            raise ModelUnavailable('llama-server exited during load')
                        try:
                            if self._request('/health',timeout=2).get('status')=='ok':
                                self.load_seconds=time.perf_counter()-started
                                logging.getLogger(__name__).info('annotation model backend=%s slots=%s',device,self.slots)
                                return
                        except (OSError,ValueError):
                            pass
                        time.sleep(.1)
                    raise ModelUnavailable('llama-server loading timed out')
                except Exception as exc:
                    self._stop_server()
                    diagnostic=self.server_log_path.read_text('utf-8',errors='replace')[-8192:]
                    if self.requested_device!='auto' or index+1==len(choices) or not device_error(RuntimeError(str(exc)+' '+diagnostic),device):
                        self._failed=str(exc)
                        raise
                    self._failed_backends.add(device)
                    logging.getLogger(__name__).warning('annotation %s backend failed; released before fallback to %s',device,choices[index+1][1])

    def _recover_backend(self, failed_device):
        with self._lock:
            if self.actual_device!=failed_device:
                return
            self._stop_server()
            self._failed_backends.add(failed_device)
            if self.requested_device!='auto':
                self._failed='Manual device inference failed; service released without automatic failover'
                raise ModelUnavailable(self._failed)
            self._failed=None
        self.start()

    def _request(self, path: str, payload: dict | None = None, timeout: float = 120):
        body = None if payload is None else json.dumps(payload,ensure_ascii=False).encode('utf-8')
        def send():
            request=urllib.request.Request(self._url+path,data=body,headers={'Content-Type':'application/json'})
            with self._opener.open(request,timeout=timeout) as response:
                return json.load(response)
        from inference_device import llama_request
        return llama_request(self,path,send)

    def _stop_server(self):
        process,self._process=self._process,None
        from inference_device import stop_llama_process
        stop_llama_process(process,timeout=8)
        if self._log_file is not None:
            self._log_file.close();self._log_file=None

    def close(self) -> None:
        with self._lock:
            self._closed=True
            self._stop_server()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    # ----------------------------------------------------------------- #
    # Project four-label contract (plan A)
    # ----------------------------------------------------------------- #
    def classify_project_contract(self, text, direction=None, context=(), following=(),
                                  retries: int = 2) -> dict:
        """Run the project's own four-label classification on one message.

        Uses ``message_classifier``'s SYSTEM prompt, enums, review reasons,
        generation parameters and nested schema, plus its post-validation
        short-message review guard. Returns
        ``{"labels", "needs_review", "reason", "seconds", "attempts"}``.
        """
        self.start()
        messages = project_contract.build_messages(text, direction, context, following)
        payload = project_contract.generation_payload(messages)
        last_error = "unknown"
        for attempt in range(retries + 1):
            started = time.perf_counter()
            try:
                response = self._request("/v1/chat/completions", payload)
                content = response["choices"][0]["message"]["content"]
                detail = project_contract.validate_detail(json.loads(content))
                detail = project_contract.apply_short_message_guard(
                    text, direction, context, detail)
                detail["seconds"] = round(time.perf_counter() - started, 3)
                detail["attempts"] = attempt + 1
                return detail
            except (OSError, ValueError, KeyError, IndexError, TypeError,
                    urllib.error.URLError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if self._process is not None and self._process.poll() is not None:
                    self._failed = "llama-server exited during inference"
                    raise ModelUnavailable(self._failed) from exc
                time.sleep(0.5 * (attempt + 1))
        raise ModelUnavailable("project-contract classification failed after "
                               f"{retries + 1} attempts: {last_error}")

    def classify_project_many(self, jobs, max_workers: int | None = None) -> list:
        """Classify many project-contract jobs concurrently, in input order.

        Each job is a mapping with ``text``, ``direction``, ``context`` and
        ``following``.
        """
        self.start()
        jobs = list(jobs)
        if not jobs:
            return []
        workers = max_workers or self.slots
        results: list = [None] * len(jobs)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self.classify_project_contract, job["text"],
                            job.get("direction"), job.get("context", ()),
                            job.get("following", ())): index
                for index, job in enumerate(jobs)
            }
            for future, index in futures.items():
                try:
                    results[index] = future.result()
                except Exception as exc:  # noqa: BLE001 - reported per item
                    results[index] = {"labels": None, "needs_review": None,
                                      "reason": None, "attempts": 0,
                                      "error": f"{type(exc).__name__}: {exc}"}
        return results

    # ----------------------------------------------------------------- #
    # Inference
    # ----------------------------------------------------------------- #
    def annotate_one(self, task, utterance, target_speaker=None, max_tokens: int = 64,
                     retries: int = 2, glosses: bool = False) -> dict:
        """Annotate one utterance; returns ``{"value", "confidence", ...}``."""
        self.start()
        messages, _ = task.messages(utterance, target_speaker, glosses=glosses)
        payload = {
            "messages": messages,
            "temperature": 0, "top_k": 1, "top_p": 1, "seed": 42,
            "samplers": ["top_k", "temperature"],
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "annotation", "strict": True,
                                                "schema": task.schema()}},
        }
        last_error = "unknown"
        for attempt in range(retries + 1):
            started = time.perf_counter()
            try:
                response = self._request("/v1/chat/completions", payload)
                content = response["choices"][0]["message"]["content"]
                parsed = json.loads(content)
                if not isinstance(parsed, dict):
                    raise ValueError("reply is not a JSON object")
                value = parsed.get(task.field)
                if value not in task.allowed:
                    raise ValueError(f"value {value!r} is outside the task enum")
                # ``cf``/``amb`` are the compact keys the schema declares.
                confidence = parsed.get("cf", parsed.get("confidence"))
                if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
                    confidence = None
                result = {"value": value,
                          "confidence": None if confidence is None else round(float(confidence), 3),
                          "attempts": attempt + 1,
                          "seconds": round(time.perf_counter() - started, 3)}
                if task.scored:
                    ambiguous = parsed.get("amb", parsed.get("ambiguous"))
                    result["ambiguous"] = bool(ambiguous)
                return result
            except (OSError, ValueError, KeyError, IndexError, TypeError,
                    urllib.error.URLError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if self._process is not None and self._process.poll() is not None:
                    self._failed = "llama-server exited during inference"
                    raise ModelUnavailable(self._failed) from exc
                time.sleep(0.5 * (attempt + 1))
        raise ModelUnavailable(f"annotation failed after {retries + 1} attempts: {last_error}")

    def annotate_many(self, task, items, target_speaker=None,
                      max_workers: int | None = None, glosses: bool = False) -> list:
        """Annotate a sequence concurrently, preserving input order.

        Results are collected and returned after the batch finishes; the caller
        then applies them sequentially, so no shared state is touched from the
        worker threads.
        """
        self.start()
        items = list(items)
        if not items:
            return []
        workers = max_workers or self.slots
        results: list = [None] * len(items)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self.annotate_one, task, item, target_speaker,
                            glosses=glosses): index
                for index, item in enumerate(items)
            }
            try:
                for future, index in futures.items():
                    try:
                        results[index] = future.result()
                    except Exception as exc:  # noqa: BLE001 - reported per item
                        results[index] = {"value": None, "confidence": None,
                                          "attempts": 0,
                                          "error": f"{type(exc).__name__}: {exc}"}
            except KeyboardInterrupt:
                for future in futures:
                    future.cancel()
                raise
        return results
