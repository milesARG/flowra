# PyInstaller build for Flowra  ->  dist\Flowra\Flowra.exe  (one-folder, fast start)
import site
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

sp = Path([p for p in site.getsitepackages() if p.endswith("site-packages")][0])

# NVIDIA cuBLAS / cuDNN runtime DLLs -> nvidia/<pkg>/bin (flowra.py adds these dirs to the DLL path)
nvidia_bins = []
for dll in (sp / "nvidia").glob("*/bin/*.dll"):
    nvidia_bins.append((str(dll), f"nvidia/{dll.parent.parent.name}/bin"))

datas = [("settings.html", "."), ("assets/logo.png", "assets"), ("assets/flowra.ico", "assets")]
datas += collect_data_files("faster_whisper")          # Silero VAD model
datas += collect_data_files("onnxruntime")

binaries = nvidia_bins + collect_dynamic_libs("ctranslate2") + collect_dynamic_libs("onnxruntime")

hidden = ["pystray._win32", "comtypes.stream"] + collect_submodules("comtypes.gen") \
    + ["overlay", "target", "settings_server"]

a = Analysis(["flowra.py"], pathex=["."], binaries=binaries, datas=datas, hiddenimports=hidden,
             excludes=["tkinter", "matplotlib", "IPython", "pytest"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Flowra", icon="assets/flowra.ico",
          console=False, version="version_info.txt", upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="Flowra", upx=False)
