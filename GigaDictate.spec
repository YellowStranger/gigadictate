# PyInstaller build: python -m PyInstaller GigaDictate.spec
# One-folder build (no unpacking to %TEMP% on every start => fast start, fewer antivirus false alarms).
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

datas = collect_data_files("uiautomation", includes=["bin/*.dll"]) + collect_data_files("onnx_asr")  # mel filterbanks (fbanks.npz) etc., ~7 MB
# onnx_asr reads its own version via importlib.metadata at import time
for pkg in ("onnx-asr", "onnxruntime", "numpy"):
    datas += copy_metadata(pkg)

a = Analysis(
    ["src/gigadictate/__main__.py"],
    pathex=["src"],
    datas=datas,
    hiddenimports=["onnx_asr.models.gigaam"],
    excludes=["tkinter", "unittest", "pydoc", "doctest", "huggingface_hub", "matplotlib", "PIL",
              "scipy", "pandas", "IPython", "setuptools", "pip"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="GigaDictate", console=False,
          icon="GigaDictate.ico", upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="GigaDictate", upx=False)
