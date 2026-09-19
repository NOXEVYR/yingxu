using System;
using System.Collections.Generic;
using System.Drawing;
using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.IO;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace YingXu.Desktop
{
    internal static class LifecycleTests
    {
        private static int count;
        [DllImport("user32.dll", CharSet=CharSet.Auto)]
        private static extern IntPtr SendMessage(IntPtr window,int message,IntPtr wParam,IntPtr lParam);
        private static bool SameIcon(Icon left,Icon right)
        {
            using(var a=new Bitmap(left.Width,left.Height)) using(var b=new Bitmap(right.Width,right.Height))
            {
                if(a.Size!=b.Size)return false;
                using(var g=Graphics.FromImage(a)){g.Clear(Color.White);g.DrawIconUnstretched(left,new Rectangle(Point.Empty,a.Size));}
                using(var g=Graphics.FromImage(b)){g.Clear(Color.White);g.DrawIconUnstretched(right,new Rectangle(Point.Empty,b.Size));}
                for(int y=0;y<a.Height;y++)for(int x=0;x<a.Width;x++)
                    if(a.GetPixel(x,y)!=b.GetPixel(x,y))
                    {
                        Console.WriteLine("Icon difference "+x+","+y+": "+a.GetPixel(x,y)+" vs "+b.GetPixel(x,y));
                        return false;
                    }
                return true;
            }
        }
        private static void Check(bool value,string name)
        {
            if (!value) { Console.WriteLine("FAILED: " + name); throw new Exception("FAILED: " + name); }
            count++; Console.WriteLine("PASS " + name);
        }
        private static object Field(object target,string name)
        {
            return target.GetType().GetField(name,BindingFlags.Instance|BindingFlags.NonPublic).GetValue(target);
        }
        private static void Field(object target,string name,object value)
        {
            target.GetType().GetField(name,BindingFlags.Instance|BindingFlags.NonPublic).SetValue(target,value);
        }
        private static object Call(object target,string name,params object[] args)
        {
            return target.GetType().GetMethod(name,BindingFlags.Instance|BindingFlags.NonPublic).Invoke(target,args);
        }
        [STAThread]
        private static int Main(string[] args)
        {
            string directory = Path.Combine(Path.GetTempPath(),"yingxu-lifecycle-"+Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(directory);
            try
            {
                Hub.Root = args[0]; Hub.Data = directory; Hub.Cache = directory;
                Hub.Port = 1; Hub.Url = "http://127.0.0.1:1/";
                Program.InitialFiles = new string[0]; Program.InstanceKey = Guid.NewGuid().ToString("N");
                Program.ActivateEvent = new EventWaitHandle(false,EventResetMode.AutoReset);
                typeof(Program).GetMethod("PrepareLibraries",BindingFlags.Static|BindingFlags.NonPublic).Invoke(null,null);
                Application.EnableVisualStyles();
                if(args.Length==3 && args[1]=="--zoom-integration") RunZoomIntegration(args[2]);
                else if(args.Length==3 && args[1]=="--startup-integration") RunStartupIntegration(args[2]);
                else Run();
                Console.WriteLine("Desktop lifecycle tests passed: " + count);
                return 0;
            }
            finally
            {
                if (Program.ActivateEvent != null) Program.ActivateEvent.Dispose();
                string resolved = Path.GetFullPath(directory);
                if (!resolved.StartsWith(Path.GetFullPath(Path.GetTempPath()),StringComparison.OrdinalIgnoreCase) ||
                    !Path.GetFileName(resolved).StartsWith("yingxu-lifecycle-",StringComparison.Ordinal) ||
                    (File.GetAttributes(resolved)&FileAttributes.ReparsePoint)!=0) throw new IOException("Invalid fixture cleanup path");
                // Native WebView interop assemblies can remain mapped until this
                // process exits. The explicit integration runner cleans its own
                // printed fixture path only after observing process completion.
                if(args.Length==3 && (args[1]=="--zoom-integration" || args[1]=="--startup-integration")) Console.WriteLine("ZOOM_FIXTURE_CLEANUP_AFTER_EXIT="+resolved);
                else Directory.Delete(resolved,true);
            }
        }
        private static void PumpStartup(Task work)
        {
            DateTime deadline=DateTime.UtcNow.AddSeconds(25);
            while(!work.IsCompleted && DateTime.UtcNow<deadline) { Application.DoEvents();Thread.Sleep(10); }
            if(!work.IsCompleted) throw new TimeoutException("Isolated startup integration timed out");
            work.GetAwaiter().GetResult();
        }
        [MethodImpl(MethodImplOptions.NoInlining)]
        private static void RunStartupIntegration(string browserFolder)
        {
            string source=Hub.Root;
            string fixture=Path.Combine(Hub.Data,"app");
            Directory.CreateDirectory(Path.Combine(fixture,"yingxu"));
            File.Copy(Path.Combine(source,"launcher.pyw"),Path.Combine(fixture,"launcher.pyw"));
            foreach(string name in new[]{"__init__.py","paths.py"})
                File.Copy(Path.Combine(source,"yingxu",name),Path.Combine(fixture,"yingxu",name));
            File.WriteAllText(Path.Combine(fixture,"server.py"),
                "import json,sys,threading,time\nfrom pathlib import Path\nfrom http.server import BaseHTTPRequestHandler,HTTPServer\n"+
                "from yingxu.paths import instance_id,default_data_root\n"+
                "class Handler(BaseHTTPRequestHandler):\n"+
                " def do_GET(self):\n"+
                "  health=self.path=='/api/health'\n"+
                "  body=(json.dumps(dict(app='yingxu',ok=True,version='0.4.16',instance_id=instance_id(default_data_root()))) if health else '<!doctype html><meta charset=utf-8><p id=fixture>YingXu startup fixture</p>').encode()\n"+
                "  self.send_response(200);self.send_header('Content-Type','application/json' if health else 'text/html');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)\n"+
                " def log_message(self,*args): pass\n"+
                "server=HTTPServer(('127.0.0.1',int(sys.argv[sys.argv.index('--port')+1])),Handler)\n"+
                "def stop():\n"+
                " deadline=time.monotonic()+30\n"+
                " while time.monotonic()<deadline and not Path('stop-fixture').exists():time.sleep(.05)\n"+
                " server.shutdown()\n"+
                "threading.Thread(target=stop,daemon=True).start()\nserver.serve_forever();server.server_close()\n");
            var probe=new TcpListener(IPAddress.Loopback,0);probe.Start();Hub.Port=((IPEndPoint)probe.LocalEndpoint).Port;probe.Stop();
            Hub.Root=fixture;Hub.Url="http://127.0.0.1:"+Hub.Port+"/";
            Environment.SetEnvironmentVariable("YINGXU_PROJECTS_DIR",Path.Combine(Hub.Data,"projects"));
            Environment.SetEnvironmentVariable("WEBVIEW2_BROWSER_EXECUTABLE_FOLDER",Path.GetFullPath(browserFolder));
            Environment.SetEnvironmentVariable("WEBVIEW2_USER_DATA_FOLDER",Path.Combine(Hub.Cache,"WebView2"));
            Environment.SetEnvironmentVariable("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS","--disable-background-networking --no-first-run");
            CoreWebView2Environment.SetLoaderDllFolderPath(Program.LoaderFolder);
            try
            {
                using(var window=new StudioWindow(false))
                {
                    ((NotifyIcon)Field(window,"tray")).Visible=false;
                    try { PumpStartup(CheckStartup(window)); }
                    finally { Field(window,"exitApproved",true);window.Close(); }
                }
                // Exercise a close while browser/environment initialization is still
                // asynchronous. Completion must not navigate a disposed controller.
                using(var window=new StudioWindow(false))
                {
                    ((NotifyIcon)Field(window,"tray")).Visible=false;
                    var work=(Task)Call(window,"InitializeAsync");
                    Field(window,"exitApproved",true);window.Close();
                    PumpStartup(work);
                    Check(!(bool)Field(window,"loaded"),"close during startup does not navigate or revive the window");
                }
            }
            finally
            {
                File.WriteAllText(Path.Combine(fixture,"stop-fixture"),"");
                string record=Path.Combine(Hub.Data,"server.pid.json");
                if(File.Exists(record))
                {
                    var data=new JavaScriptSerializer().Deserialize<Dictionary<string,object>>(File.ReadAllText(record));
                    try { using(var process=Process.GetProcessById((int)data["pid"])) Check(process.WaitForExit(5000),"owned synthetic startup backend exits"); }
                    catch(ArgumentException) { }
                }
            }
        }
        private static async Task CheckStartup(StudioWindow window)
        {
            var elapsed=Stopwatch.StartNew();
            await (Task)Call(window,"InitializeAsync");
            var web=(WebView2)Field(window,"web");
            Check(web!=null && web.CoreWebView2!=null,"startup initializes a real WebView controller");
            Check(Hub.Healthy(Hub.Port),"navigation waits for validated isolated backend");
            for(int i=0;i<200 && !(bool)Field(window,"loaded");i++)await Task.Delay(25);
            Check((bool)Field(window,"loaded"),"new startup path completes real HTTP navigation");
            string body=await web.CoreWebView2.ExecuteScriptAsync("document.getElementById('fixture').textContent");
            Check(body=="\"YingXu startup fixture\"","isolated HTML fixture renders through production startup path");
            Check(!web.CoreWebView2.Settings.AreHostObjectsAllowed && !web.CoreWebView2.Settings.AreDevToolsEnabled,
                "parallel startup preserves WebView restrictions");
            Check(!window.Visible,"startup integration never opens the user-facing window");
            // The minimal HTTP fixture intentionally has no application bootstrap.
            Field(window,"pageReady",true);
            await CheckDroppedFileBridge(web);
            string log=File.ReadAllText(Path.Combine(Hub.Data,"desktop.log"));
            int service=log.IndexOf("startup_stage=service_ready"),browser=log.IndexOf("startup_stage=browser_ready"),navigate=log.IndexOf("startup_stage=navigate");
            Check(service>=0 && browser>=0 && navigate>service && navigate>browser,"both startup branches finish before navigation");
            foreach(string line in log.Split('\n'))if(line.Contains("startup_stage="))Console.WriteLine("STARTUP_INTEGRATION "+line.Trim());
            Console.WriteLine("STARTUP_INTEGRATION total_ms="+elapsed.ElapsedMilliseconds);
            var exited=new TaskCompletionSource<bool>();
            web.CoreWebView2.Environment.BrowserProcessExited+=(sender,args)=>exited.TrySetResult(true);
            Field(window,"exitApproved",true);window.Close();
            for(int i=0;i<200 && !exited.Task.IsCompleted;i++)await Task.Delay(25);
            Check(exited.Task.IsCompleted,"startup fixture browser exits after approved close");
        }
        private static async Task CheckDroppedFileBridge(WebView2 web)
        {
            // Real runtime File -> AdditionalObjects -> CoreWebView2File mapping, using only
            // synthetic temporary files. CDP populates a file input without a dialog.
            // This tests the production bridge, not a fabricated .NET event argument.
            var json=new JavaScriptSerializer();
            string first=Path.Combine(Hub.Data,"拖入合成 图片.png");
            string second=Path.Combine(Hub.Data,"拖入合成 第二张.jpg");
            File.WriteAllText(first,"Synthetic drop metadata fixture one");
            File.WriteAllText(second,"Synthetic drop metadata fixture two");
            const string requestId="1234567890abcdef1234567890abcdef";
            string observedTypes=null;
            EventHandler<CoreWebView2WebMessageReceivedEventArgs> observe=(sender,args)=>{
                if(args.WebMessageAsJson.Contains(requestId))
                {
                    try {
                        var names=new List<string>();
                        foreach(object attached in args.AdditionalObjects) names.Add(attached==null?"null":attached.GetType().FullName);
                        observedTypes=String.Join(",",names.ToArray());
                    } catch(Exception error) { observedTypes="ERROR:"+error.GetType().FullName;Console.WriteLine("DROP_MAPPING "+observedTypes+" HRESULT="+error.HResult); }
                }
            };
            web.CoreWebView2.WebMessageReceived+=observe;
            try
            {
                Check(await web.CoreWebView2.ExecuteScriptAsync("window.yingxuDesktopDropPaths===true && typeof window.chrome.webview.postMessageWithAdditionalObjects==='function'")=="true",
                    "production navigation exposes supported attached-file drop bridge");
                await web.CoreWebView2.ExecuteScriptAsync("window.dropFixtureResponses={};window.chrome.webview.addEventListener('message',function(e){if(e.data.action==='resolved-drop-files')window.dropFixtureResponses[e.data.requestId]=e.data;});var input=document.createElement('input');input.type='file';input.multiple=true;input.id='drop-fixture-input';document.body.appendChild(input);");
                var tree=json.Deserialize<Dictionary<string,object>>(await web.CoreWebView2.CallDevToolsProtocolMethodAsync("DOM.getDocument","{}"));
                var root=(Dictionary<string,object>)tree["root"];
                var node=json.Deserialize<Dictionary<string,object>>(await web.CoreWebView2.CallDevToolsProtocolMethodAsync("DOM.querySelector",json.Serialize(new {nodeId=root["nodeId"],selector="#drop-fixture-input"})));
                await web.CoreWebView2.CallDevToolsProtocolMethodAsync("DOM.setFileInputFiles",json.Serialize(new {nodeId=node["nodeId"],files=new[]{first,second}}));
                Check(await web.CoreWebView2.ExecuteScriptAsync("document.getElementById('drop-fixture-input').files.length")=="2",
                    "real WebView file input receives two isolated on-disk Files");
                await web.CoreWebView2.ExecuteScriptAsync("window.chrome.webview.postMessageWithAdditionalObjects({action:'resolve-drop-files',requestId:'"+requestId+"'},Array.from(document.getElementById('drop-fixture-input').files));");
                Dictionary<string,object> response=await DropFixtureResponse(web,requestId);
                Check(observedTypes=="Microsoft.Web.WebView2.Core.CoreWebView2File,Microsoft.Web.WebView2.Core.CoreWebView2File",
                    "pinned SDK maps both native Files to CoreWebView2File in real runtime");
                Check(!response.ContainsKey("error") && response.ContainsKey("paths"),"production native bridge returns resolved paths without import errors");
                var paths=response["paths"] as System.Collections.ArrayList;
                Check(paths!=null && paths.Count==2 && (string)paths[0]==first && (string)paths[1]==second,
                    "native drop response preserves actual Unicode full paths and order");
                Check(File.ReadAllText(first)=="Synthetic drop metadata fixture one" && File.ReadAllText(second)=="Synthetic drop metadata fixture two",
                    "native resolver leaves both temporary source contents unchanged");
                // A dropped directory must resolve as the directory itself, not
                // an empty byte-upload or the first file inside it.
                string directory=Path.Combine(Hub.Data,"合成 项目文件夹");
                Directory.CreateDirectory(directory);
                File.WriteAllText(Path.Combine(directory,"剧本.md"),"Synthetic project folder");
                const string directoryId="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
                await web.CoreWebView2.CallDevToolsProtocolMethodAsync("DOM.setFileInputFiles",json.Serialize(new {nodeId=node["nodeId"],files=new[]{directory}}));
                await web.CoreWebView2.ExecuteScriptAsync("window.chrome.webview.postMessageWithAdditionalObjects({action:'resolve-drop-files',requestId:'"+directoryId+"'},Array.from(document.getElementById('drop-fixture-input').files));");
                var directoryResponse=await DropFixtureResponse(web,directoryId);
                var directoryPaths=directoryResponse.ContainsKey("paths")?directoryResponse["paths"] as System.Collections.ArrayList:null;
                Check(!directoryResponse.ContainsKey("error") && directoryPaths!=null && directoryPaths.Count==1 && (string)directoryPaths[0]==directory,
                    "real WebView attached directory preserves project root path for library import");
                const string syntheticId="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
                string syntheticSend=await web.CoreWebView2.ExecuteScriptAsync("(function(){try{window.chrome.webview.postMessageWithAdditionalObjects({action:'resolve-drop-files',requestId:'"+syntheticId+"'},[new File(['synthetic browser bytes'],'generated.png',{type:'image/png'})]);return 'sent';}catch(e){return e.name+': '+e.message;}})()");
                if(syntheticSend=="\"sent\"")
                {
                    var synthetic=await DropFixtureResponse(web,syntheticId);
                    Check(!synthetic.ContainsKey("error") && synthetic.ContainsKey("paths") && synthetic["paths"]==null,
                        "generated browser File without disk path falls back without claiming import");
                }
                else
                {
                    Check(syntheticSend.StartsWith("\"TypeError:",StringComparison.Ordinal),
                        "runtime rejects generated browser File synchronously for frontend blob fallback");
                    Check(await web.CoreWebView2.ExecuteScriptAsync("window.dropFixtureResponses['"+syntheticId+"']||null")=="null",
                        "unsupported generated File does not invoke native resolver or claim import");
                }
            }
            finally { web.CoreWebView2.WebMessageReceived-=observe; }
        }
        private static async Task<Dictionary<string,object>> DropFixtureResponse(WebView2 web,string requestId)
        {
            for(int index=0;index<100;index++)
            {
                string response=await web.CoreWebView2.ExecuteScriptAsync("window.dropFixtureResponses['"+requestId+"']||null");
                if(response!="null") return new JavaScriptSerializer().Deserialize<Dictionary<string,object>>(response);
                await Task.Delay(20);
            }
            throw new TimeoutException("Real attached-file bridge did not return the fixture request: "+requestId);
        }
        [MethodImpl(MethodImplOptions.NoInlining)]
        private static void RunZoomIntegration(string browserFolder)
        {
            // Explicit, offline integration mode. It never shows a window, connects
            // to the backend, registers a hotkey or accesses screen/clipboard data.
            CoreWebView2Environment.SetLoaderDllFolderPath(Program.LoaderFolder);
            using(var window=new StudioWindow(false))
            {
                ((NotifyIcon)Field(window,"tray")).Visible=false;
                try
                {
                    var work=CheckZoomReset(window,browserFolder);
                    DateTime deadline=DateTime.UtcNow.AddSeconds(15);
                    while(!work.IsCompleted && DateTime.UtcNow<deadline) { Application.DoEvents();Thread.Sleep(10); }
                    if(!work.IsCompleted) throw new TimeoutException("Offline WebView zoom integration timed out");
                    work.GetAwaiter().GetResult();
                }
                finally
                {
                    ((NotifyIcon)Field(window,"tray")).Dispose();
                    ((OpenInbox)Field(window,"inbox")).Dispose();
                    ((RegisteredWaitHandle)Field(window,"activation")).Unregister(null);
                    ((System.Windows.Forms.Timer)Field(window,"exitTimer")).Dispose();
                }
            }
        }
        private static async Task CheckZoomReset(StudioWindow window,string browserFolder)
        {
            var web=new WebView2 { Dock=DockStyle.Fill };window.Controls.Add(web);Field(window,"web",web);
            IntPtr initializedHandle=web.Handle;
            var environment=await CoreWebView2Environment.CreateAsync(browserFolder,Path.Combine(Hub.Cache,"zoom-profile"),
                new CoreWebView2EnvironmentOptions("--disable-background-networking --no-first-run"));
            var exited=new TaskCompletionSource<bool>();
            environment.BrowserProcessExited+=(sender,args)=>exited.TrySetResult(true);
            await web.EnsureCoreWebView2Async(environment);
            int events=0;
            web.ZoomFactorChanged+=(sender,args)=>{events++;Call(window,"UpdateZoomStatus");};
            var zoom=(ToolStripStatusLabel)Field(window,"zoomStatus");
            var messages=new JavaScriptSerializer();
            Call(window,"ReceiveDesktopRequest",Hub.Url,messages.Serialize(new{action="image-preview",active=true}));
            Check(!web.CoreWebView2.Settings.IsZoomControlEnabled,"image preview disables native page zoom so Ctrl+wheel belongs to the image");
            Call(window,"ReceiveDesktopRequest","https://example.com",messages.Serialize(new{action="image-preview",active=false}));
            Check(!web.CoreWebView2.Settings.IsZoomControlEnabled,"foreign frame cannot change zoom routing");
            Call(window,"ReceiveDesktopRequest",Hub.Url,messages.Serialize(new{action="image-preview",active=false}));
            Check(web.CoreWebView2.Settings.IsZoomControlEnabled,"leaving image restores native interface zoom");
            // Normal property assignment intentionally does not emit the SDK event.
            // An out-of-range assignment invokes the engine's real normalization event.
            web.ZoomFactor=100;
            for(int i=0;i<80 && events==0;i++) await Task.Delay(25);
            Check(events>0 && zoom.Text=="界面 "+Math.Round(web.ZoomFactor*100).ToString(System.Globalization.CultureInfo.InvariantCulture)+"%",
                "real WebView normalization event updates the product percentage callback");
            Check(Math.Abs(web.ZoomFactor-1)>0.01,"reset regression starts at a non-default engine zoom");
            zoom.PerformClick();
            Check(Math.Abs(web.ZoomFactor-1)<0.000001 && zoom.Text=="界面 100%",
                "real click resets engine and label synchronously without a setter event");
            var navigation=new TaskCompletionSource<bool>();
            web.CoreWebView2.NavigationCompleted+=(sender,args)=>navigation.TrySetResult(args.IsSuccess);
            web.NavigateToString("<!doctype html><meta charset='utf-8'><script>window.pauseMessageCount=0;window.chrome.webview.addEventListener('message',function(event){if(event.data.action==='pause-media')window.pauseMessageCount++;});</script>");
            for(int i=0;i<100 && !navigation.Task.IsCompleted;i++)await Task.Delay(25);
            Check(navigation.Task.IsCompleted && navigation.Task.Result,"isolated media lifecycle page loads inside real WebView");
            Field(window,"pageReady",true);
            var close=new FormClosingEventArgs(CloseReason.UserClosing,false);
            Call(window,"OnFormClosing",close);
            string pauseCount="0";
            for(int i=0;i<80 && pauseCount!="1";i++) {
                await Task.Delay(25);pauseCount=await web.CoreWebView2.ExecuteScriptAsync("window.pauseMessageCount");
            }
            Check(close.Cancel && !window.IsDisposed && !window.Visible && pauseCount=="1",
                "close to tray keeps window alive and posts exactly one pause-media message to real WebView");
            Check(!window.Visible,"offline zoom integration keeps the native window hidden");
            web.Dispose();
            for(int i=0;i<100 && !exited.Task.IsCompleted;i++) await Task.Delay(25);
            Check(exited.Task.IsCompleted,"isolated browser exits before temporary profile cleanup");
        }
        [MethodImpl(MethodImplOptions.NoInlining)]
        private static void Run()
        {
            using (var window = new StudioWindow(false))
            {
                window.Text = "映序桌面生命周期 · 合成测试";
                var tray = (NotifyIcon)Field(window,"tray");
                using(var embedded=Assembly.GetExecutingAssembly().GetManifestResourceStream("brand.ico"))
                using(var bytes=new MemoryStream())
                {
                    embedded.CopyTo(bytes);
                    Check(Convert.ToBase64String(bytes.ToArray())==Convert.ToBase64String(File.ReadAllBytes(Path.Combine(Hub.Root,"desktop","brand.ico"))),
                        "embedded window/tray icon matches the packaged brand asset");
                }
                using(var expected=new Icon(Path.Combine(Hub.Root,"desktop","brand.ico"),window.Icon.Size))
                {
                    Check(SameIcon(window.Icon,expected)&&SameIcon(tray.Icon,expected),"window and notification area use the current brand artwork");
                    IntPtr handle=SendMessage(window.Handle,0x007F,new IntPtr(1),IntPtr.Zero);
                    Check(handle!=IntPtr.Zero,"native taskbar WM_GETICON exposes an explicit large icon");
                    using(var native=(Icon)Icon.FromHandle(handle).Clone())
                    using(var expectedNative=new Icon(Path.Combine(Hub.Root,"desktop","brand.ico"),native.Size))
                    {
                        Console.WriteLine("Native icon size: "+native.Size+"; asset size: "+expectedNative.Size);
                        Check(SameIcon(native,expectedNative),"native taskbar icon pixels match the current packaged brand artwork");
                    }
                }
                Check(tray.Visible && tray.ContextMenuStrip.Items.Count == 4,"tray icon and open/settings/exit menu exist");
                var zoom=(ToolStripStatusLabel)Field(window,"zoomStatus");
                Check(zoom.Text=="界面 100%"&&zoom.IsLink&&zoom.Owner==((ToolStripStatusLabel)Field(window,"status")).Owner,"independent zoom percentage keeps existing status messages in the same status bar");
                zoom.PerformClick();Check(zoom.Text=="界面 100%","zoom reset safely waits for WebView initialization");
                Call(window,"BringToUser"); Application.DoEvents();
                Check(window.Visible,"tray reopen restores visible window");
                window.Close(); Application.DoEvents();
                Check(!window.IsDisposed && !window.Visible,"default close hides window without disposing drafts");
                Call(window,"BringToUser"); Application.DoEvents();
                Check(window.Visible && !window.IsDisposed,"hidden window reopens with same instance");
                Field(window,"pageReady",true); Field(window,"loaded",true); Field(window,"closeToTray",false);
                window.Close(); Application.DoEvents();
                string request = (string)Field(window,"exitRequest");
                Check(!String.IsNullOrEmpty(request) && !window.IsDisposed,"close-to-tray off requests unsaved-draft approval before exit");
                var json = new JavaScriptSerializer();
                Call(window,"ReceiveDesktopRequest","https://example.com",json.Serialize(new {action="exit-response",requestId=request,allow=true}));
                Check((string)Field(window,"exitRequest")==request && !window.IsDisposed,"external exit response cannot close the editor");
                Call(window,"ReceiveDesktopRequest",Hub.Url,json.Serialize(new {action="exit-response",requestId="stale",allow=true}));
                Check((string)Field(window,"exitRequest")==request,"stale exit response is ignored");
                Call(window,"ReceiveDesktopRequest",Hub.Url,json.Serialize(new {action="exit-response",requestId=request,allow=false}));
                Check(Field(window,"exitRequest")==null && !window.IsDisposed,"cancelled draft approval preserves window");
                Call(window,"RequestExit"); request=(string)Field(window,"exitRequest");
                Call(window,"ReceiveDesktopRequest",Hub.Url,json.Serialize(new {action="exit-response",requestId=request,allow=true}));
                Application.DoEvents();
                Check(window.IsDisposed,"approved exit disposes the window");
                Check(!tray.Visible,"approved exit removes tray icon");
            }
            using (var capturing = new StudioWindow(false))
            {
                string contextRequest=null; int selections=0,registrations=0,unregisters=0;
                var fake=new CaptureCoordinator(message => {
                    var map=new JavaScriptSerializer().Deserialize<Dictionary<string,object>>(new JavaScriptSerializer().Serialize(message));
                    if((string)map["action"]=="capture-context-request")contextRequest=(string)map["requestId"];
                    return true;
                },(text,error)=>{},new CaptureServices {
                    ScreenBounds=()=>new Rectangle(-100,0,64,32),Select=bounds=>{selections++;return null;},
                    Copy=image=>{throw new Exception("Synthetic cancellation must never touch clipboard");},
                    Upload=(image,project)=>{throw new Exception("Synthetic cancellation must never upload");}
                });
                ((CaptureCoordinator)Field(capturing,"capture")).Dispose(); Field(capturing,"capture",fake);
                ((CaptureHotkey)Field(capturing,"captureHotkey")).Dispose();
                var keys=new CaptureHotkey((h,id,m,k)=>{registrations++;return true;},(h,id)=>unregisters++);
                Field(capturing,"captureHotkey",keys); Field(capturing,"captureEnabled",true);
                Call(capturing,"ApplyCaptureHotkey"); Call(capturing,"ApplyCaptureHotkey");
                Check(registrations==1,"window settings register one event hotkey without polling");
                Field(capturing,"captureEnabled",false); Call(capturing,"ApplyCaptureHotkey");
                Check(unregisters==1&&!keys.Registered,"turning background capture off unregisters global hotkey");
                var json=new JavaScriptSerializer();
                Check(!(bool)Call(capturing,"ReceiveDesktopRequest","https://example.com",json.Serialize(new{action="capture-request"})),"untrusted page cannot initiate capture");
                Call(capturing,"ReceiveDesktopRequest",Hub.Url,json.Serialize(new{action="capture-request"})); Application.DoEvents();
                Check(fake.Busy&&contextRequest!=null,"manual capture works while background hotkey is disabled");
                capturing.Close(); Application.DoEvents();
                Check(!capturing.IsDisposed&&fake.Busy,"titlebar close cannot dispose an active capture");
                Call(capturing,"RequestExit");
                Check(Field(capturing,"exitRequest")==null&&!capturing.IsDisposed,"exit during capture preserves editor and waits for completion");
                Call(capturing,"BringToUser");
                Check(!capturing.Visible&&(bool)Field(capturing,"showAfterCapture"),"explicit activation is deferred until capture completes");
                string response=json.Serialize(new{action="capture-context",requestId=contextRequest,projectId=(string)null,itemId=(string)null});
                Call(capturing,"ReceiveDesktopRequest","https://example.com",response);
                Check(selections==0&&fake.Busy,"untrusted context cannot complete the screenshot handshake");
                Call(capturing,"ReceiveDesktopRequest",Hub.Url,response); Application.DoEvents();
                Check(selections==1&&!fake.Busy&&capturing.Visible,"trusted cancellation releases busy state and honors deferred activation");
                Field(capturing,"exitApproved",true); capturing.Close(); Application.DoEvents();
            }
            bool approved = false;
            using (var failed = new StudioWindow(false,() => approved))
            {
                Field(failed,"loaded",true); Field(failed,"pageFailed",true); Field(failed,"pageReady",false);
                Call(failed,"RequestExit"); Application.DoEvents();
                Check(!failed.IsDisposed && Field(failed,"exitRequest")==null,"failed page exit cancellation preserves window without waiting on dead WebView");
                approved = true; Call(failed,"RequestExit"); Application.DoEvents();
                Check(failed.IsDisposed,"explicit native confirmation exits a failed page while leaving persisted draft files untouched");
            }
            approved = false;
            using (var failedClose = new StudioWindow(false,() => approved))
            {
                Field(failedClose,"loaded",true); Field(failedClose,"pageFailed",true);
                Call(failedClose,"BringToUser"); failedClose.Close(); Application.DoEvents();
                Check(failedClose.Visible && !failedClose.IsDisposed,"closing failed page asks for confirmation instead of hiding in tray");
                approved=true; failedClose.Close(); Application.DoEvents();
                Check(failedClose.IsDisposed,"failed page titlebar close exits after explicit confirmation");
            }
            using (var failedStartup = new StudioWindow(false,() => { throw new Exception("No editor draft exists"); }))
            {
                Field(failedStartup,"pageFailed",true);
                failedStartup.Close(); Application.DoEvents();
                Check(failedStartup.IsDisposed,"failed startup titlebar close exits directly when no editor ever loaded");
            }
            approved = false;
            using (var notReady = new StudioWindow(false,() => approved))
            {
                Field(notReady,"loaded",true); Field(notReady,"pageReady",false);
                Call(notReady,"RequestExit"); Application.DoEvents();
                Check(!notReady.IsDisposed,"loaded page without ready bridge preserves drafts when native exit is cancelled");
                approved = true; Call(notReady,"RequestExit"); Application.DoEvents();
                Check(notReady.IsDisposed,"loaded page without ready bridge can exit after explicit confirmation");
            }
            approved = false;
            using (var unresponsive = new StudioWindow(false,() => approved))
            {
                Field(unresponsive,"loaded",true); Field(unresponsive,"exitUnresponsive",true); Field(unresponsive,"pageReady",true);
                Call(unresponsive,"RequestExit"); Application.DoEvents();
                Check(!unresponsive.IsDisposed && Field(unresponsive,"exitRequest")==null,"timed-out exit requires native confirmation rather than another dead handshake");
                approved=true; Call(unresponsive,"RequestExit"); Application.DoEvents();
                Check(unresponsive.IsDisposed,"explicit native confirmation releases a timed-out page");
            }
        }
    }
}
