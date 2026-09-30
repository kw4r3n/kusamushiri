# PyInstaller spec: `uv run --group build pyinstaller --noconfirm --clean packaging/kusamushiri.spec` writes dist/kusamushiri/.
# Chromium is not bundled; the app downloads it on first launch.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

root = Path(SPECPATH).parent
# The UI only uses QtCore/QtGui/QtWidgets; plugins otherwise drag in QML, Quick and PDF.
UNUSED_QT_MODULES = ("QtQml", "QtQuick", "QtPdf", "QtNetwork", "QtOpenGL", "QtVirtualKeyboard", "QtSvg")
UNUSED_QT_LIBS = tuple(f"Qt6{m[2:]}" for m in UNUSED_QT_MODULES)

a = Analysis(
    [str(root / "packaging" / "launcher.py")],
    pathex=[str(root / "src")],
    datas=collect_data_files("playwright"),
    excludes=["tkinter", *(f"PySide6.{m}" for m in UNUSED_QT_MODULES)],
    noarchive=False,
)
a.binaries = [
    b for b in a.binaries
    if not any(lib in Path(b[0]).name for lib in UNUSED_QT_LIBS) and "qtvirtualkeyboard" not in b[0].lower()
]
a.datas = [d for d in a.datas if "PySide6/Qt/translations" not in d[0].replace("\\", "/")]
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="kusamushiri",
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="kusamushiri", upx=False)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="kusamushiri.app",
        bundle_identifier="io.github.kw4r3n.kusamushiri",
        info_plist={"NSHighResolutionCapable": True},
    )
