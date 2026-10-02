// Reflection-only Stage J check against the final packaged native host.
// It deliberately does not call Program.Main or create a second product build.
using System;
using System.Collections;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Net;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Threading.Tasks;
using System.Linq.Expressions;
using System.Linq;
using System.Windows.Forms;
using System.Web.Script.Serialization;

internal sealed class StageJHostChecks
{
    private readonly string root, exe, sdk, output, data, projects, version, build, skill, task;
    private readonly int port;
    private readonly string projectId;
    private readonly JavaScriptSerializer json = new JavaScriptSerializer();
    private readonly List<string> checks = new List<string>();
    private readonly List<string> consoleErrors = new List<string>();
    private readonly Dictionary<string,string> screenshots = new Dictionary<string,string>();
    private readonly Dictionary<string,Assembly> hostedSdkAssemblies = new Dictionary<string,Assembly>(StringComparer.Ordinal);
    private Assembly app;
    private Type program, hub;
    private Form window;
    private object webView, core;
    private Timer timer;
    private bool sequenceStarted, ticking, exitRequested;
    private readonly List<object> devtoolsReceivers = new List<object>();
    private readonly TaskCompletionSource<bool> runtimeReady = new TaskCompletionSource<bool>();
    private readonly List<string> navigationEvents = new List<string>();
    private bool webViewControlCreated, webViewInitialized, navigationStarted, navigationCompleted;
    private bool windowLoadObserved, windowShownObserved;
    private bool? navigationSucceeded;
    private string navigationUri, navigationError;
    private object failureDiagnostics;
    private Exception failure;
    private DateTime pageReadyDeadline;

    private StageJHostChecks(string[] args)
    {
        root = Path.GetFullPath(args[0]); exe = Path.GetFullPath(args[1]); sdk = Path.GetFullPath(args[2]);
        output = Path.GetFullPath(args[3]); port = Int32.Parse(args[4]); data = Path.GetFullPath(args[5]);
        projects = Path.GetFullPath(args[6]); projectId = args[7]; skill = args[8]; task = args[9];
        version = args[10]; build = args[11];
    }

    [STAThread]
    private static int Main(string[] args)
    {
        if (args.Length != 12) { Console.Error.WriteLine("Expected root, final exe, SDK, report dir, port, synthetic data/projects, project ID, skill/task names, version and build."); return 2; }
        try { return new StageJHostChecks(args).Run(); }
        catch (Exception error) { Console.Error.WriteLine(error); return 1; }
    }

