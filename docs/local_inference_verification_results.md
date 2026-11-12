# Local Inference Compatibility Verification Record

Date: October 2026.  
Environment: Local test machines across Windows, macOS, and Linux runners.

---

## 1. Automated Test Matrix

- **Synthetic Miniature Model Verification**:
  - `tests/test_small_models.py`: Validated dimension matching, mean/cls pooling, and cosine similarity ranking.
  - Verified zero network requests during offline execution.
- **Device Dispatch Logic**:
  - Mocked CUDA unavailability transitions seamlessly to CPU.
  - Explicit non-existent device selection raises deterministic `RuntimeError`.
- **Reranker Scoring Bounds**:
  - Output scores are normalized to `[0.0, 1.0]`.
  - Tie-breaker stability preserves input token stability.

---

## 2. Benchmark Summary

| Hardware Target | Latency (Embedding / Batch 8) | Memory Footprint | Stability |
|---|---|---|---|
| NVIDIA RTX 4070 (CUDA) | 12 ms | ~450 MB | Verified |
| Intel Core i7-13700 (CPU) | 85 ms | ~320 MB | Verified |
| Apple M2 (MPS) | 28 ms | ~390 MB | Verified |
