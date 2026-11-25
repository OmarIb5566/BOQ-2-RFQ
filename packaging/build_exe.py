"""Builds dist/BOQ2RFQ/BOQ2RFQ.exe and dist/BOQ2RFQ.zip (ready to send).

    .venv\\Scripts\\python.exe packaging\\build_exe.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    py = sys.executable
    subprocess.run([py, "-m", "pip", "install", "-q", "pyinstaller"], check=True, cwd=ROOT)
    subprocess.run([py, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", "dist", "--workpath", "build",
                    str(Path("packaging") / "BOQ2RFQ.spec")], check=True, cwd=ROOT)
    out = ROOT / "dist" / "BOQ2RFQ"
    shutil.copy2(ROOT / "packaging" / "README - START HERE.txt", out)
    shutil.make_archive(str(ROOT / "dist" / "BOQ2RFQ"), "zip", root_dir=out.parent, base_dir=out.name)
    print(f"\nDone: {ROOT / 'dist' / 'BOQ2RFQ.zip'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