    private int Run()
    {
        Directory.CreateDirectory(output);
        Environment.SetEnvironmentVariable("YINGXU_DATA_DIR", data);
        Environment.SetEnvironmentVariable("YINGXU_PROJECTS_DIR", projects);
        Environment.SetEnvironmentVariable("YINGXU_WEBVIEW2_MODE", "bundled");
        Environment.SetEnvironmentVariable("YINGXU_PYTHON", null);
        Environment.SetEnvironmentVariable("PYTHONHOME", null);
        Environment.SetEnvironmentVariable("PYTHONPATH", null);
        app = Assembly.LoadFrom(exe);
        // Assembly.Load(byte[]) has no normal load context. This reflection host
        // explicitly requests Core before StudioWindow is JIT compiled, unlike
        // Program.Main. Resolve both embedded SDK DLLs to one identity instance
        // so WinForms property signatures and native call sites use the same Core.
        AppDomain.CurrentDomain.AssemblyResolve += ResolveHostedSdk;
        program = app.GetType("YingXu.Desktop.Program", true);
        hub = app.GetType("YingXu.Desktop.Hub", true);
        Set(hub, "Root", root); Set(hub, "Data", data); Set(hub, "Port", port);
        Set(hub, "Url", "http://127.0.0.1:" + port + "/");
        Set(hub, "Cache", Path.Combine(data, "desktop"));
        Directory.CreateDirectory((string)Get(hub, "Cache"));
        Set(program, "InitialFiles", new string[0]);
        Set(program, "InstanceKey", Call(hub, "InstanceId").ToString().Substring(0,24));
        Call(program, "PrepareIsolatedLibraries");
        string loader = (string)Get(program, "LoaderFolder");
        Type environment = HostedSdk("Microsoft.Web.WebView2.Core").GetType("Microsoft.Web.WebView2.Core.CoreWebView2Environment", true);
        environment.GetMethod("SetLoaderDllFolderPath", BindingFlags.Public | BindingFlags.Static).Invoke(null, new object[] { loader });
        VerifySdkResource("Microsoft.Web.WebView2.Core.dll");
        VerifySdkResource("Microsoft.Web.WebView2.WinForms.dll");
        VerifySdkResource("WebView2Loader.dll");
        CheckHealth();

        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        Application.SetUnhandledExceptionMode(UnhandledExceptionMode.CatchException);
        Application.ThreadException += delegate(object sender, System.Threading.ThreadExceptionEventArgs args)
        {
            if (failure == null) failure = args.Exception;
        };
        Type studio = app.GetType("YingXu.Desktop.StudioWindow", true);
        ConstructorInfo ctor = studio.GetConstructor(BindingFlags.Instance | BindingFlags.NonPublic, null,
            new Type[] { typeof(bool), typeof(Func<bool>), typeof(bool) }, null);
        if (ctor == null) throw new MissingMethodException("StudioWindow production constructor not found.");
        window = (Form)ctor.Invoke(new object[] { true, new Func<bool>(() => false), true });
        window.Load += delegate { windowLoadObserved = true; };
        window.Shown += delegate { windowShownObserved = true; };
        window.MinimumSize = new Size(780,620); window.ClientSize = new Size(1440,900);
        FieldInfo tray = studio.GetField("closeToTray", BindingFlags.Instance | BindingFlags.NonPublic);
        tray.SetValue(window, false);
        window.ControlAdded += OnControlAdded;
        pageReadyDeadline=DateTime.UtcNow.AddSeconds(55);
        timer = new Timer { Interval = 150 };
        timer.Tick += async delegate
        {
            if (ticking) return;
            ticking = true;
            try
            {
                Exception tickError = null;
                try { await Tick(); }
                catch (Exception error) { tickError = error; }
                if (tickError != null)
                {
                    if (failure == null) failure = tickError;
                    await CaptureFailureDiagnostics();
                    RequestExit();
                }
            }
            finally { ticking = false; }
        };
        timer.Start();
        Application.Run(window);
        timer.Stop(); timer.Dispose();
        if (failure != null) { SaveResult(); throw new InvalidOperationException("Stage J host scenario failed.", failure); }
        if (consoleErrors.Count != 0) { SaveResult(); throw new InvalidOperationException("Browser console reported JavaScript errors: " + String.Join(" | ", consoleErrors.ToArray())); }
        checks.Add("no-webview-console-errors");
        SaveResult();
        return 0;
    }

    private Assembly ResolveHostedSdk(object sender, ResolveEventArgs args)
    {
        var requested = new AssemblyName(args.Name);
        string name = requested.Name;
        if (name != "Microsoft.Web.WebView2.Core" && name != "Microsoft.Web.WebView2.WinForms") return null;
        Assembly resolved = HostedSdk(name);
        if (!String.Equals(resolved.GetName().FullName, requested.FullName, StringComparison.Ordinal))
            throw new InvalidDataException("Requested SDK identity differs from packaged resource: " + name);
        return resolved;
    }

    private Assembly HostedSdk(string name)
    {
        if (name != "Microsoft.Web.WebView2.Core" && name != "Microsoft.Web.WebView2.WinForms")
            throw new InvalidDataException("Unexpected hosted SDK name.");
        Assembly resolved;
        if (!hostedSdkAssemblies.TryGetValue(name, out resolved))
        {
            using (var source = app.GetManifestResourceStream(name + ".dll"))
            using (var memory = new MemoryStream())
            {
                if (source == null) throw new InvalidDataException("Packaged SDK resource is missing: " + name);
                source.CopyTo(memory);
                resolved = Assembly.Load(memory.ToArray());
            }
            hostedSdkAssemblies.Add(name, resolved);
        }
        return resolved;
    }

