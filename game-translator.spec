# -*- mode: python ; coding: utf-8 -*-
"""GameTranslator 打包配置（GPU 版，onedir 模式）
用法：.venv\\Scripts\\pyinstaller game-translator.spec --noconfirm
产物：dist/GameTranslator/ 整个文件夹压缩后分发

说明：paddle GPU 的 C 扩展在 PyInstaller 隔离进程里枚举子模块会崩溃（0xC0000005），
故 paddle/paddleocr/nvidia 整包作为数据复制到 _internal，运行时走文件系统导入。"""
import os
import sysconfig

site = sysconfig.get_paths()["purelib"]

datas = []
binaries = []
# paddle 系被 exclude 后依赖链断开：第三方依赖全部显式声明（纯 py 包，Analysis 安全收集）
hiddenimports = [
    "keyring.backends.Windows",
    # paddlepaddle-gpu 依赖
    "google.protobuf", "google.protobuf.internal",
    "opt_einsum", "safetensors", "httpx", "networkx",
    # paddleocr / paddlex 依赖
    "aiohttp", "yaml",
    "aistudio", "chardet", "colorlog", "filelock",
    "huggingface_hub", "modelscope",
    "pandas", "prettytable", "cpuinfo", "pydantic",
    "ruamel.yaml", "ujson",
    # OCR 链路常见间接依赖
    "scipy", "skimage", "shapely", "pyclipper", "lmdb", "tqdm",
    "fonttools", "defusedxml",
]

# paddle / paddleocr / paddlex / google(protobuf 命名空间包) 整包原样复制
# （保持包结构，运行时 import 走 _internal 文件系统）
for pkg in ("paddle", "paddleocr", "paddlex", "google"):
    src = os.path.join(site, pkg)
    if os.path.isdir(src):
        datas.append((src, pkg))

# CUDA 运行库整目录复制（paddle 运行时按 nvidia/*/bin 布局查找 DLL）
nvidia_src = os.path.join(site, "nvidia")
if os.path.isdir(nvidia_src):
    datas.append((nvidia_src, "nvidia"))

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 整包已作为数据复制，禁止 Analysis 再分析（避免隔离进程崩溃）
        # 注：paddlex 不列在这里——excludes 会连带过滤同名的 datas 复制
        "paddle", "paddleocr",
        # 无关大依赖减体积
        "matplotlib", "IPython", "jupyter", "pytest",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="GameTranslator",
    debug=False,
    console=False,          # 无控制台窗口
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,              # CUDA DLL 禁用 UPX（压缩会导致加载失败/误报）
    name="GameTranslator",
)
