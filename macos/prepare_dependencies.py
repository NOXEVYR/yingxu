"""Install exact macOS build archives after size and SHA-256 verification."""
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import urllib.request

ROOT=Path(__file__).resolve().parents[1]

def main():
    if sys.platform!='darwin' or platform.machine()!='arm64' or sys.version_info[:2]!=(3,13):
        raise SystemExit('Use CPython 3.13 on Apple Silicon macOS')
    lock=json.loads((ROOT/'macos/dependencies-lock.json').read_text())
    target=ROOT/'.release-work/mac-dependencies';target.mkdir(parents=True,exist_ok=True)
    wheels=[];sources=[]
    print(f"Locked dependency downloads: {lock['bytes']} bytes",flush=True)
    for entry in lock['packages']:
        path=target/entry['filename']
        assert path.parent==target and entry['url'].startswith('https://files.pythonhosted.org/')
        if not path.exists():
            with urllib.request.urlopen(entry['url'],timeout=60) as response:
                data=response.read(entry['bytes']+1)
            if len(data)!=entry['bytes'] or hashlib.sha256(data).hexdigest()!=entry['sha256']:
                raise RuntimeError('Dependency verification failed: '+entry['filename'])
            path.write_bytes(data)
        assert path.stat().st_size==entry['bytes']
        assert hashlib.sha256(path.read_bytes()).hexdigest()==entry['sha256']
        (wheels if path.suffix=='.whl' else sources).append(str(path))
    command=[sys.executable,'-m','pip','install','--disable-pip-version-check','--no-index','--no-deps']
    subprocess.run(command+wheels,check=True)
    if sources:
        subprocess.run(command+['--no-build-isolation']+sources,check=True)
    subprocess.run([sys.executable,'-m','pip','check'],check=True)

if __name__=='__main__':main()