    private async Task Tick()
    {
        if (window.IsDisposed) return;
        // Initialization callbacks can fail before pageReady. Preserve their
        // original exception and use the same normal exit handshake instead of
        // replacing it with the later generic 55-second timeout.
        if (failure != null && !exitRequested)
        {
            await CaptureFailureDiagnostics();
            RequestExit();
            return;
        }
        webView = studioField("web");
        if (webView != null && core == null) core = webView.GetType().GetProperty("CoreWebView2").GetValue(webView, null);
        if (!sequenceStarted && Convert.ToBoolean(studioField("pageReady")))
        {
            sequenceStarted = true;
            await RunPages();
            RequestExit();
        }
        else if (!sequenceStarted && DateTime.UtcNow > pageReadyDeadline)
        {
            failure=new TimeoutException("Production StudioWindow did not report desktop-ready within 55 seconds.");
            await CaptureFailureDiagnostics();
            RequestExit();
        }
        if (exitRequested && !window.IsDisposed && DateTime.UtcNow > exitDeadline)
        {
            failure = new TimeoutException("StudioWindow did not complete its normal prepare-exit handshake.");
            SaveResult();
            Environment.Exit(3);
        }
    }

    private DateTime exitDeadline;
    private object studioField(string name) { return window.GetType().GetField(name, BindingFlags.Instance | BindingFlags.NonPublic).GetValue(window); }
    private void RequestExit()
    {
        if (exitRequested || window == null || window.IsDisposed) return;
        exitRequested = true; exitDeadline = DateTime.UtcNow.AddSeconds(25);
        window.GetType().GetMethod("RequestExit", BindingFlags.Instance | BindingFlags.NonPublic).Invoke(window, null);
    }

    private void OnControlAdded(object sender, ControlEventArgs args)
    {
        if (!args.Control.GetType().FullName.StartsWith("Microsoft.Web.WebView2.WinForms.WebView2",StringComparison.Ordinal)) return;
        webViewControlCreated=true;
        EventInfo evt = args.Control.GetType().GetEvent("CoreWebView2InitializationCompleted");
        if (evt == null) { failure=new MissingMemberException("WebView2 initialization event unavailable."); return; }
        evt.AddEventHandler(args.Control,BuildHandler(evt,GetType().GetMethod("OnWebViewInitialized",BindingFlags.Instance|BindingFlags.NonPublic)));
    }

    private async void OnWebViewInitialized(object sender, object args)
    {
        try
        {
            object success=args.GetType().GetProperty("IsSuccess").GetValue(args,null);
            if (!(success is bool) || !(bool)success) throw new InvalidOperationException("WebView2 initialization failed before navigation.");
            webViewInitialized=true;
            core=sender.GetType().GetProperty("CoreWebView2").GetValue(sender,null);
            var coreAssembly=HostedSdk("Microsoft.Web.WebView2.Core");
            if (!Object.ReferenceEquals(sender.GetType().GetProperty("CoreWebView2").PropertyType.Assembly,coreAssembly) ||
                !Object.ReferenceEquals(core.GetType().Assembly,coreAssembly))
                throw new InvalidDataException("Hosted WinForms and native Core SDK type identities differ.");
            checks.Add("hosted-sdk-single-core-type-identity");
            SubscribeCoreEvent("NavigationStarting","OnNavigationStarting");
            SubscribeCoreEvent("NavigationCompleted","OnNavigationCompleted");
            SubscribeDevTools("Runtime.exceptionThrown");
            SubscribeDevTools("Runtime.consoleAPICalled");
            MethodInfo enable=core.GetType().GetMethod("CallDevToolsProtocolMethodAsync",new Type[]{typeof(string),typeof(string)});
            Task command=(Task)enable.Invoke(core,new object[]{"Runtime.enable","{}"});
            await command;
            runtimeReady.TrySetResult(true);
        }
        catch(Exception error) { if(failure==null)failure=error;runtimeReady.TrySetException(error); }
    }

