# PyInstaller build: python -m PyInstaller GigaDictate.spec
# One-folder build (no unpacking to %TEMP% on every start => fast start, fewer antivirus false alarms).
from PyInstaller.utils.hooks import collect_data_files

datas = collect_data_files("onnx_asr")  # mel filterbanks (fbanks.npz) etc., ~7 MB

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
