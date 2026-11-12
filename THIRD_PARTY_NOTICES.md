# Third-Party Dependencies and Licensing Notices

Open QQ Dot is distributed under the GNU General Public License v3.0 or later (GPL-3.0-or-later). External dependencies maintain their respective licenses and terms. The full GPLv3 text is in [LICENSE](LICENSE).

The blue Android companion in `android/` includes the Android Remote Control MCP source and modifications for Open QQ Dot. Copyright 2026-2027 Daniele Salvatore Albano; MIT License. Its complete license is retained in `android/LICENSE.md`. Bundled vendor components keep their own notices. The discarded `mobile/` preview is not the released Android source.

---

## UI and Runtime Dependencies

- **uv**:
  - Bundled in the Windows launcher and installer to create or repair a Python environment. Dual licensed under MIT OR Apache-2.0; copies are in `licenses/third-party/uv-LICENSE-MIT` and `licenses/third-party/uv-LICENSE-APACHE`, and embedded with each EXE.
  - Upstream source: <https://github.com/astral-sh/uv>. Existing Python environments and the user-selected uv cache are reused. Model weights are downloaded separately on demand.

- **PySide6-Fluent-Widgets (1.11.3)**:
  - Licensed under GPLv3. The full upstream license is in `licenses/third-party/PySide6-Fluent-Widgets-LICENSE`.
  - Upstream source: <https://github.com/zhiyiYo/PyQt-Fluent-Widgets>. The corresponding source for the GPL-covered UI library is available from that repository; the installer bundles the library only as part of the combined application.
  - When distributing combined binary applications incorporating this library, applicable GPLv3 compliance requirements must be satisfied.
- **PySide6 / Qt**:
  - Licensed under LGPLv3 / GPLv3 / Commercial terms. The base launcher does not bundle Qt runtime binaries; dependencies are resolved at runtime via pip.

- **msvc-runtime (optional Windows vector environment)**:
  - Microsoft C++ runtime binaries under their Microsoft license, provided by the Python package at <https://pypi.org/project/msvc-runtime/>. The downloaded package's license remains in its `dist-info/licenses` directory and accompanies the app-local DLL copy. This project's GPL does not relicense these binaries; they are downloaded into the user's environment, not included in the source archive.
- **pystray**:
  - Contains LGPL/GPL components. Upstream licensing terms apply as specified in the local license copies.
- **NapCatQQ**:
  - Source: <https://github.com/NapNeko/NapCatQQ>
  - Copyright © 2024 Mlikiowa.
  - Distributed under the project's **Limited Redistribution License**, not GPLv3. The full text is in `licenses/third-party/NapCat-LICENSE`. It permits redistribution only with the full license and clear source/copyright attribution, allows minor redistribution modifications but not their public release, and prohibits commercial use.
  - NapCat is a separate component invoked as a local process. Open QQ Dot does not relicense NapCat, and the GPLv3 license of Open QQ Dot does not replace NapCat's license.
  - The installer does **not** bundle NapCat. The runtime is downloaded only after the operator confirms on the QQ connection page; the downloaded copy receives `NAPCAT-LICENSE.txt`, `NAPCAT-SOURCE.json` and the release tag.

## Optional Local Retrieval Models

- **Tencent qq-botpy (1.2.1)**: MIT License, Copyright (c) 2021 Tencent. Official QQ robot gateway/API SDK: <https://github.com/tencent-connect/botpy>. Full license: `licenses/third-party/qq-botpy-LICENSE`. Installed as a Python dependency; bot credentials and runtime data are excluded from releases.

- **BAAI/bge-m3**: MIT License; [upstream model card](https://huggingface.co/BAAI/bge-m3).
- **BAAI/bge-reranker-v2-m3**: Apache License 2.0; [upstream model card](https://huggingface.co/BAAI/bge-reranker-v2-m3).

## Optional Local Voice Models (QQ voice replies)

- **sherpa-onnx (1.13.8)** and **ONNX (1.23.1)**: Apache License 2.0; [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx), [ONNX](https://github.com/onnx/onnx). Installed from `requirements-voice.txt`.
- **MeloTTS Chinese–English (`csukuangfj/vits-melo-tts-zh_en`)**: MIT License (MeloTTS); the `LICENSE` file is downloaded with the model. Stored locally with FP16 weights.
- **Kokoro-82M v1.1-zh (`csukuangfj/kokoro-multi-lang-v1_1`)**: Apache License 2.0; [upstream model card](https://huggingface.co/hexgrad/Kokoro-82M-v1.1-zh). The `LICENSE` file is downloaded with the model. Stored locally with FP16 weights.
- The Chinese text front ends bundle cppjieba dictionaries (MIT) and, for Kokoro, espeak-ng data (GPLv3); both are fetched with the model and are not part of this repository.
- **VoxCPM2 (`openbmb/VoxCPM2`)** and the **voxcpm** package (2.0.3): Apache License 2.0; [upstream model card](https://huggingface.co/openbmb/VoxCPM2). Runs in a separate, user-provided Python environment (`requirements-voice-clone.txt`). Voice cloning must only be used with voices the operator has the right to use.

Local model weights and Python environments are excluded from the clean source package. Redistributing weights requires retaining their upstream license and attribution notices; the application's GPL-3.0-or-later license does not replace model licenses.