    private void SubscribeCoreEvent(string eventName,string methodName)
    {
        EventInfo evt=core.GetType().GetEvent(eventName);
        if(evt==null)throw new MissingMemberException("Pinned WebView2 SDK lacks "+eventName+".");
        MethodInfo target=GetType().GetMethod(methodName,BindingFlags.Instance|BindingFlags.NonPublic);
        evt.AddEventHandler(core,BuildHandler(evt,target));
    }

    private void OnNavigationStarting(object sender,object args)
    {
        navigationStarted=true;
        object value=args.GetType().GetProperty("Uri").GetValue(args,null);
        navigationUri=SafeUrl(Convert.ToString(value));
        navigationEvents.Add("starting:"+(navigationUri??"unknown"));
    }

    private void OnNavigationCompleted(object sender,object args)
    {
        navigationCompleted=true;
        object success=args.GetType().GetProperty("IsSuccess").GetValue(args,null);
        if(success is bool)navigationSucceeded=(bool)success;
        object status=args.GetType().GetProperty("WebErrorStatus").GetValue(args,null);
        navigationError=Convert.ToString(status);
        navigationEvents.Add("completed:"+(navigationSucceeded.HasValue?navigationSucceeded.Value.ToString():"unknown")+":"+navigationError);
    }

    private static string SafeUrl(string value)
    {
        Uri uri;
        if(String.IsNullOrEmpty(value)||!Uri.TryCreate(value,UriKind.Absolute,out uri))return null;
        try{return uri.GetLeftPart(UriPartial.Path);}catch{return uri.Scheme+"://"+uri.Host;}
    }

    private object ReadField(object instance,string name)
    {
        try{return instance.GetType().GetField(name,BindingFlags.Instance|BindingFlags.NonPublic|BindingFlags.Public).GetValue(instance);}
        catch{return null;}
    }

    private object ReadProperty(object instance,string name)
    {
        try
        {
            if(instance==null)return null;
            PropertyInfo property=instance.GetType().GetProperty(name,BindingFlags.Instance|BindingFlags.Public|BindingFlags.NonPublic);
            return property==null?null:property.GetValue(instance,null);
        }
        catch(Exception error){return "unavailable:"+error.GetType().Name;}
    }

    private object ReadSyntheticStartupLog()
    {
        try
        {
            string path=Path.Combine(data,"desktop.log");
            if(!File.Exists(path))return new {state="missing"};
            var info=new FileInfo(path);
            if((info.Attributes&FileAttributes.ReparsePoint)!=0)return new {state="reparse-point-refused"};
            if(info.Length>65536)return new {state="too-large",bytes=info.Length};
            string[] lines=File.ReadAllLines(path,Encoding.UTF8);
            var selected=new List<string>();
            foreach(string line in lines)
            {
                int marker=line.IndexOf("startup_stage=",StringComparison.Ordinal);
                if(marker<0)marker=line.IndexOf("browser_source=",StringComparison.Ordinal);
                if(marker<0)marker=line.IndexOf("service_",StringComparison.Ordinal);
                if(marker<0)marker=line.IndexOf("window_ready runtime=",StringComparison.Ordinal);
                if(marker>=0)
                {
                    string value=line.Substring(marker);
                    if(value.Length>256)value=value.Substring(0,256);
                    selected.Add(value);
                    continue;
                }
                // Keep only the exception type from a startup error; messages can
                // contain paths or request details and are unnecessary here.
                marker=line.IndexOf("window_error ",StringComparison.Ordinal);
                if(marker>=0)
                {
                    string value=line.Substring(marker+"window_error ".Length);
                    int end=value.IndexOfAny(new char[]{':',' ','\t'});
                    if(end>=0)value=value.Substring(0,end);
                    selected.Add("window_error "+value);
                }
            }
            return new {state="read",lines=selected};
        }
        catch(Exception error){return new {state="unavailable",error=error.GetType().Name};}
    }

