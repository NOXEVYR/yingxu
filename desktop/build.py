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
import sys
import tempfile
import time
import zipfile

SDK_VERSION = "1.0.4191.47"
SDK_SHA256 = "f492bbf547d0da329553b6727435b677579b1e9f91cc9e4a1ad029366d5f23d0"
ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "desktop"
sys.path.insert(0, str(ROOT / 'tools'))
from build_identity import read_identity, generated_native_source, generated_native_manifest, require_native_identity


def cleanup_integration_fixture(value, fixture_root=None):
    fixture = Path(value).resolve()
    expected_parent = Path(fixture_root or tempfile.gettempdir()).resolve()
    if fixture.parent != expected_parent or not fixture.name.startswith('yingxu-lifecycle-') or fixture.is_symlink():
        raise ValueError('Invalid integration fixture cleanup path')
    # The host has exited, but its WebView subprocess may still release handles.
    # Retry sharing violations and files concurrently removed by WebView, for at
    # most five seconds. Never kill browsers or hide other cleanup errors.
    for attempt in range(26):
        try:
            shutil.rmtree(fixture)
            return
        except FileNotFoundError as error:
            if not fixture.exists():
                return
            # Python before 3.13 can enumerate a lock file just before WebView
            # removes it. Retry the remaining owned fixture, not an unrelated path.
            if not error.filename or not Path(error.filename).resolve().is_relative_to(fixture) or attempt == 25:
                raise
            time.sleep(0.2)
        except OSError as error:
            if getattr(error, 'winerror', None) not in (32, 33, 145) or attempt == 25:
                raise
            time.sleep(0.2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-package", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--manifest-output", type=Path, help="Optional separate build record for a staged EXE")
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--test-runtime-root", type=Path, help="Use this verified runtime directory for native WebView and media fixtures")
    parser.add_argument("--test-app-root", type=Path, help="Use this extracted/candidate application root for native tests (defaults to the supplied runtime's parent)")
    parser.add_argument("--test-fixture-root", type=Path, help="Place native test processes and fixtures in this owned existing directory")
    parser.add_argument("--isolated-test-units", action="store_true", help="Only bounded parameter/identity tests; no GUI or service startup")
    parser.add_argument("--alias-root", type=Path, help="Optional existing junction to test against the physical application folder")
    args = parser.parse_args()
    runtime_root = (args.test_runtime_root or (ROOT / "runtime")).resolve()
    test_app_root = (args.test_app_root.resolve() if args.test_app_root is not None else
                     runtime_root.parent if args.test_runtime_root is not None else ROOT)
    fixture_root = None
    test_environment = None
    if args.test or args.isolated_test_units:
        if args.test_fixture_root is not None:
            fixture_root = args.test_fixture_root.resolve()
            if not fixture_root.is_dir() or fixture_root.is_symlink():
                raise SystemExit("The native test fixture root must be an existing, ordinary directory.")
            if fixture_root == ROOT or ROOT in fixture_root.parents or fixture_root in ROOT.parents:
                raise SystemExit("Native test fixtures must be outside the source and application data roots.")
            local_data = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "YingXu"
            default_projects = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Documents/YingXu/Projects"
            for protected in (local_data.resolve(), default_projects.resolve()):
                if fixture_root == protected or fixture_root in protected.parents or protected in fixture_root.parents:
                    raise SystemExit("Native test fixtures overlap a protected application data directory.")
        else:
            fixture_root = Path(tempfile.gettempdir()).resolve()
        test_environment = os.environ.copy()
        test_environment["TEMP"] = str(fixture_root)
        test_environment["TMP"] = str(fixture_root)
        test_environment["TMPDIR"] = str(fixture_root)
        test_environment["YINGXU_WEBVIEW2_MODE"] = "bundled"
        test_environment["PYTHONUTF8"] = "1"
    if args.test_runtime_root is not None and not runtime_root.is_dir():
        raise SystemExit("The requested native test runtime directory does not exist.")
    if args.test_runtime_root is not None and args.test and (
        not (runtime_root / "webview2/msedgewebview2.exe").is_file() or
        not (runtime_root / "ffmpeg/bin/ffmpeg.exe").is_file()
    ):
        raise SystemExit("The requested native test runtime is missing its locked WebView2 or FFmpeg executable.")
    if args.test and args.test_runtime_root is not None:
        if not (test_app_root / "server.py").is_file() or not (test_app_root / "frontend" / "index.html").is_file():
            raise SystemExit("When an explicit runtime root is used, native GUI tests require its extracted application root (use --test-app-root if needed).")
        if runtime_root != (test_app_root / "runtime").resolve():
            raise SystemExit("Native WebView tests must use the runtime inside the selected application root so all hosts exercise the same runtime.")
        if read_identity(test_app_root) != read_identity(ROOT):
            raise SystemExit("The native test application root has a different build identity from the source being compiled.")
    identity = read_identity(ROOT)
    if hashlib.sha256(args.sdk_package.read_bytes()).hexdigest() != SDK_SHA256:
        raise SystemExit("WebView2 SDK archive SHA-256 does not match the pinned official package.")
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if not compiler.is_file():
        raise SystemExit("The Windows .NET Framework C# compiler is not available.")
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="yingxu-desktop-build-", dir=str(fixture_root) if fixture_root and (args.test or args.isolated_test_units) else None) as temporary:
        folder = Path(temporary)
        identity_source = folder / 'BuildIdentity.cs'
        identity_source.write_bytes(generated_native_source(identity))
        native_manifest = folder / 'app.manifest'
        native_manifest.write_bytes(generated_native_manifest(DESKTOP / 'app.manifest', identity))
        def compile_native(command, **kwargs):
            # Legacy csc treats /target following an input as a new output group.
            # Keep the generated input after every compiler option.
            return subprocess.run(command + [str(identity_source)], **kwargs)
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
            compile_native(common + ["/target:exe", f"/out:{tests}", str(DESKTOP / "Core.cs"),
                                      str(DESKTOP / "Integration.cs"),
                                      str(DESKTOP / "Tests.cs")], check=True)
            subprocess.run([str(tests), str(test_app_root)] + ([str(args.alias_root)] if args.alias_root else []), check=True, env=test_environment)
            folder_tests = folder / "folder-foreground-tests.exe"
            compile_native(common + ["/target:exe", f"/out:{folder_tests}", str(DESKTOP / "Core.cs"),
                str(DESKTOP / "Integration.cs"), str(DESKTOP / "FolderForegroundTests.cs")], check=True)
            subprocess.run([str(folder_tests)], check=True, timeout=15, env=test_environment)
            capture_tests = folder / "capture-tests.exe"
            compile_native(common + ["/target:exe", f"/out:{capture_tests}",
                "/reference:System.Drawing.dll", "/reference:System.Windows.Forms.dll",
                str(DESKTOP / "Core.cs"), str(DESKTOP / "Integration.cs"),
                str(DESKTOP / "Capture.cs"), str(DESKTOP / "CaptureTests.cs")], check=True)
            subprocess.run([str(capture_tests)],check=True,timeout=30,env=test_environment)
        browser_root = runtime_root / 'webview2'
        if args.test and (browser_root / 'msedgewebview2.exe').is_file():
            smoke = folder / 'runtime-check.exe'
            browser_refs = ["/reference:System.Drawing.dll", "/reference:System.Windows.Forms.dll",
                            f"/reference:{folder / 'Microsoft.Web.WebView2.Core.dll'}",
                            f"/reference:{folder / 'Microsoft.Web.WebView2.WinForms.dll'}"]
            compile_native(common + browser_refs + ["/target:exe", f"/out:{smoke}",
                f"/win32manifest:{native_manifest}", str(DESKTOP / 'Core.cs'), str(DESKTOP / 'RuntimeCheck.cs')], check=True)
            subprocess.run([str(smoke), str(test_app_root), str(browser_root)], check=True, timeout=55, env=test_environment)
        exe = folder / "YingXu.exe"
        command = common + ["/target:winexe", f"/out:{exe}",
            "/reference:System.Drawing.dll", "/reference:System.Windows.Forms.dll",
            f"/reference:{folder / 'Microsoft.Web.WebView2.Core.dll'}",
            f"/reference:{folder / 'Microsoft.Web.WebView2.WinForms.dll'}",
            f"/win32manifest:{native_manifest}", f"/win32icon:{DESKTOP / 'brand.ico'}",
            f"/resource:{DESKTOP / 'brand.ico'},brand.ico"]
        for name in ('quick-reader.html', 'quick-reader.css', 'quick-reader.js', 'markdown-preview.js', 'obsidian-images.js', 'quick-video.html', 'quick-video.js', 'video-player.js', 'video-player.css'):
            command.append(f"/resource:{ROOT / 'frontend' / name},{name}")
        for name in members:
            command.append(f"/resource:{folder / name},{name}")
        compile_native(command + [str(DESKTOP / "Core.cs"), str(DESKTOP / "IsolatedStartup.cs"), str(DESKTOP / "Integration.cs"), str(DESKTOP / "Capture.cs"), str(DESKTOP / "Program.cs"), str(DESKTOP / "QuickReader.cs"), str(DESKTOP / "QuickWindowChrome.cs"), str(DESKTOP / "QuickVideoResource.cs")], check=True)
        if args.test:
            instance_tests = folder / "single-instance-tests.exe"
            compile_native(common + ["/target:exe", f"/out:{instance_tests}", str(DESKTOP / "Core.cs"),
                str(DESKTOP / "Integration.cs"), str(DESKTOP / "SingleInstanceTests.cs")], check=True)
            subprocess.run([str(instance_tests), str(exe)], check=True, timeout=35, env=test_environment)
            lifecycle = folder / "lifecycle-tests.exe"
            lifecycle_command = [value for value in command if not value.startswith('/target:') and not value.startswith('/out:')]
            compile_native(lifecycle_command + ["/target:exe", f"/out:{lifecycle}", "/main:YingXu.Desktop.LifecycleTests",
                str(DESKTOP / "Core.cs"), str(DESKTOP / "IsolatedStartup.cs"), str(DESKTOP / "Integration.cs"), str(DESKTOP / "Capture.cs"), str(DESKTOP / "Program.cs"), str(DESKTOP / "QuickReader.cs"), str(DESKTOP / "QuickWindowChrome.cs"), str(DESKTOP / "QuickVideoResource.cs"),
                str(DESKTOP / "LifecycleTests.cs")], check=True)
            subprocess.run([str(lifecycle),str(test_app_root)],check=True,timeout=30,env=test_environment)
            update_tests = folder / "incremental-install-tests.exe"
            compile_native(lifecycle_command + ["/target:exe", f"/out:{update_tests}", "/main:YingXu.Desktop.IncrementalInstallTests",
                str(DESKTOP / "Core.cs"), str(DESKTOP / "IsolatedStartup.cs"), str(DESKTOP / "Integration.cs"), str(DESKTOP / "Capture.cs"),
                str(DESKTOP / "Program.cs"), str(DESKTOP / "QuickReader.cs"), str(DESKTOP / "QuickWindowChrome.cs"), str(DESKTOP / "QuickVideoResource.cs"),
                str(DESKTOP / "IncrementalInstallTests.cs")], check=True)
            subprocess.run([str(update_tests),str(test_app_root)],check=True,timeout=30,env=test_environment)
            reader_tests = folder / "quick-reader-tests.exe"
            compile_native(lifecycle_command + ["/target:exe", f"/out:{reader_tests}", "/main:YingXu.Desktop.QuickReaderTests",
                str(DESKTOP / "Core.cs"), str(DESKTOP / "IsolatedStartup.cs"), str(DESKTOP / "Integration.cs"), str(DESKTOP / "Capture.cs"), str(DESKTOP / "Program.cs"),
                str(DESKTOP / "QuickReader.cs"), str(DESKTOP / "QuickWindowChrome.cs"), str(DESKTOP / "QuickVideoResource.cs"), str(DESKTOP / "QuickReaderTests.cs")], check=True)
            reader_result = subprocess.run([str(reader_tests), str(test_app_root), str(runtime_root), str(fixture_root)], capture_output=True, text=True, encoding='utf-8', timeout=180, env=test_environment)
            print(reader_result.stdout, end='')
            if reader_result.stderr: print(reader_result.stderr, end='')
            for line in reader_result.stdout.splitlines():
                if line.startswith('ZOOM_FIXTURE_CLEANUP_AFTER_EXIT='):
                    cleanup_integration_fixture(line.split('=', 1)[1], fixture_root)
            reader_result.check_returncode()

            if (browser_root / 'msedgewebview2.exe').is_file():
                for mode in ('--zoom-integration', '--startup-integration'):
                    integration = subprocess.run([str(lifecycle), str(test_app_root), mode,
                        str(browser_root)], capture_output=True, text=True, encoding='utf-8', timeout=60, env=test_environment)
                    print(integration.stdout, end='')
                    if integration.stderr: print(integration.stderr, end='')
                    for line in integration.stdout.splitlines():
                        if not line.startswith('ZOOM_FIXTURE_CLEANUP_AFTER_EXIT='): continue
                        cleanup_integration_fixture(line.split('=', 1)[1], fixture_root)
                    integration.check_returncode()
        if args.isolated_test_units:
            units = folder / "isolated-startup-tests.exe"
            unit_command = ["/target:exe" if item == "/target:winexe" else f"/out:{units}" if item.startswith("/out:") else item for item in command]
            compile_native(unit_command + ["/main:YingXu.Desktop.IsolatedStartupTests", str(DESKTOP / "Core.cs"), str(DESKTOP / "IsolatedStartup.cs"), str(DESKTOP / "Integration.cs"), str(DESKTOP / "Capture.cs"), str(DESKTOP / "Program.cs"), str(DESKTOP / "QuickReader.cs"), str(DESKTOP / "QuickWindowChrome.cs"), str(DESKTOP / "QuickVideoResource.cs"), str(DESKTOP / "IsolatedStartupTests.cs")], check=True)
            unit_result = subprocess.run([str(units)], check=True, capture_output=True, text=True, encoding="utf-8", timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), env=test_environment)
            print(unit_result.stdout, end="")
        metadata = require_native_identity(exe, identity)
        if read_identity(ROOT) != identity:
            raise ValueError('Application identity source changed during native compilation.')
        shutil.copy2(exe, output)
    result = {"exe": output.name, "bytes": output.stat().st_size,
              "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
              "architecture": "x64", "subsystem": "Windows GUI", "sdk": SDK_VERSION,
              "sdk_sha256": SDK_SHA256, "contains_user_data": False}
    result.update(identity.metadata())
    result.update(metadata)
    result['generated_identity_sha256'] = hashlib.sha256(generated_native_source(identity)).hexdigest()
    result['generated_manifest_sha256'] = hashlib.sha256(generated_native_manifest(DESKTOP / 'app.manifest', identity)).hexdigest()
    manifest = args.manifest_output or DESKTOP / "build.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
