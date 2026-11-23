# Third-party notices — F95Checker Enhanced

F95Checker Enhanced is a modified version of **F95Checker** by **WillyJL**
(https://github.com/WillyJL/F95Checker), licensed under the **GNU General Public License v3.0** (see `LICENSE`).
The modifications (fork: https://github.com/Alex200295/F95Checker_Enhanced) are released under the same license.
Their full source code is in that repository and in the `F95Checker_Enhanced-Source.zip` of every release.

The Windows release also ships the following programs and model, unmodified, in the `tools` folder next to
`F95Checker.exe`. They are separate programs, started by F95Checker as external processes (or, for the model, read as
data). Each keeps its own license:

| Folder | What | Version | License | Source / home |
|---|---|---|---|---|
| `tools/realesrgan` | Real-ESRGAN ncnn Vulkan (sharper banners, HD pictures) + its models | 0.2.5.0 (20220424) | BSD 3-Clause (Real-ESRGAN, © 2021 Xintao Wang), ncnn: BSD 3-Clause (© Tencent) | https://github.com/xinntao/Real-ESRGAN |
| `tools/rife` | RIFE ncnn Vulkan (60 fps animations), model `rife-v4.6` only | 20221029 | MIT (© nihui; RIFE models © hzwer, MIT) | https://github.com/nihui/rife-ncnn-vulkan |
| `tools/ffmpeg` | FFmpeg `ffmpeg.exe` + `ffprobe.exe`, "essentials" build by Gyan Doshi | 8.1.2 | **GPL v3** (built with `--enable-gpl --enable-version3`, see `tools/ffmpeg/LICENSE`) | Build: https://www.gyan.dev/ffmpeg/builds/ — https://github.com/GyanD/codexffmpeg/releases/tag/8.1.2 — FFmpeg source code: https://ffmpeg.org/download.html and https://github.com/FFmpeg/FFmpeg |
| `tools/depth` | Depth Anything V2 **Small**, ONNX export (3D & living pictures) | — | Apache 2.0 (© Depth Anything authors) | https://huggingface.co/depth-anything/Depth-Anything-V2-Small — ONNX: https://huggingface.co/onnx-community/depth-anything-v2-small |

`vcomp140.dll` in `tools/realesrgan` and `tools/rife` is the Microsoft Visual C++ OpenMP runtime, redistributed as
shipped by those projects.

Python libraries bundled in `lib` by the build (as in the original F95Checker, plus): **ONNX Runtime DirectML**
(MIT, © Microsoft — https://github.com/microsoft/onnxruntime, includes the DirectML redistributable under its Microsoft
license), **PyAV** (BSD 3-Clause — https://github.com/PyAV-Org/PyAV, with FFmpeg libraries under LGPL/GPL),
**NumPy** (BSD 3-Clause), **soundcard** (BSD 3-Clause).

Not shipped, never redistributed: the **Depth Anything V2 Base** model (CC BY-NC 4.0, non-commercial: download it
yourself if you want it), the **DLSS 5 / ReShade kit** (NVIDIA DLLs, ReShade, add-ons: F95Checker only installs a kit
that you built yourself), **Intiface Central**, **Lossless Scaling**, **7-Zip / WinRAR**, **JDownloader**.

## Written offer for GPL source code

The source code of F95Checker Enhanced is published at https://github.com/Alex200295/F95Checker_Enhanced (each release
has a `-Source.zip`). The source code of the shipped FFmpeg build is available from the links above (FFmpeg tag
`n8.1.2`). If any of these links stops working, open an issue on the fork's GitHub repository and the source will be
provided for at least three years after the release that shipped it.
