# PyInstaller spec for BOQ2RFQ.exe — build with: .venv\Scripts\python packaging\build_exe.py
from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = SPECPATH + "\\.."

datas = [
    (ROOT + "\\webapp\\templates", "webapp\\templates"),
    (ROOT + "\\webapp\\static", "webapp\\static"),
    (ROOT + "\\boq_agent\\knowledge\\flowchart_rules.json", "boq_agent\\knowledge"),
    (ROOT + "\\boq_agent\\knowledge\\masterformat_titles.json", "boq_agent\\knowledge"),
    (ROOT + "\\boq_agent\\knowledge\\document_rules.json", "boq_agent\\knowledge"),
    (ROOT + "\\boq_agent\\rfq\\fields.json", "boq_agent\\rfq"),
    (ROOT + "\\boq_agent\\rfq\\templates", "boq_agent\\rfq\\templates"),
]
binaries = []
hiddenimports = collect_submodules("boq_agent") + collect_submodules("webapp") + [
    "xlrd", "pythoncom", "pywintypes", "win32com", "win32com.client",
]

for pkg in ("pymupdf", "pydantic", "flask", "jinja2", "waitress"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

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