    private async Task CaptureFailureDiagnostics()
    {
        if(failureDiagnostics!=null)return;
        var details=new Dictionary<string,object>();
        details["webview_control_created"]=webViewControlCreated;
        details["window_load_observed"]=windowLoadObserved;
        details["window_shown_observed"]=windowShownObserved;
        details["webview_initialized"]=webViewInitialized;
        details["hosted_sdk_assembly_instances"]=AppDomain.CurrentDomain.GetAssemblies()
            .Where(value=>value.GetName().Name=="Microsoft.Web.WebView2.Core" || value.GetName().Name=="Microsoft.Web.WebView2.WinForms")
            .Select(value=>value.GetName().FullName).ToArray();
        details["devtools_runtime_enabled"]=runtimeReady.Task.Status==TaskStatus.RanToCompletion;
        details["navigation_started"]=navigationStarted;
        details["navigation_completed"]=navigationCompleted;
        details["navigation_succeeded"]=navigationSucceeded;
        details["navigation_error_status"]=navigationError;
        details["navigation_uri_without_query"]=navigationUri;
        details["navigation_events"]=navigationEvents;
        details["core_source_without_query"]=SafeUrl(Convert.ToString(ReadProperty(core,"Source")));
        details["core_is_loading"]=ReadProperty(core,"IsLoading");
        details["webview_source_without_query"]=SafeUrl(Convert.ToString(ReadProperty(webView,"Source")));
        details["page_ready"]=ReadField(window,"pageReady");
        details["page_failed"]=ReadField(window,"pageFailed");
        details["window_visible"]=window!=null&&!window.IsDisposed&&window.Visible;
        details["window_client_size"]=window==null?null:window.ClientSize.ToString();
        object loading=ReadField(window,"loading");
        details["loading_visible"]=ReadProperty(loading,"Visible");
        details["loading_text"]=ReadProperty(loading,"Text");
        details["synthetic_desktop_startup_log"]=ReadSyntheticStartupLog();
        details["console_error_count"]=consoleErrors.Count;
        if(core!=null)
        {
            try
            {
                Task<object> probe=EvaluateJs("JSON.stringify({readyState:document.readyState,title:document.title,path:location.origin+location.pathname,resourceItems:!!document.querySelector('#resourceItems'),appShell:!!document.querySelector('.app-shell'),nativeStartup:window.yingxuNativeStartup===true})");
                Task completed=await Task.WhenAny(probe,Task.Delay(2500));
                if(completed==probe)details["document_probe"]=await probe;
                else details["document_probe"]="timed-out";
            }
            catch(Exception error){details["document_probe"]="unavailable:"+error.GetType().Name;}
        }
        else details["document_probe"]="core-not-initialized";
        failureDiagnostics=details;
    }

    private Delegate BuildHandler(EventInfo evt,MethodInfo target)
    {
        MethodInfo invoke=evt.EventHandlerType.GetMethod("Invoke");
        ParameterInfo[] parameters=invoke.GetParameters();
        ParameterExpression[] args=Array.ConvertAll(parameters,p=>Expression.Parameter(p.ParameterType,p.Name));
        var body=Expression.Call(Expression.Constant(this),target,Expression.Convert(args[0],typeof(object)),Expression.Convert(args[1],typeof(object)));
        return Expression.Lambda(evt.EventHandlerType,body,args).Compile();
    }

