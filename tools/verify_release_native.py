"""Run the extracted Windows release root in bundled Python and WebView2.

The native harness opens only a generated video under an owned test directory.
It starts the backend from the extracted package root and exercises the strict
frame UI through StudioWindow's actual WebView2 host.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import uuid
import zipfile

TOOL_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_ROOT))
from build_identity import generated_native_source, read_identity, require_native_identity

SDK_SHA256 = "f492bbf547d0da329553b6727435b677579b1e9f91cc9e4a1ad029366d5f23d0"


def ordinary_directory(path: Path, name: str) -> Path:
    original = Path(os.path.abspath(path))
    try:
        attributes = original.stat(follow_symlinks=False).st_file_attributes
    except OSError as error:
        raise ValueError(f"{name} must be an existing ordinary directory: {original}") from error
    if original.is_symlink() or attributes & 0x400 or not original.is_dir():
        raise ValueError(f"{name} must be an existing ordinary directory: {original}")
    return original.resolve(strict=True)


def overlaps(first: Path, second: Path) -> bool:
    first = first.resolve().as_posix().casefold().rstrip("/")
    second = second.resolve().as_posix().casefold().rstrip("/")
    return first == second or first.startswith(second + "/") or second.startswith(first + "/")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def http_json(port: int, method: str, path: str, data=None):
    import http.client

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=6)
    headers = {}
    body = None
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        if response.status >= 400:
            raise RuntimeError(f"{method} {path} returned HTTP {response.status}: {raw[:512]!r}")
        return json.loads(raw)
    finally:
        connection.close()


def wait_health(port: int, process: subprocess.Popen | None) -> dict:
    until = time.monotonic() + 30
    last = None
    while time.monotonic() < until:
        try:
            health = http_json(port, "GET", "/api/health")
            if health.get("ok") is True and health.get("app") == "yingxu":
                return health
        except (OSError, ValueError, RuntimeError) as error:
            last = error
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"Bundled backend exited before health check: {last}")
        time.sleep(0.1)
    raise TimeoutError(f"Bundled backend health timeout: {last}")


def write_server_runner(path: Path) -> None:
    path.write_text(
        """import argparse, sys, threading
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument('--root',required=True); p.add_argument('--data',required=True); p.add_argument('--projects',required=True); p.add_argument('--port',type=int,required=True); a=p.parse_args()
root=Path(a.root).resolve(); sys.path.insert(0,str(root)); sys.path.insert(0,str(root/'runtime'/'site-packages')); __import__('os').chdir(root)
from server import Application, Server
app=Application(Path(a.data),Path(a.projects)); server=Server(('127.0.0.1',a.port),app)
thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':0.2},daemon=True); thread.start()
print('NATIVE_BACKEND_READY',flush=True)
try:
  for line in sys.stdin:
    if line.strip()=='shutdown': break
finally:
  server.shutdown(); thread.join(timeout=15); server.server_close(); app.close()
