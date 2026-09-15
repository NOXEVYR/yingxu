"""Build a windowless x64 desktop EXE with the local .NET Framework compiler.

The Microsoft SDK archive must already be downloaded. No network access, package
installation, data copying or service restart occurs during this build.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import zipfile

SDK_VERSION = "1.0.4191.47"
SDK_SHA256 = "f492bbf547d0da329553b6727435b677579b1e9f91cc9e4a1ad029366d5f23d0"
ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "desktop"


def cleanup_integration_fixture(value):
    fixture = Path(value).resolve()
    if fixture.parent != Path(tempfile.gettempdir()).resolve() or not fixture.name.startswith('yingxu-lifecycle-') or fixture.is_symlink():
        raise ValueError('Invalid integration fixture cleanup path')
    # The host has exited, but its WebView subprocess may still release handles.
    # Retry only sharing violations, for at most five seconds; never kill browsers.
    for attempt in range(26):
        try:
            shutil.rmtree(fixture)
            return
        except FileNotFoundError:
            if not fixture.exists():
                return
            raise
        except OSError as error:
            if getattr(error, 'winerror', None) not in (32, 33) or attempt == 25:
                raise
            time.sleep(0.2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-package", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--manifest-output", type=Path, help="Optional separate build record for a staged EXE")
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--alias-root", type=Path, help="Optional existing junction to test against the physical application folder")
    args = parser.parse_args()
    if hashlib.sha256(args.sdk_package.read_bytes()).hexdigest() != SDK_SHA256:
        raise SystemExit("WebView2 SDK archive SHA-256 does not match the pinned official package.")
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if not compiler.is_file():
        raise SystemExit("The Windows .NET Framework C# compiler is not available.")
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="yingxu-desktop-build-") as temporary:
        folder = Path(temporary)
        members = {
            "Microsoft.Web.WebView2.Core.dll": "lib/net462/Microsoft.Web.WebView2.Core.dll",
            "Microsoft.Web.WebView2.WinForms.dll": "lib/net462/Microsoft.Web.WebView2.WinForms.dll",
            "WebView2Loader.dll": "runtimes/win-x64/native/WebView2Loader.dll",
            "WebView2-LICENSE.txt": "LICENSE.txt",
        }
        with zipfile.ZipFile(args.sdk_package) as archive:
            for name, member in members.items():
                (folder / name).write_bytes(archive.read(member))
        common = [str(compiler), "/nologo", "/optimize+", "/platform:x64", "/utf8output",
                  "/reference:System.dll", "/reference:System.Core.dll", "/reference:System.Web.Extensions.dll"]
        if args.test:
            tests = folder / "desktop-tests.exe"
            subprocess.run(common + ["/target:exe", f"/out:{tests}", str(DESKTOP / "Core.cs"),
                                      str(DESKTOP / "Integration.cs"),
                                      str(DESKTOP / "Tests.cs")], check=True)
            subprocess.run([str(tests), str(ROOT)] + ([str(args.alias_root)] if args.alias_root else []), check=True)
            capture_tests = folder / "capture-tests.exe"
            subprocess.run(common + ["/target:exe", f"/out:{capture_tests}",
                "/reference:System.Drawing.dll", "/reference:System.Windows.Forms.dll",
                str(DESKTOP / "Core.cs"), str(DESKTOP / "Integration.cs"),
                str(DESKTOP / "Capture.cs"), str(DESKTOP / "CaptureTests.cs")], check=True)
            subprocess.run([str(capture_tests)],check=True,timeout=30)
        if args.test and (ROOT / 'runtime/webview2/msedgewebview2.exe').is_file():
            smoke = folder / 'runtime-check.exe'
            browser_refs = ["/reference:System.Drawing.dll", "/reference:System.Windows.Forms.dll",
                            f"/reference:{folder / 'Microsoft.Web.WebView2.Core.dll'}",
                            f"/reference:{folder / 'Microsoft.Web.WebView2.WinForms.dll'}"]
            subprocess.run(common + browser_refs + ["/target:exe", f"/out:{smoke}",
                f"/win32manifest:{DESKTOP / 'app.manifest'}", str(DESKTOP / 'Core.cs'), str(DESKTOP / 'RuntimeCheck.cs')], check=True)
            subprocess.run([str(smoke), str(ROOT)], check=True, timeout=55)
        exe = folder / "YingXu.exe"
        command = common + ["/target:winexe", f"/out:{exe}",
            "/reference:System.Drawing.dll", "/reference:System.Windows.Forms.dll",
            f"/reference:{folder / 'Microsoft.Web.WebView2.Core.dll'}",
            f"/reference:{folder / 'Microsoft.Web.WebView2.WinForms.dll'}",
            f"/win32manifest:{DESKTOP / 'app.manifest'}", f"/win32icon:{DESKTOP / 'brand.ico'}",
            f"/resource:{DESKTOP / 'brand.ico'},brand.ico"]
        for name in members:
            command.append(f"/resource:{folder / name},{name}")
        subprocess.run(command + [str(DESKTOP / "Core.cs"), str(DESKTOP / "Integration.cs"), str(DESKTOP / "Capture.cs"), str(DESKTOP / "Program.cs")], check=True)
        if args.test:
            lifecycle = folder / "lifecycle-tests.exe"
            lifecycle_command = [value for value in command if not value.startswith('/target:') and not value.startswith('/out:')]
            subprocess.run(lifecycle_command + ["/target:exe", f"/out:{lifecycle}", "/main:YingXu.Desktop.LifecycleTests",
                str(DESKTOP / "Core.cs"), str(DESKTOP / "Integration.cs"), str(DESKTOP / "Capture.cs"), str(DESKTOP / "Program.cs"),
                str(DESKTOP / "LifecycleTests.cs")], check=True)
            subprocess.run([str(lifecycle),str(ROOT)],check=True,timeout=30)
            if (ROOT / 'runtime/webview2/msedgewebview2.exe').is_file():
                for mode in ('--zoom-integration', '--startup-integration'):
                    integration = subprocess.run([str(lifecycle), str(ROOT), mode,
                        str(ROOT / 'runtime/webview2')], capture_output=True, text=True, encoding='utf-8', timeout=60)
                    print(integration.stdout, end='')
                    if integration.stderr: print(integration.stderr, end='')
                    for line in integration.stdout.splitlines():
                        if not line.startswith('ZOOM_FIXTURE_CLEANUP_AFTER_EXIT='): continue
                        cleanup_integration_fixture(line.split('=', 1)[1])
                    integration.check_returncode()
        shutil.copy2(exe, output)
    result = {"exe": output.name, "bytes": output.stat().st_size,
              "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
              "architecture": "x64", "subsystem": "Windows GUI", "sdk": SDK_VERSION,
              "sdk_sha256": SDK_SHA256, "contains_user_data": False}
    manifest = args.manifest_output or DESKTOP / "build.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
