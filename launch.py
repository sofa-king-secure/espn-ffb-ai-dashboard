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
sys.path.insert(0, str(ROOT))
from core.config import lan_ips, load_settings  # noqa: E402

cmd = [sys.executable, "-m", "streamlit", "run", str(ROOT / "app.py")]
if load_settings().lan_access:
    # Listen on every interface (LAN + VPN). .streamlit/config.toml keeps 127.0.0.1 as the default.
    cmd += ["--server.address", "0.0.0.0"]
    print("Network access ON. From other devices on your network / VPN:")
    for ip in lan_ips() or ["(no network address found)"]:
        print(f"    http://{ip}:8501")
    print("Windows: if the firewall asks, allow Python on PRIVATE networks only.\n")

webbrowser.open_new_tab("http://localhost:8501")
try:
    sys.exit(subprocess.call(cmd))
except KeyboardInterrupt:  # Ctrl+C: Streamlit gets the same signal and stops; exit quietly
    print("\nStopped.")
    sys.exit(0)
