#!/usr/bin/env python3
"""Launcher for Mir's ESPN FFB AI Analyzer: `python launch.py` on Windows or macOS."""
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent

if sys.version_info < (3, 11):
    sys.exit(f"Python 3.11+ required (found {sys.version.split()[0]}).")

try:
    import streamlit  # noqa: F401
except ImportError:
    sys.exit("Dependencies missing. Run:  python -m pip install -r requirements.txt")

os.chdir(ROOT)
webbrowser.open_new_tab("http://localhost:8501")
try:
    sys.exit(subprocess.call([sys.executable, "-m", "streamlit", "run", str(ROOT / "app.py")]))
except KeyboardInterrupt:  # Ctrl+C: Streamlit gets the same signal and stops; exit quietly
    print("\nStopped.")
    sys.exit(0)