    private void SubscribeDevTools(string eventName)
    {
        MethodInfo getReceiver=core.GetType().GetMethod("GetDevToolsProtocolEventReceiver",new Type[]{typeof(string)});
        if(getReceiver==null)throw new MissingMethodException("Pinned WebView2 SDK lacks GetDevToolsProtocolEventReceiver.");
        object receiver=getReceiver.Invoke(core,new object[]{eventName});
        EventInfo evt=receiver.GetType().GetEvent("DevToolsProtocolEventReceived");
        if(evt==null)throw new MissingMemberException("Pinned WebView2 DevTools protocol receiver event is missing.");
        MethodInfo target=GetType().GetMethod("OnDevToolsEvent",BindingFlags.Instance|BindingFlags.NonPublic);
        MethodInfo invoke=evt.EventHandlerType.GetMethod("Invoke");
        ParameterInfo[] parameters=invoke.GetParameters();
        ParameterExpression[] args=Array.ConvertAll(parameters,p=>Expression.Parameter(p.ParameterType,p.Name));
        var body=Expression.Call(Expression.Constant(this),target,Expression.Convert(args[0],typeof(object)),
                                 Expression.Convert(args[1],typeof(object)),Expression.Constant(eventName));
        evt.AddEventHandler(receiver,Expression.Lambda(evt.EventHandlerType,body,args).Compile());
        devtoolsReceivers.Add(receiver);
    }

    private void OnDevToolsEvent(object sender,object args,string eventName)
    {
        string raw=Convert.ToString(args.GetType().GetProperty("ParameterObjectAsJson").GetValue(args,null));
        object parsed;
        try { parsed=json.DeserializeObject(raw); } catch { consoleErrors.Add(eventName+": invalid protocol payload");return; }
        var values=parsed as Dictionary<string,object>;
        if(eventName=="Runtime.exceptionThrown")
        {
            object detail;
            if(values==null||!values.TryGetValue("exceptionDetails",out detail))consoleErrors.Add("Runtime.exceptionThrown: missing exceptionDetails");
            else { var d=detail as Dictionary<string,object>;object message;
                if(d!=null&&d.TryGetValue("text",out message))consoleErrors.Add("uncaught: "+Convert.ToString(message));
                else consoleErrors.Add("uncaught JavaScript exception"); }
        }
        else if(eventName=="Runtime.consoleAPICalled")
        {
            object type;
            if(values==null||!values.TryGetValue("type",out type)){consoleErrors.Add("Runtime.consoleAPICalled: missing type");return;}
            if(String.Equals(Convert.ToString(type),"error",StringComparison.OrdinalIgnoreCase))
            {
                object arguments;var messages=new List<string>();
                if(!values.TryGetValue("args",out arguments)||!(arguments is object[])){consoleErrors.Add("console.error: invalid arguments");return;}
                if(arguments is object[])
                    foreach(object item in (object[])arguments){var entry=item as Dictionary<string,object>;object value;if(entry!=null&&entry.TryGetValue("value",out value))messages.Add(Convert.ToString(value));else if(entry!=null&&entry.TryGetValue("description",out value))messages.Add(Convert.ToString(value));}
                consoleErrors.Add("console.error: "+String.Join(" ",messages.ToArray()));
            }
        }
    }

    private async Task RunPages()
    {
        Task ready=await Task.WhenAny(runtimeReady.Task,Task.Delay(20000));
        if(ready!=runtimeReady.Task)throw new TimeoutException("WebView2 DevTools Runtime did not enable before page assertions.");
        await runtimeReady.Task;
        await WaitFor("project-list", "!!document.querySelector('.project-button[data-project=" + Lit(projectId) + "]')");
        await Click("project", "document.querySelector('.project-button[data-project=" + Lit(projectId) + "]')?.click(); true");
        await WaitFor("selected-project", "document.querySelector('#projectHero')?.innerText.includes('Stage J')");
        await CaptureMode("classic", "skills-classic.png");
        await CaptureMode("focus", "skills-focus.png");
        await SetSize(980,700);
        await OpenCollaborationTools();
        await WaitFor("narrow-tools-visible", "(()=>{const b=document.querySelector('.collaboration-tools-toggle'),p=document.querySelector('.collaboration-tools');if(!b||!p)return false;const r=b.getBoundingClientRect(),s=getComputedStyle(b),ps=getComputedStyle(p);return s.display!=='none'&&r.width>0&&r.height>0&&ps.display!=='none'&&p.getBoundingClientRect().width>0})()");
        await Capture("collaboration-focus-narrow.png");
        checks.Add("narrow-window-project-tools-visible");
    }