print('NATIVE_BACKEND_CLOSED',flush=True)
""",
        encoding="utf-8",
        newline="\n",
    )


def create_video(runtime: Path, destination: Path) -> dict:
    ffmpeg = runtime / "ffmpeg" / "bin" / "ffmpeg.exe"
    if not ffmpeg.is_file():
        raise FileNotFoundError(f"The extracted release runtime has no FFmpeg: {ffmpeg}")
    command = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25", "-t", "30",
        "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "29",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(destination),
    ]
    result = subprocess.run(command, capture_output=True, timeout=60, check=False,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode or not destination.is_file() or destination.stat().st_size < 4096:
        details = result.stderr.decode("utf-8", errors="replace")[-2000:]
        raise RuntimeError(f"Bundled FFmpeg failed to create the synthetic video: {details}")
    return {"path": str(destination), "bytes": destination.stat().st_size, "frames": 750,
            "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}


def compile_harness(release_root: Path, sdk_package: Path, build_dir: Path, identity) -> Path:
    if sys.platform != "win32":
        raise RuntimeError("The native release verifier requires Windows.")
    raw = sdk_package.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SDK_SHA256:
        raise ValueError("WebView2 SDK archive SHA-256 does not match the pinned official package.")
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    compiler = windir / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
    if not compiler.is_file():
        raise FileNotFoundError(f"The Windows .NET Framework C# compiler is missing: {compiler}")
    generated_identity = build_dir / "BuildIdentity.cs"
    generated_identity.write_bytes(generated_native_source(identity))
    members = {
        "Microsoft.Web.WebView2.Core.dll": "lib/net462/Microsoft.Web.WebView2.Core.dll",
        "Microsoft.Web.WebView2.WinForms.dll": "lib/net462/Microsoft.Web.WebView2.WinForms.dll",
        "WebView2Loader.dll": "runtimes/win-x64/native/WebView2Loader.dll",
    }
    with zipfile.ZipFile(sdk_package) as archive:
        for name, member in members.items():
            (build_dir / name).write_bytes(archive.read(member))
    output = build_dir / "release-native-video-tests.exe"
    command = [
        str(compiler), "/nologo", "/optimize+", "/platform:x64", "/utf8output",
        "/target:exe", f"/out:{output}", "/main:YingXu.Desktop.VideoFrameTests",
        "/reference:System.dll", "/reference:System.Core.dll", "/reference:System.Web.Extensions.dll",
        "/reference:System.Drawing.dll", "/reference:System.Windows.Forms.dll",
        f"/reference:{build_dir / 'Microsoft.Web.WebView2.Core.dll'}",
        f"/reference:{build_dir / 'Microsoft.Web.WebView2.WinForms.dll'}",
        f"/resource:{release_root / 'desktop' / 'brand.ico'},brand.ico",
        f"/resource:{build_dir / 'Microsoft.Web.WebView2.Core.dll'},Microsoft.Web.WebView2.Core.dll",
        f"/resource:{build_dir / 'Microsoft.Web.WebView2.WinForms.dll'},Microsoft.Web.WebView2.WinForms.dll",
        f"/resource:{build_dir / 'WebView2Loader.dll'},WebView2Loader.dll",
    ]
    for relative in (
        "desktop/Core.cs", "desktop/IsolatedStartup.cs", "desktop/Integration.cs",
        "desktop/Capture.cs", "desktop/Program.cs", "desktop/QuickReader.cs",
        "desktop/QuickWindowChrome.cs", "desktop/QuickVideoResource.cs",
        "desktop/VideoFrameTests.cs",
    ):
        command.append(str(release_root / relative))
    command.append(str(generated_identity))
    result = subprocess.run(command, cwd=release_root, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=60, check=False)
    if result.returncode:
        raise RuntimeError("Native WebView harness compilation failed:\n" + result.stdout + result.stderr)
    if not output.is_file():
        raise RuntimeError("C# compiler returned success without creating the native harness.")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="The extracted final Windows application root")
    parser.add_argument("--sdk-package", required=True, type=Path, help="Pinned official WebView2 SDK .nupkg")
    parser.add_argument("--fixture-root", required=True, type=Path, help="Existing isolated test-output directory outside source and app data")
    parser.add_argument("--timeout", type=int, default=260, help="Maximum seconds for the real WebView/backend flow")
    args = parser.parse_args()

    root = ordinary_directory(args.root, "Release root")
    fixture_parent = ordinary_directory(args.fixture_root, "Native fixture root")
    runtime = ordinary_directory(root / "runtime", "Bundled runtime")
    sdk = args.sdk_package.resolve(strict=True)
    if overlaps(root, fixture_parent) or overlaps(root, Path.home() / "Documents" / "YingXu" / "Projects"):
        raise ValueError("The package root overlaps a test or protected project directory.")
    required_release_files = (
        "YingXu.exe", "server.py", "launcher.pyw", "frontend/index.html",
        "desktop/brand.ico", "desktop/Core.cs", "desktop/IsolatedStartup.cs",
        "desktop/Integration.cs", "desktop/Capture.cs", "desktop/Program.cs",
        "desktop/QuickReader.cs", "desktop/QuickWindowChrome.cs",
        "desktop/QuickVideoResource.cs", "desktop/VideoFrameTests.cs",
        "yingxu/__init__.py",
    )
    missing = [relative for relative in required_release_files if not (root / relative).is_file()]
    if missing:
        raise ValueError("The selected release root is missing packaged application files: " + ", ".join(missing))
    if not (runtime / "webview2" / "msedgewebview2.exe").is_file() or not (runtime / "python.exe").is_file():
        raise ValueError("The extracted package runtime is missing bundled WebView2 or Python.")
    identity = read_identity(root)
    native_identity = require_native_identity(root / "YingXu.exe", identity)
    if not 180 <= args.timeout <= 600:
        raise ValueError("Native harness timeout must be between 180 and 600 seconds.")
    for protected in (
        Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "YingXu",
        Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Documents" / "YingXu" / "Projects",
    ):
        if overlaps(fixture_parent, protected):
            raise ValueError("Native fixtures overlap protected application data or projects.")

    test_root = fixture_parent / ("native-video-" + uuid.uuid4().hex)
    test_root.mkdir()
    data = test_root / "data"
    projects = test_root / "projects"
    evidence = test_root / "evidence"
    profile = test_root / "user-profile"
    build_dir = test_root / "build"
    for directory in (data, projects, evidence, profile, build_dir):
        directory.mkdir()
    video = test_root / "synthetic-cfr-25fps.mp4"
    runner_script = test_root / "bundled-server-runner.py"
    port = free_port()
    environment = os.environ.copy()
    environment.update(
        YINGXU_DATA_DIR=str(data), YINGXU_PROJECTS_DIR=str(projects),
        YINGXU_WEBVIEW2_MODE="bundled", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1",
        USERPROFILE=str(profile), HOME=str(profile), LOCALAPPDATA=str(test_root / "local"),
        APPDATA=str(test_root / "roaming"), TEMP=str(test_root / "temp"),
        TMP=str(test_root / "temp"), TMPDIR=str(test_root / "temp"),
        PATH=str(Path(os.environ["WINDIR"]) / "System32"),
    )
    for variable in ("YINGXU_PYTHON", "YINGXU_RESUME_SESSION_TOKEN", "PYTHONHOME", "PYTHONPATH"):
        environment.pop(variable, None)
    Path(environment["TEMP"]).mkdir()
    Path(environment["APPDATA"]).mkdir()
    Path(environment["LOCALAPPDATA"]).mkdir()
    write_server_runner(runner_script)
    preflight_id = str(uuid.uuid4())
    (test_root / ".yingxu-isolated-owner.json").write_text(json.dumps({
        "schema": "yingxu-native-isolated/1", "test_id": preflight_id,
        "program_root": str(root), "data_root": str(data),
        "projects_root": str(projects),
    }, ensure_ascii=False), encoding="utf-8")
    fixture_metadata = create_video(runtime, video)
    harness = compile_harness(root, sdk, build_dir, identity)
    record = {
        "schema": "yingxu-native-frame-verification/1", "release_root": str(root),
        "runtime_root": str(runtime), "version": identity.version, "build_revision": identity.build_revision,
        "native_identity": native_identity,
        "test_root": str(test_root), "data_root": str(data), "projects_root": str(projects),
        "port": port, "fixture": fixture_metadata, "checks": [], "private_data_used": False,
    }
    backend_log_path = evidence / "backend.log"
    backend_log = backend_log_path.open("ab", buffering=0)
    server = None
    browser_probe = None
    backend_closed = False
    harness_result = None
    try:
        # IsolatedStartup intentionally rejects a WebView directory nested in
        # the application root. Preflight does not create a controller, so copy
        # only the package's browser executable to a sibling probe directory.
        # The actual WebView acceptance below uses the complete packaged runtime.
        browser_probe = fixture_parent / ("native-browser-probe-" + uuid.uuid4().hex)
        browser_probe.mkdir()
        shutil.copyfile(runtime / "webview2" / "msedgewebview2.exe",
                        browser_probe / "msedgewebview2.exe")
        preflight_command = [
            str(root / "YingXu.exe"), "--isolated-test", "--root", str(root),
            "--test-root", str(test_root), "--data", str(data),
            "--projects", str(projects), "--evidence-root", str(evidence),
            "--webview-root", str(browser_probe), "--port", str(port),
            "--test-id", preflight_id, "--preflight-only",
        ]
        (evidence / "main-exe-preflight-command.json").write_text(
            json.dumps(preflight_command, ensure_ascii=False, indent=2), encoding="utf-8")
        server = subprocess.Popen(
            [str(runtime / "python.exe"), "-B", "-u", str(runner_script), "--root", str(root),
             "--data", str(data), "--projects", str(projects), "--port", str(port)],
            cwd=root, env=environment, stdin=subprocess.PIPE, stdout=backend_log,
            stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        health = wait_health(port, server)
        if health.get("version") != identity.version or health.get("build_revision") != identity.build_revision:
            raise RuntimeError("Bundled backend identity differs from the extracted release.")
        bootstrap = http_json(port, "GET", "/api/bootstrap")
        if Path(bootstrap.get("data_root", "")).resolve() != data.resolve() or Path(bootstrap.get("project_root", "")).resolve() != projects.resolve():
            raise RuntimeError("Bundled backend did not use the isolated data and project roots.")
        token = bootstrap.get("token")
        if not isinstance(token, str) or len(token) < 40:
            raise RuntimeError("Bundled backend did not issue an isolated write token.")
        import http.client
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=6)
        try:
            body = json.dumps({"capture_enabled": False, "automatic_update_check": False,
                               "automatic_update_download": False}, ensure_ascii=False).encode("utf-8")
            connection.request("PATCH", "/api/settings", body=body, headers={
                "Origin": f"http://127.0.0.1:{port}", "X-YingXu-Token": token,
                "Content-Type": "application/json",
            })
            response = connection.getresponse()
            setting_result = json.loads(response.read())
            if response.status >= 400 or setting_result.get("capture_enabled") is not False:
                raise RuntimeError("Could not disable global capture/update actions in the isolated backend.")
        finally:
            connection.close()
        record["checks"].append("final-root server.py served from bundled Python with isolated data/projects and no real user profile scan")

        preflight = subprocess.run(preflight_command, cwd=root, env=environment, capture_output=True,
                                   timeout=45, check=False,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        preflight_output = preflight.stdout.decode("utf-8", errors="replace")
        preflight_error = preflight.stderr.decode("utf-8", errors="replace")
        preflight_record = evidence / "preflight.json"
        if preflight.returncode != 0 or not preflight_record.is_file():
            raise RuntimeError("Packaged main EXE isolated preflight failed: " +
                               (preflight_output + preflight_error)[-2000:])
        preflight_json = json.loads(preflight_record.read_text(encoding="utf-8"))
        if (preflight_json.get("state") != "preflight_passed" or
                preflight_json.get("version") != identity.version or
                preflight_json.get("build_revision") != identity.build_revision or
                preflight_json.get("root") != str(root) or
                preflight_json.get("data") != str(data) or
                preflight_json.get("isolated") is not True or
                preflight_json.get("backend_started") is not False):
            raise RuntimeError("Packaged main EXE isolated preflight returned an unexpected identity or state.")
        record["checks"].append("packaged YingXu.exe passed --isolated-test preflight and root/data identity against the isolated backend; preflight-only did not launch a WebView controller")

        command = [str(harness), str(root), str(runtime), str(test_root), str(data), str(projects),
                   str(port), str(video), str(evidence)]
        (evidence / "native-command.json").write_text(json.dumps(command, ensure_ascii=False, indent=2), encoding="utf-8")
        with (evidence / "native-harness.log").open("wb") as log:
            process = subprocess.Popen(command, cwd=root, env=environment, stdout=log, stderr=subprocess.STDOUT,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                code = process.wait(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    code = process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill(); code = process.wait(timeout=8)
                raise TimeoutError(f"Native WebView/strict-frame harness timed out after {args.timeout} seconds; see {evidence / 'native-harness.log'}")
        harness_result = code
        log_text = (evidence / "native-harness.log").read_text(encoding="utf-8", errors="replace")
        if code != 0:
            raise RuntimeError(f"Native WebView/strict-frame harness returned {code}; see {evidence / 'native-harness.log'}")
        if "NATIVE_FRAME_TESTS=" not in log_text or "strict-frame-webview.png" not in log_text:
            raise RuntimeError("Native harness did not report its complete WebView checks and capture.")
        record["checks"].append("StudioWindow WebView playback, A/B, real backend frame index, pause/still/steps/keyboard/resume/cancel")
        record["checks"].append("CapturePreviewAsync strict-frame screenshot written from the full desktop WebView")
    finally:
        if server is not None and server.poll() is None:
            try:
                server.stdin.write(b"shutdown\n"); server.stdin.flush()
                server.wait(timeout=35)
                backend_closed = server.returncode == 0
            except Exception:
                server.terminate()
                try: server.wait(timeout=8)
                except subprocess.TimeoutExpired: server.kill(); server.wait(timeout=8)
            finally:
                if server.stdin: server.stdin.close()
        backend_log.close()
        if browser_probe is not None:
            shutil.rmtree(browser_probe)
        record["backend_closed_gracefully"] = backend_closed
        record["harness_exit_code"] = harness_result
        (evidence / "native-report.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    if not backend_closed:
        raise RuntimeError(f"Bundled backend did not shut down cleanly; see {backend_log_path}")
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"NATIVE_RELEASE_VERIFY_ERROR {type(error).__name__}: {error}", file=sys.stderr)
        raise
