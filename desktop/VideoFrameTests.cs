using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace YingXu.Desktop
{
    // End-to-end verifier for an extracted release root. The backend is started
    // and stopped by tools/verify_release_native.py with the bundle's Python.
    internal static class VideoFrameTests
    {
        private static readonly JavaScriptSerializer Json = new JavaScriptSerializer();
        private static readonly List<string> Passed = new List<string>();
        private static string evidence;
        private static int result = 1;

        private static void Check(bool condition, string name)
        {
            if (!condition) throw new InvalidOperationException("FAILED: " + name);
            Passed.Add(name);
            Console.WriteLine("PASS " + name);
        }

        private static object Field(object instance, string name)
        {
            var field = instance.GetType().GetField(name, BindingFlags.Instance | BindingFlags.NonPublic);
            if (field == null) throw new MissingFieldException(instance.GetType().FullName, name);
            return field.GetValue(instance);
        }

        private static async Task<string> Script(CoreWebView2 web, string source)
        {
            return await web.ExecuteScriptAsync(source);
        }

        private static async Task WaitFor(CoreWebView2 web, string expression, string name, int seconds)
        {
            DateTime until = DateTime.UtcNow.AddSeconds(seconds);
            string last = "";
            while (DateTime.UtcNow < until)
            {
                last = await Script(web, expression);
                if (last == "true") return;
                await Task.Delay(100);
            }
            throw new TimeoutException("Timed out waiting for " + name + "; last DOM result=" + last);
        }

        private static async Task Click(CoreWebView2 web, string selector)
        {
            string encoded = Json.Serialize(selector);
            string raw = await Script(web,
                "JSON.stringify((()=>{const n=document.querySelector(" + encoded + ");if(!n)throw Error('missing '+" + encoded + ");n.scrollIntoView({block:'center',inline:'center'});const r=n.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2}})())");
            var point = Json.Deserialize<Dictionary<string, object>>(Json.Deserialize<string>(raw));
            double x = Convert.ToDouble(point["x"]), y = Convert.ToDouble(point["y"]);
            await web.CallDevToolsProtocolMethodAsync("Input.dispatchMouseEvent",
                Json.Serialize(new { type = "mousePressed", x = x, y = y, button = "left", buttons = 1, clickCount = 1 }));
            await web.CallDevToolsProtocolMethodAsync("Input.dispatchMouseEvent",
                Json.Serialize(new { type = "mouseReleased", x = x, y = y, button = "left", buttons = 0, clickCount = 1 }));
        }

        private static async Task Key(CoreWebView2 web, string key, string code, int virtualKey)
        {
            await web.CallDevToolsProtocolMethodAsync("Input.dispatchKeyEvent",
                Json.Serialize(new { type = "keyDown", key = key, code = code,
                    windowsVirtualKeyCode = virtualKey, nativeVirtualKeyCode = virtualKey }));
            await web.CallDevToolsProtocolMethodAsync("Input.dispatchKeyEvent",
                Json.Serialize(new { type = "keyUp", key = key, code = code,
                    windowsVirtualKeyCode = virtualKey, nativeVirtualKeyCode = virtualKey }));
        }

        private static async Task<string> FindIndexSession(CoreWebView2 web)
        {
            string raw = await Script(web,
                "JSON.stringify(performance.getEntriesByType('resource').map(x=>x.name).filter(x=>x.includes('/api/video/frames/jobs/')))" );
            string encoded = Json.Deserialize<string>(raw);
            var names = Json.Deserialize<List<string>>(encoded);
            if (names != null) foreach (string name in names)
            {
                Uri uri;
                if (!Uri.TryCreate(name, UriKind.Absolute, out uri)) continue;
                string[] parts = uri.AbsolutePath.Split('/');
                if (parts.Length > 5 && parts[1] == "api" && parts[2] == "video" && parts[3] == "frames" &&
                    parts[4] == "jobs" && parts[5].Length == 32) return parts[5];
            }
            throw new InvalidDataException("The real workbench did not poll its frame-index session.");
        }

        private static async Task RunChecks(StudioWindow window, string videoPath)
        {
            var web = (WebView2)Field(window, "web");
            var core = web.CoreWebView2;
            if (core == null) throw new InvalidOperationException("Studio WebView2 controller was not initialized.");
            Check(Hub.IsLocalPage(core.Source, Hub.Url), "StudioWindow navigates to the release-root local workbench");

            // Exercise the same desktop file-open bridge used by Open With and
            // Quick Reader handoff; the API registers the fixture in the real app.
            window.OpenFromReader(new[] { videoPath }, true, null);
            await WaitFor(core,
                "(()=>{const v=document.querySelector('#editorContent .video-player video');return !!v&&v.readyState>=2&&Number.isFinite(v.duration)})()",
                "real external-video route and WebView decode", 35);
            Check(await Script(core, "document.querySelector('#editorContent video').duration>20") == "true",
                "bundled WebView decodes the backend-registered synthetic H.264 fixture");
            await Click(core, "#editorContent [data-command=play]");
            await WaitFor(core, "(()=>{const v=document.querySelector('#editorContent video');return !v.paused&&v.currentTime>.15})()",
                "actual workbench video playback", 10);
            Check(await Script(core, "!!document.querySelector('#editorContent .video-review')") == "true",
                "full workbench mounts its real review and frame controls");

            // A/B uses the actual review UI and player. Seek the native media
            // clock, then set the markers with CDP-driven button input.
            await Click(core, "#editorContent .video-review summary");
            await WaitFor(core, "document.querySelector('.review-status')?.textContent.includes('审片记录已载入')",
                "real backend review record loads", 15);
            await Script(core, "document.querySelector('#editorContent video').currentTime=.45");
            await WaitFor(core, "Math.abs(document.querySelector('#editorContent video').currentTime-.45)<.1",
                "A marker seek", 8);
            await Click(core, "#editorContent [data-review=a]");
            await Script(core, "document.querySelector('#editorContent video').currentTime=.9");
            await WaitFor(core, "Math.abs(document.querySelector('#editorContent video').currentTime-.9)<.1",
                "B marker seek", 8);
            await Click(core, "#editorContent [data-review=b]");
            await Click(core, "#editorContent [data-review=ab]");
            await WaitFor(core,
                "(()=>{const v=document.querySelector('#editorContent video');return document.querySelector('[data-review=ab]').getAttribute('aria-pressed')==='true'&&!v.paused&&v.currentTime<.7})()",
                "A/B controls loop the real synthetic video", 10);
            Check(true, "CDP clicks activate the workbench A/B loop");
            await Click(core, "#editorContent [data-review=ab]");

            // The strict index is a real POST to the package backend. Its
            // worker decodes the source while the browser clock keeps moving.
            await Script(core, "document.querySelector('#editorContent video').currentTime=1.2");
            await WaitFor(core, "Math.abs(document.querySelector('#editorContent video').currentTime-1.2)<.1",
                "position video before background indexing", 8);
            double before = Convert.ToDouble(await Script(core, "document.querySelector('#editorContent video').currentTime"));
            await Click(core, "#editorContent [data-frame=start]");
            await Task.Delay(450);
            bool remainedPlaying = await Script(core,
                "(()=>{const v=document.querySelector('#editorContent video');return !v.paused&&v.currentTime>" +
                before.ToString(System.Globalization.CultureInfo.InvariantCulture) + "+.15})()") == "true";
            await WaitFor(core, "document.querySelector('.frame-status')?.textContent.includes('后台索引已就绪')",
                "real backend frame index completes", 150);
            Check(remainedPlaying && await Script(core, "!document.querySelector('#editorContent video').paused") == "true",
                "real backend indexing and completion do not pause or reset playback");
            string status = Json.Deserialize<string>(await Script(core, "document.querySelector('.frame-status').textContent"));
            Check(status.Contains("后台索引已就绪") && status.Contains("帧"), "frame index readiness and decoded frame count are shown in the real UI");
            string indexSession = await FindIndexSession(core);
            var indexedState = DesktopApi.Request("/api/video/frames/jobs/" + indexSession);
            object indexedJobState, indexedKind, indexedSessionValue;
            Check(indexedState.TryGetValue("state", out indexedJobState) && (indexedJobState as string) == "ready" &&
                  indexedState.TryGetValue("kind", out indexedKind) && (indexedKind as string) == "index" &&
                  indexedState.TryGetValue("session", out indexedSessionValue) && (indexedSessionValue as string) == indexSession,
                "real backend confirms the indexed session is ready before still-frame inspection");

            await Click(core, "#editorContent [data-frame=enter]");
            await WaitFor(core,
                "(()=>{const v=document.querySelector('#editorContent video'),o=document.querySelector('.frame-overlay');return !!o&&!o.hidden&&v.paused&&document.querySelector('.frame-overlay-label').textContent.includes('解码静帧 0 /')&&document.querySelector('.frame-overlay img').naturalWidth>0})()",
                "explicit entry pauses and paints a backend-decoded PNG", 45);
            Check(await Script(core, "document.querySelector('.frame-overlay img').naturalWidth>=160") == "true",
                "decoded still image is displayed in the package WebView");

            await Click(core, "#editorContent [data-frame=next]");
            await WaitFor(core, "document.querySelector('.frame-overlay-label')?.textContent.includes('解码静帧 1 /')",
                "next-frame CDP click", 30);
            await Click(core, "#editorContent [data-frame=previous]");
            await WaitFor(core, "document.querySelector('.frame-overlay-label')?.textContent.includes('解码静帧 0 /')",
                "previous-frame CDP click", 30);
            await Click(core, "#editorContent [data-frame=next]");
            await WaitFor(core, "document.querySelector('.frame-overlay-label')?.textContent.includes('解码静帧 1 /')",
                "next-frame restored after previous", 30);

            await Script(core, "document.querySelector('.video-screen').focus({preventScroll:true})");
            await Key(core, ",", "Comma", 188);
            await WaitFor(core, "document.querySelector('.frame-overlay-label')?.textContent.includes('解码静帧 0 /')",
                "comma keyboard shortcut steps backward", 30);
            await Key(core, ".", "Period", 190);
            await WaitFor(core, "document.querySelector('.frame-overlay-label')?.textContent.includes('解码静帧 1 /')",
                "period keyboard shortcut steps forward", 30);
            Check(await Script(core, "document.querySelector('#editorContent video').paused") == "true",
                "keyboard frame steps keep explicit still inspection paused");

            string screenshot = Path.Combine(evidence, "strict-frame-webview.png");
            using (var output = new FileStream(screenshot, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                await core.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, output);
            Check(new FileInfo(screenshot).Length > 2048, "real Studio WebView strict-frame state captured at " + Path.GetFileName(screenshot));

            await Click(core, "#editorContent [data-frame=resume]");
            await WaitFor(core,
                "(()=>document.querySelector('.frame-overlay').hidden&&!document.querySelector('#editorContent video').paused)()",
                "return from the decoded still to playing video", 15);
            var resumedIndexState = DesktopApi.Request("/api/video/frames/jobs/" + indexSession);
            object resumedJobState;
            Check(resumedIndexState.TryGetValue("state", out resumedJobState) && (resumedJobState as string) == "ready",
                "return-to-video preserves the real ready index for another inspection");
            await Click(core, "#editorContent [data-frame=cancel]");
            await WaitFor(core, "document.querySelector('.frame-status')?.textContent.includes('后台索引与帧检查已关闭')",
                "close/cancel frame inspection in the real UI", 20);
            var backendState = DesktopApi.Request("/api/video/frames/jobs/" + indexSession);
            object state;
            Check(backendState.TryGetValue("state", out state) && (state as string) == "cancelled",
                "real backend confirms the UI cancelled this ready index session and released it");
            Check(await Script(core, "!document.querySelector('#editorContent video').paused") == "true",
                "closing frame inspection leaves regular playback active");

            Console.WriteLine("NATIVE_FRAME_TESTS=" + Json.Serialize(Passed));
            result = 0;
        }

        private static void Close(StudioWindow window)
        {
            if (window == null || window.IsDisposed) return;
            var field = window.GetType().GetField("exitApproved", BindingFlags.Instance | BindingFlags.NonPublic);
            if (field != null) field.SetValue(window, true);
            window.Close();
        }

        [STAThread]
        private static int Main(string[] args)
        {
            if (args.Length != 8) throw new ArgumentException("Expected release root, runtime root, fixture root, data root, projects root, port, video path and evidence root.");
            string root = Path.GetFullPath(args[0]);
            string runtime = Path.GetFullPath(args[1]);
            string testRoot = Path.GetFullPath(args[2]);
            string data = Path.GetFullPath(args[3]);
            string projects = Path.GetFullPath(args[4]);
            int port = Int32.Parse(args[5], System.Globalization.CultureInfo.InvariantCulture);
            string video = Path.GetFullPath(args[6]);
            evidence = Path.GetFullPath(args[7]);
            if (!Directory.Exists(root) || !Directory.Exists(runtime) || !Directory.Exists(testRoot) || !Directory.Exists(evidence) ||
                !File.Exists(Path.Combine(root, "server.py")) || !File.Exists(Path.Combine(root, "launcher.pyw")) ||
                !File.Exists(Path.Combine(runtime, "webview2", "msedgewebview2.exe")) ||
                !File.Exists(Path.Combine(runtime, "ffmpeg", "bin", "ffmpeg.exe")) ||
                !video.StartsWith(testRoot.TrimEnd('\\', '/') + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase) ||
                !data.StartsWith(testRoot.TrimEnd('\\', '/') + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase) ||
                !projects.StartsWith(testRoot.TrimEnd('\\', '/') + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase) ||
                !evidence.StartsWith(testRoot.TrimEnd('\\', '/') + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase) ||
                IsolatedStartup.Overlaps(root, testRoot) || IsolatedStartup.Overlaps(data, projects) ||
                IsolatedStartup.Overlaps(data, evidence) || IsolatedStartup.Overlaps(projects, evidence))
                throw new InvalidDataException("Native frame harness paths are missing, linked, or overlapping.");
            Directory.CreateDirectory(data); Directory.CreateDirectory(projects);
            Environment.SetEnvironmentVariable("YINGXU_DATA_DIR", data);
            Environment.SetEnvironmentVariable("YINGXU_PROJECTS_DIR", projects);
            Environment.SetEnvironmentVariable("YINGXU_WEBVIEW2_MODE", "bundled");
            Environment.SetEnvironmentVariable("YINGXU_PYTHON", null);
            Hub.Root = root; Hub.Data = data; Hub.Cache = Path.Combine(data, "desktop");
            Hub.Port = port; Hub.Url = "http://127.0.0.1:" + port + "/";
            Program.Isolated = null; Program.IncrementalInstalling = false;
            Program.InitialFiles = new string[0]; Program.InstanceKey = Guid.NewGuid().ToString("N");
            Program.ActivateEvent = new EventWaitHandle(false, EventResetMode.AutoReset);
            try
            {
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                typeof(Program).GetMethod("PrepareLibraries", BindingFlags.Static | BindingFlags.NonPublic).Invoke(null, null);
                CoreWebView2Environment.SetLoaderDllFolderPath(Program.LoaderFolder);
                using (var window = new StudioWindow(initialize: true, routed: true))
                using (var startup = new System.Windows.Forms.Timer { Interval = 100 })
                using (var deadline = new System.Windows.Forms.Timer { Interval = 240000 })
                {
                    bool started = false;
                    startup.Tick += async delegate
                    {
                        if (started || window.IsDisposed || !(bool)Field(window, "pageReady")) return;
                        started = true; startup.Stop(); deadline.Stop();
                        try { await RunChecks(window, video); }
                        catch (Exception error)
                        {
                            Console.WriteLine("NATIVE_FRAME_ERROR " + error.GetType().Name + ": " + error.Message);
                            result = 1;
                        }
                        finally { Close(window); }
                    };
                    deadline.Tick += delegate
                    {
                        deadline.Stop(); startup.Stop();
                        Console.WriteLine("NATIVE_FRAME_ERROR TimeoutException: StudioWindow did not become ready.");
                        result = 1; Close(window);
                    };
                    window.Shown += delegate { startup.Start(); deadline.Start(); };
                    Application.Run(window);
                }
            }
            finally { if (Program.ActivateEvent != null) Program.ActivateEvent.Dispose(); }
            return result;
        }
    }
}
