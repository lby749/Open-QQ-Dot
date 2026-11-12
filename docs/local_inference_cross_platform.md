# Local Models: CUDA / MPS / Metal / CPU

This document specifies device selection, cross-platform fallbacks, and runtime dependencies for local embedding, reranking, and persona models.

---

## 1. Automatic Fallback Strategy

When device selection is set to `auto`, the runtime attempts device binding in descending priority:
1. **CUDA (NVIDIA GPUs)**: Verified via `torch.cuda.is_available()`.
2. **MPS / Metal (Apple Silicon / macOS)**: Verified via `torch.backends.mps.is_available()`.
3. **CPU (Universal Fallback)**: Employed when hardware accelerators are absent or out-of-memory errors occur.

Manual device selection (`cuda`, `mps`, `cpu`) enforces strict allocation and raises actionable errors upon failure rather than silently downgrading.

---

## 2. Platform Dependencies

- **Windows (CUDA / CPU)**:
  - Install PyTorch with CUDA support if an NVIDIA GPU is available (`requirements-embedding-cuda.txt`).
  - Standard CPU installation uses `requirements-embedding-cpu.txt`.
- **macOS (MPS / CPU)**:
  - Supports Apple Silicon hardware acceleration via MPS.
- **Linux (CUDA / ROCm / CPU)**:
  - Standard PyTorch wheel installations apply.

---

## 3. Fallback Degradation

If local models are not downloaded or fail to load, the system degrades automatically to:
- Heuristic rule matching
- Inverted index and BM25 token retrieval
- Remote API endpoints (if configured)
