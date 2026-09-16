"""Compatibility command: verify the final archive including frozen WKWebView."""
from pathlib import Path
import subprocess
import sys
root=Path(__file__).resolve().parents[1]
subprocess.run([sys.executable,str(root/'macos/verify_release.py'),str(root/'releases/macos-preview/YingXu-v0.4.10-mac.1-macOS-arm64.zip')],check=True)