    private async Task CaptureMode(string mode, string skillShot)
    {
        await SetMode(mode);
        await Click("skills-section", "document.querySelector('#workspaceTools').open=true;document.querySelector('[data-section=skills]')?.click();true");
        await WaitFor("skills-" + mode, "document.querySelector('#resourceItems')?.innerText.includes(" + Lit(skill) + ")");
        await Capture(skillShot);
        await Click("collaboration-section", "document.querySelector('#workspaceTools').open=true;document.querySelector('[data-section=context]')?.click();true");
        await WaitFor("collaboration-task-" + mode, "document.querySelector('[data-ai-tasks]')?.innerText.includes(" + Lit(task) + ")");
        await OpenCollaborationTools();
        await WaitFor("collaboration-tool-panel-" + mode, "document.querySelector('.collaboration-workspace')?.classList.contains('collaboration-tools-open')&&document.querySelector('#contextToolPanel-handoff')?.innerText.includes('交接整个项目')");
        await Capture("collaboration-" + mode + ".png");
        checks.Add("production-pages-" + mode + "-skills-and-collaboration");
    }

    private async Task SetMode(string mode)
    {
        await Click("mode-" + mode, "(()=>{document.querySelector('#workspaceTools').open=true;const b=document.querySelector('[data-workspace-layout=" + Lit(mode) + "]');if(!b)return false;if(b.getAttribute('aria-pressed')!=='true')b.click();return true})()");
        string active = "document.querySelector('[data-workspace-layout=" + Lit(mode) + "]')?.getAttribute('aria-pressed')==='true'";
        active += mode == "focus" ? "&&document.querySelector('.app-shell')?.classList.contains('focus-workbench')" : "&&!document.querySelector('.app-shell')?.classList.contains('focus-workbench')";
        await WaitFor("mode-active-" + mode, active);
        checks.Add("workspace-layout-" + mode);
    }

    private async Task SetSize(int width, int height)
    {
        window.ClientSize = new Size(width,height);
        await Task.Delay(400);
    }

    private async Task OpenCollaborationTools()
    {
        await Click("open-project-tools", "(()=>{const b=document.querySelector('.collaboration-tools-toggle');if(b)b.click();return !!b})()");
    }

    private async Task Click(string name, string script)
    {
        object result = await EvaluateJs(script);
        if (!(result is bool) || !(bool)result) throw new InvalidOperationException("Could not activate production UI control: " + name);
        await Task.Delay(350);
    }

    private async Task WaitFor(string name, string expression)
    {
        DateTime until = DateTime.UtcNow.AddSeconds(20);
        while (DateTime.UtcNow < until)
        {
            object value = await EvaluateJs("JSON.stringify(!!(" + expression + "))");
            if (value is bool && (bool)value) { checks.Add(name); return; }
            await Task.Delay(200);
        }
        throw new TimeoutException("Timed out waiting for production page check: " + name);
    }

    private async Task Capture(string name)
    {
        Type enumType = core.GetType().Assembly.GetType("Microsoft.Web.WebView2.Core.CoreWebView2CapturePreviewImageFormat", true);
        object png = Enum.Parse(enumType,"Png");
        MethodInfo method = core.GetType().GetMethod("CapturePreviewAsync", new Type[] { enumType, typeof(Stream) });
        if (method == null) throw new MissingMethodException("WebView2 CapturePreviewAsync(Png, Stream) was not found.");
        string path = Path.Combine(output,name);
        using (var stream = new FileStream(path,FileMode.CreateNew,FileAccess.Write,FileShare.None))
        {
            Task capture = (Task)method.Invoke(core,new object[] { png, stream });
            await capture;
        }
        byte[] signature = new byte[] { 137,80,78,71,13,10,26,10 };
        byte[] raw = File.ReadAllBytes(path);
        if (raw.Length < 1024 || !signature.SequenceEqual(new ArraySegment<byte>(raw,0,8))) throw new InvalidDataException("Invalid WebView screenshot: " + name);
        using (var sha = SHA256.Create()) screenshots.Add(name,BitConverter.ToString(sha.ComputeHash(raw)).Replace("-", "").ToLowerInvariant());
    }

