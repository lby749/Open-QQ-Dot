# Model Recommendations and Fallback Guide

This guide assists users in selecting local embedding, reranker, and classification models, detailing default fallbacks when local weights are absent.

---

## 1. Recommended Lightweight Models

The base distribution does not package model weights. Users requiring enhanced offline semantic understanding may download:

| Model Role | Recommended Model | Dimensions | Parameter Size | Download Source |
|---|---|---|---|---|
| Semantic Embedding | `bge-small-zh-v1.5` | 512 | ~100M (~400MB) | HuggingFace / ModelScope |
| Semantic Embedding (Multilingual) | `bge-m3` | 1024 | ~560M (~2.2GB) | HuggingFace / ModelScope |
| Candidate Reranking | `bge-reranker-base` | - | ~280M (~1.1GB) | HuggingFace / ModelScope |

---

## 2. Programmatic Fallback Pipeline

When local weights are omitted:
1. **Keyword Retrieval**: Relies on inverted indexing and BM25 token frequencies.
2. **Intent Classification**: Evaluates deterministic regex rules and token heuristics.
3. **Synthesis & Dialogue**: Handled via external cloud APIs or CLI sessions (Codex / Claude / Antigravity).

For device configuration and runtime options, consult [Cross-Platform Local Inference](local_inference_cross_platform.md).
