# PyInstaller spec for BOQ2RFQ.exe — build with packaging\build_exe.bat
from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

ROOT = SPECPATH + "\\.."

datas = [
    (ROOT + "\\BOQ2RFQ.py", "."),
    (ROOT + "\\boq_agent\\knowledge\\flowchart_rules.json", "boq_agent\\knowledge"),
    (ROOT + "\\boq_agent\\rfq\\fields.json", "boq_agent\\rfq"),
    (ROOT + "\\boq_agent\\rfq\\templates", "boq_agent\\rfq\\templates"),
]
binaries = []
hiddenimports = collect_submodules("boq_agent") + [
    "xlrd", "pythoncom", "pywintypes", "win32com", "win32com.client",
]

for pkg in ("streamlit", "pymupdf", "pydantic"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h
datas += copy_metadata("streamlit")

a = Analysis(
    [ROOT + "\\packaging\\launcher.py"],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "IPython", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="BOQ2RFQ",
    console=True,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="BOQ2RFQ", upx=False)