    private async Task<object> EvaluateJs(string script)
    {
        if (core == null) throw new InvalidOperationException("WebView2 is not ready.");
        MethodInfo execute = core.GetType().GetMethod("ExecuteScriptAsync",new Type[] { typeof(string) });
        Task operation = (Task)execute.Invoke(core,new object[] { script });
        await operation;
        string raw = Convert.ToString(operation.GetType().GetProperty("Result").GetValue(operation,null));
        object value = json.DeserializeObject(raw);
        if (value is string)
        {
            try { return json.DeserializeObject((string)value); } catch { return value; }
        }
        return value;
    }

    private void CheckHealth()
    {
        var request=(HttpWebRequest)WebRequest.Create("http://127.0.0.1:"+port+"/api/health");
        request.Proxy=null; request.Timeout=5000; request.AllowAutoRedirect=false;
        Dictionary<string,object> health;
        using (var response=request.GetResponse()) using (var reader=new StreamReader(response.GetResponseStream(),Encoding.UTF8))
            health=json.Deserialize<Dictionary<string,object>>(reader.ReadToEnd());
        string expectedProgram = Call(hub,"PathIdentity",Call(hub,"NormalizeRoot",root)).ToString();
        string expectedInstance = Call(hub,"InstanceId").ToString();
        Require(health["app"] as string=="yingxu" && Convert.ToBoolean(health["ok"]),"local-health");
        Require((health["version"] as string)==version && (health["build_revision"] as string)==build,"version-and-build-identity");
        Require((health["program_id"] as string)==expectedProgram && (health["instance_id"] as string)==expectedInstance,"synthetic-program-and-data-identity");
    }

    private void VerifySdkResource(string name)
    {
        string resource = name;
        using (Stream stream=app.GetManifestResourceStream(resource))
        {
            if (stream==null) throw new InvalidDataException("Final EXE is missing embedded SDK member " + resource);
            using (var sha=SHA256.Create())
            using (var buffer=new MemoryStream())
            {
                stream.CopyTo(buffer);
                byte[] actual=sha.ComputeHash(buffer.ToArray()),expected=sha.ComputeHash(File.ReadAllBytes(Path.Combine(sdk,name)));
                if (!StructuralComparisons.StructuralEqualityComparer.Equals(actual,expected)) throw new InvalidDataException("Embedded SDK member differs from verified official archive: " + name);
            }
        }
        checks.Add("embedded-official-sdk-" + name);
    }

    private void Require(bool value,string name) { if(!value)throw new InvalidDataException("Failed check: "+name);checks.Add(name); }
    private object Call(Type type,string name,params object[] args) { return type.GetMethod(name,BindingFlags.Static|BindingFlags.NonPublic|BindingFlags.Public).Invoke(null,args); }
    private object Get(Type type,string name) { return type.GetField(name,BindingFlags.Static|BindingFlags.NonPublic|BindingFlags.Public).GetValue(null); }
    private void Set(Type type,string name,object value) { type.GetField(name,BindingFlags.Static|BindingFlags.NonPublic|BindingFlags.Public).SetValue(null,value); }
    private static string Lit(string text) { return new JavaScriptSerializer().Serialize(text); }
    private void SaveResult()
    {
        var result=new Dictionary<string,object>{{"ok",failure==null&&consoleErrors.Count==0},{"version",version},{"build_revision",build},{"port",port},{"project_id",projectId},{"skill",skill},{"task",task},{"synthetic_data_dir",data},{"synthetic_projects_dir",projects},{"checks",checks},{"console_errors",consoleErrors},{"screenshots_sha256",screenshots},{"host_diagnostics",failureDiagnostics},{"error",failure==null?null:failure.ToString()}};
        File.WriteAllText(Path.Combine(output,"native-result.json"),json.Serialize(result),new UTF8Encoding(false));
    }
}
