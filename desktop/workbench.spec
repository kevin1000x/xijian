# PyInstaller: one shared onedir bundle, windowed shell + console protocol host.
from pathlib import Path
import sys
import os
root = Path(SPECPATH).parent
# Only product assets and runtime identity sources; no workspace, .git or credentials.
datas = [(str(root / 'workbench'), 'workbench'),
         (str(root / 'runtime' / 'execution_worker.py'), 'runtime'),
         (str(root / 'runtime' / 'supervised_execution.py'), 'runtime'),
         (str(root / 'runtime' / 'workbench_isolation.py'), 'runtime'),
         (str(root / 'runtime' / 'workbench_win32.py'), 'runtime'),
         (str(root / 'tools' / 'workbench_mcp.py'), 'tools'),
         (str(root / 'tools' / 'workbench_qoder_bridge.cjs'), 'tools')]
# Conda NumPy loads MKL dynamically; static PE scanning cannot discover this family.
# Include the existing MKL runtime closure, never developer settings or executables.
math_libs = Path(sys.base_prefix) / 'Library' / 'bin'
binaries = [(str(p), '.') for p in math_libs.glob('mkl*.dll')]
for name in ('libiomp5md.dll',):
    if (math_libs / name).is_file(): binaries.append((str(math_libs / name), '.'))
a = Analysis([str(root / 'desktop_app.py'), str(root / 'desktop_host.py')],
    pathex=[str(root)], datas=datas, binaries=binaries,
    hiddenimports=['webview', 'webview.platforms.edgechromium', 'webview.platforms.winforms',
                   'runtime.execution_worker', 'openpyxl'],
    excludes=['PyQt5','PyQt6','PySide2','PySide6','tkinter','matplotlib','scipy',
              'IPython','notebook','jupyter','pytest','numba','tables','h5py',
              'sqlalchemy','pyarrow','fastparquet','fsspec','s3fs','boto3',
              'lxml','sympy','torch','tensorflow','sklearn','cv2','sphinx','chardet'],
    noarchive=False)
# Package only release VC runtimes from an installed, licensed VS redist tree.
# UCRT/API-set DLLs are supplied by our supported Windows 10/11 operating system.
vc_dir = Path(os.environ.get('XIJIAN_VC_REDIST',
    'C:/Program Files/Microsoft Visual Studio/18/Community/VC/Redist/MSVC/14.44.35112/x64/Microsoft.VC143.CRT'))
vc_files = {p.name.lower(): p for p in vc_dir.glob('*.dll')}
required_vc = {'msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll'}
if not required_vc.issubset(vc_files):
    raise RuntimeError('Set XIJIAN_VC_REDIST to an installed release x64 Microsoft.VC143.CRT directory.')
kept_binaries = []
for target, source, kind in a.binaries:
    name = Path(target).name.lower()
    if name == 'ucrtbase.dll' or name.startswith('api-ms-win-') or name in vc_files:
        continue
    kept_binaries.append((target, source, kind))
a.binaries = kept_binaries + [(p.name, str(p), 'BINARY') for p in vc_files.values()]
pyz = PYZ(a.pure)
app = EXE(pyz, [s for s in a.scripts if s[0] != 'desktop_host'], [],
    exclude_binaries=True, name='Xijian', console=False, upx=False)
host = EXE(pyz, [s for s in a.scripts if s[0] != 'desktop_app'], [],
    exclude_binaries=True, name='XijianHost', console=True, upx=False)
coll = COLLECT(app, host, a.binaries, a.datas, name='Xijian', upx=False)
