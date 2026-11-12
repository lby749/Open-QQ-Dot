# AMD DirectML（实验性）

适用：Windows + AMD Radeon 显卡，且需要本地向量/重排模型 GPU 加速。

## 限制

- `torch-directml` 目前只有 CPython 3.10–3.12 wheel，**不支持 Python 3.13**。
- 它要求 `torch==2.4.1`，与默认安装的 `torch>=2.6` 不同。
- 本地大模型 `llama.cpp` **不支持 DirectML**；这条路径只影响 PyTorch 的向量/重排模型。
- TTS 走 sherpa-onnx，本版本不会因为选择 DirectML 而自动切换到 AMD GPU。
- 当前开发机是 NVIDIA RTX 5070，没有 AMD 显卡，DirectML 路径未做真实 AMD 端到端验收。

## 安装（独立 Python 3.11/3.12 环境）

```powershell
py -3.12 -m venv D:\OpenQQDot-amd
D:\OpenQQDot-amd\Scripts\python.exe -m pip install -r requirements-embedding-directml.txt
```

把该解释器交给本地向量/重排运行环境；不要把 `torch-directml` 安装进默认 Python 3.13 的 `.venv`。

## 使用

1. 在「模型来源 / 生效模型选择」把运行硬件设为：
   ```text
   AMD DirectML
   ```
2. 或设置：
   ```json
   {"inference_device": "directml"}
   ```
3. 重新加载本地模型，或重启程序。

## 验证

```powershell
python -X utf8 -m pytest -q tests/test_inference_devices.py
```

若 `torch_directml` 导入失败或没有可用 AMD 设备，会明确报错，不会静默假装使用 GPU。
