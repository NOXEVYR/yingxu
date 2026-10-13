using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace YingXu.Desktop
{
    // One process owns the file inbox. The workspace is created only on demand.
    internal sealed class DesktopContext : ApplicationContext
    {
        private readonly Control dispatcher = new Control();
        private readonly OpenInbox inbox;
        private readonly RegisteredWaitHandle activation;
        private readonly Dictionary<string,QuickReaderWindow> readers = new Dictionary<string,QuickReaderWindow>(StringComparer.OrdinalIgnoreCase);
        private StudioWindow studio;
        private readonly Func<StudioWindow> createStudio;
        private bool stopping;

        internal DesktopContext(Func<StudioWindow> factory = null)
        {
            createStudio=factory ?? (() => new StudioWindow(routed:true));
            IntPtr handle = dispatcher.Handle;
            inbox = new OpenInbox(Program.InstanceKey, paths => Dispatch(() => Open(paths)));
            activation = ThreadPool.RegisterWaitForSingleObject(Program.ActivateEvent,
                (value,timedOut) => { try { Dispatch(() => OpenStudio(new string[0])); } catch (IOException) { } }, null, Timeout.Infinite, false);
            dispatcher.BeginInvoke((Action)(() => Open(Program.InitialFiles ?? new string[0])));
        }

        private void Dispatch(Action action)
        {
            if (stopping || dispatcher.IsDisposed || Program.IncrementalInstalling) throw new IOException("映序正在退出或准备更新，请稍后重新打开。");
            try { dispatcher.BeginInvoke(action); }
            catch (InvalidOperationException) { throw new IOException("映序正在退出，请重新打开。"); }
        }

        internal void Open(string[] paths)
        {
            if(Program.IncrementalInstalling)throw new IOException("映序正在准备更新，请稍后重新打开。");
            if (paths.Length == 0) { OpenStudio(paths); return; }
            var full = new List<string>();
            foreach (string path in paths)
            {
                if (!QuickReaderDocument.Supports(path) && !QuickVideoResource.Supports(path)) { full.Add(path); continue; }
                QuickReaderWindow reader;
                if (!readers.TryGetValue(path,out reader))
                {
                    if (readers.Count >= 32) { MessageBox.Show("最多同时打开 32 个阅览窗口，请先关闭部分窗口。","映序"); continue; }
                    reader = new QuickReaderWindow(path,OpenStudio,openVideoStudio:OpenVideoStudio);
                    readers.Add(path,reader);
                    reader.FormClosed += delegate { readers.Remove(path); FinishIfEmpty(); };
                    reader.Show();
                }
                if (reader.WindowState == FormWindowState.Minimized) reader.WindowState = FormWindowState.Normal;
                reader.Activate();
            }
            if (full.Count != 0) OpenWorkspace(full.ToArray(),false);
        }

        internal void OpenStudio(string[] paths) { OpenWorkspace(paths,true); }

        private void OpenVideoStudio(string[] paths,VideoPlayback playback) { OpenWorkspace(paths,true,playback); }

        private void OpenWorkspace(string[] paths,bool fullWorkspace,VideoPlayback playback=null)
        {
            if (studio == null || studio.IsDisposed)
            {
                studio = createStudio();
                studio.FormClosed += delegate { studio = null; FinishIfEmpty(); };
                studio.Show();
            }
            studio.OpenFromReader(paths,fullWorkspace,playback);
        }

        private void FinishIfEmpty()
        {
            if (studio == null && readers.Count == 0) ExitThread();
        }

        protected override void ExitThreadCore()
        {
            stopping = true;
            activation.Unregister(null);
            inbox.Dispose();
            dispatcher.Dispose();
            base.ExitThreadCore();
        }
    }

    internal sealed class VideoPlayback
    {
        public double time; public bool playing; public double volume; public bool muted; public double rate; public bool loop;
    }

    internal static class QuickReaderDocument
    {
        internal const int MaximumBytes = 8 * 1024 * 1024;
        internal static bool Supports(string path)
        {
            switch (Path.GetExtension(path).ToLowerInvariant())
            {
                case ".md": case ".markdown": case ".txt": case ".json":
                case ".csv": case ".srt": case ".vtt": return true;
                default: return false;
            }
        }

        internal static string Read(string path)
        {
            path = Hub.ResolveOpenedFilePath(path);
            if (!Supports(path)) throw new InvalidDataException("此类型请在映序主端中打开。");
            byte[] raw;
            using (var stream = new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.ReadWrite | FileShare.Delete))
            {
                if (stream.Length > MaximumBytes) throw new InvalidDataException("此文件超过 8 MiB，请在映序主端中打开。");
                using (var buffer = new MemoryStream())
                {
                    byte[] chunk = new byte[65536]; int count;
                    while ((count = stream.Read(chunk,0,chunk.Length)) > 0)
                    {
                        if (buffer.Length + count > MaximumBytes) throw new InvalidDataException("文件过大，请在映序主端中打开。");
                        buffer.Write(chunk,0,count);
                    }
                    raw = buffer.ToArray();
                }
            }
            // Preserve the exact source; decoding and rendering never write it back.
            string text;
            if (raw.Length >= 4 && raw[0]==255 && raw[1]==254 && raw[2]==0 && raw[3]==0)
                text = new UTF32Encoding(false,true,true).GetString(raw,4,raw.Length-4);
            else if (raw.Length >= 4 && raw[0]==0 && raw[1]==0 && raw[2]==254 && raw[3]==255)
                text = new UTF32Encoding(true,true,true).GetString(raw,4,raw.Length-4);
            else if (raw.Length >= 2 && raw[0]==255 && raw[1]==254)
                text = new UnicodeEncoding(false,true,true).GetString(raw,2,raw.Length-2);
            else if (raw.Length >= 2 && raw[0]==254 && raw[1]==255)
                text = new UnicodeEncoding(true,true,true).GetString(raw,2,raw.Length-2);
            else
            {
                int skip = raw.Length>=3 && raw[0]==239 && raw[1]==187 && raw[2]==191 ? 3 : 0;
                try { text = new UTF8Encoding(false,true).GetString(raw,skip,raw.Length-skip); }
                catch (DecoderFallbackException) { text = Encoding.GetEncoding(54936,EncoderFallback.ExceptionFallback,DecoderFallback.ExceptionFallback).GetString(raw); }
            }
            if (text.IndexOf('\0') >= 0) throw new InvalidDataException("文件包含非文本内容，请在映序主端中打开。");
            return text;
        }

        internal static string Resource(string name)
        {
            using (var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream(name))
            using (var reader = new StreamReader(stream,Encoding.UTF8)) return reader.ReadToEnd();
        }
        internal static string Html()
        {
            return Resource("quick-reader.html").Replace("/*READER_STYLE*/",Resource("quick-reader.css"))
                .Replace("/*READER_SCRIPTS*/",Resource("obsidian-images.js")+"\n"+Resource("markdown-preview.js")+"\n"+Resource("quick-reader.js"));
        }
    }

    internal sealed class QuickReaderWindow : QuickWindowChrome
    {
        private readonly string path;
        private readonly bool video;
        private readonly ToolStrip bar;
        private readonly WebView2 web;
        private readonly Label loading;
        private volatile bool closed;
        private QuickVideoFile videoFile;
        private readonly ToolStripButton zoom;
        private readonly System.Diagnostics.Stopwatch startup = System.Diagnostics.Stopwatch.StartNew();

        internal QuickReaderWindow(string file,Action<string[]> openStudio, bool initialize = true,Action<string[],VideoPlayback> openVideoStudio=null)
        {
            path = Hub.ResolveOpenedFilePath(file);
            video=QuickVideoResource.Supports(path);
            Text = Path.GetFileName(file) + (video?" · 映序快速播放":" · 映序快速阅览");
            AutoScaleMode = AutoScaleMode.Dpi;
            MinimumSize = new Size(520,420);
            ClientSize = video?new Size(1040,640):new Size(920,760);
            StartPosition = FormStartPosition.CenterScreen;
            BackColor = Color.White;
            Font = new Font("Microsoft YaHei UI",10F);
            using (var source = Assembly.GetExecutingAssembly().GetManifestResourceStream("brand.ico")) Icon = new Icon(source);
            web = new WebView2 { Dock=DockStyle.Fill,DefaultBackgroundColor=video?Color.Black:Color.White };
            loading = new Label { Dock=DockStyle.Fill,Text=video?"正在打开视频…":"正在打开文本…",TextAlign=ContentAlignment.MiddleCenter };
            bar = new ToolStrip { Dock=DockStyle.Top,GripStyle=ToolStripGripStyle.Hidden,Padding=new Padding(12,6,12,6),BackColor=Color.White,Font=Font };
            bar.Items.Add(new ToolStripLabel("快速阅览 · 只读"));
            bar.Items.Add(new ToolStripSeparator());
            var smaller = new ToolStripButton("−"); smaller.ToolTipText="缩小文字";
            smaller.Click += delegate { ChangeZoom(-0.1); }; bar.Items.Add(smaller);
            zoom = new ToolStripButton("100%"); zoom.ToolTipText="恢复文字大小";
            zoom.Click += delegate { if (web.CoreWebView2 != null) { web.ZoomFactor=1; zoom.Text="100%"; } }; bar.Items.Add(zoom);
            var larger = new ToolStripButton("＋"); larger.ToolTipText="放大文字";
            larger.Click += delegate { ChangeZoom(0.1); }; bar.Items.Add(larger);
            StudioButton.Click += async delegate
            {
                try {
                    if(video && web.CoreWebView2!=null) {
                        string value=await web.CoreWebView2.ExecuteScriptAsync("(()=>{const v=document.querySelector('video');if(!v)return null;const s={time:v.currentTime,playing:!v.paused,volume:v.volume,muted:v.muted,rate:v.playbackRate,loop:v.loop};v.pause();if(document.fullscreenElement)document.exitFullscreen();return s;})()");
                        SetPlayerFullScreen(false);
                        var playback=new JavaScriptSerializer().Deserialize<VideoPlayback>(value);
                        if(openVideoStudio!=null && playback!=null) {openVideoStudio(new[]{Hub.ResolveOpenedFilePath(path)},playback);return;}
                    }
                    openStudio(new[] { Hub.ResolveOpenedFilePath(path) });
                }
                catch (Exception error) { MessageBox.Show(this,error.Message,"映序",MessageBoxButtons.OK,MessageBoxIcon.Warning); }
            };
            Controls.Add(web); Controls.Add(loading); if(!video)Controls.Add(bar);
            FinishCaptionLayout();
            Resize+=delegate { if(video && WindowState==FormWindowState.Minimized && web.CoreWebView2!=null)web.CoreWebView2.PostWebMessageAsJson("{\"action\":\"pause\"}"); };
            if (initialize) Shown += async delegate { await InitializeAsync(); };
            FormClosed += delegate { closed=true; web.Dispose(); if(videoFile!=null)videoFile.Dispose(); if(video)bar.Dispose(); };
        }

        private void ChangeZoom(double delta)
        {
            if (web.CoreWebView2 != null) { web.ZoomFactor=Math.Max(0.5,Math.Min(3,web.ZoomFactor+delta)); zoom.Text=Math.Round(web.ZoomFactor*100)+"%"; }
        }

        private void InitializeVideo(CoreWebView2Environment environment)
        {
            var core=web.CoreWebView2;
            videoFile=new QuickVideoFile(Hub.ResolveOpenedFilePath(path));
            string pageUri="https://yingxu-reader.invalid/"+Guid.NewGuid().ToString("N")+"/index.html";
            string mediaUri=new Uri(new Uri(pageUri),"media").AbsoluteUri;
            string html=QuickReaderDocument.Resource("quick-video.html")
                .Replace("/*VIDEO_STYLE*/",QuickReaderDocument.Resource("video-player.css"))
                .Replace("/*VIDEO_SCRIPTS*/",QuickReaderDocument.Resource("video-player.js")+"\n"+QuickReaderDocument.Resource("quick-video.js"));
            core.AddWebResourceRequestedFilter("*",CoreWebView2WebResourceContext.All);
            core.WebResourceRequested+=(sender,e)=> {
                string uri=e.Request.Uri,method=e.Request.Method;
                if(uri==pageUri && method=="GET") {
                    e.Response=environment.CreateWebResourceResponse(new MemoryStream(Encoding.UTF8.GetBytes(html)),200,"OK","Content-Type: text/html; charset=utf-8\r\nCache-Control: no-store");return;
                }
                if(uri!=mediaUri || (method!="GET" && method!="HEAD")) { e.Response=environment.CreateWebResourceResponse(null,403,"Blocked","");return; }
                try {
                    long length=videoFile.Length,start,end;
                    string range=e.Request.Headers.Contains("Range")?e.Request.Headers.GetHeader("Range"):null;
                    if(!QuickVideoResource.Range(range,length,out start,out end)) {
                        e.Response=environment.CreateWebResourceResponse(null,416,"Range Not Satisfiable","Content-Range: bytes */"+length+"\r\nContent-Length: 0");return;
                    }
                    bool partial=!String.IsNullOrEmpty(range);
                    string headers="Content-Type: "+QuickVideoResource.Mime(path)+"\r\nAccept-Ranges: bytes\r\nCache-Control: no-store\r\nContent-Length: "+Math.Max(0,end-start+1);
                    if(partial)headers+="\r\nContent-Range: bytes "+start+"-"+end+"/"+length;
                    e.Response=environment.CreateWebResourceResponse(method=="HEAD"?null:new QuickVideoStream(videoFile,start,end,()=>closed),partial?206:200,partial?"Partial Content":"OK",headers);
                } catch(Exception error) { Hub.Log("quick_video_stream "+error.Message);e.Response=environment.CreateWebResourceResponse(null,404,"Unavailable",""); }
            };
            core.NavigationStarting+=(sender,e)=>{if(e.Uri!=pageUri)e.Cancel=true;};
            core.FrameNavigationStarting+=(sender,e)=>{e.Cancel=true;};
            core.NewWindowRequested+=(sender,e)=>{e.Handled=true;};
            core.PermissionRequested+=(sender,e)=>{e.State=CoreWebView2PermissionState.Deny;};
            core.DownloadStarting+=(sender,e)=>{e.Cancel=true;};
            core.ContainsFullScreenElementChanged+=delegate { SetPlayerFullScreen(core.ContainsFullScreenElement); };
            core.ProcessFailed+=delegate { if(!closed){SetPlayerFullScreen(false);loading.Text="播放窗口暂时不可用，请重新打开。";loading.Show();loading.BringToFront();} };
            core.NavigationCompleted+=(sender,e)=> {
                if(closed)return;
                if(!e.IsSuccess){loading.Text="视频页面加载失败，请重新打开。";return;}
                core.PostWebMessageAsJson(new JavaScriptSerializer().Serialize(new {action="open-video",name=Path.GetFileName(path),src=mediaUri}));
                loading.Hide();web.Focus();
            };
            core.Navigate(pageUri);
        }

        internal async Task InitializeAsync()
        {
            try
            {
                var content = Task.Run(() => video?"":QuickReaderDocument.Read(path));
                // Observe file errors even if browser initialization fails or the window closes.
                content.ContinueWith(t => { var ignored=t.Exception; },TaskContinuationOptions.OnlyOnFaulted);
                string folder = Hub.BundledBrowserFolder();
                if (Environment.GetEnvironmentVariable("YINGXU_WEBVIEW2_MODE") != "bundled")
                {
                    try
                    {
                        string installed=CoreWebView2Environment.GetAvailableBrowserVersionString();
                        if (folder==null || CoreWebView2Environment.CompareBrowserVersions(installed,CoreWebView2Environment.GetAvailableBrowserVersionString(folder))>=0) folder=null;
                    }
                    catch (WebView2RuntimeNotFoundException) { }
                }
                if (folder!=null) await Task.Run(() => Hub.PrepareBrowserFolder(folder));
                if (closed) return;
                var environment=await CoreWebView2Environment.CreateAsync(folder,Path.Combine(Hub.Cache,"WebView2"),new CoreWebView2EnvironmentOptions { Language="zh-CN",AdditionalBrowserArguments="--autoplay-policy=no-user-gesture-required" });
                if (closed) return;
                await web.EnsureCoreWebView2Async(environment);
                if (closed) return;
                var core=web.CoreWebView2;
                core.Profile.PreferredColorScheme=CoreWebView2PreferredColorScheme.Light;
                core.Settings.AreDevToolsEnabled=false;
                core.Settings.AreHostObjectsAllowed=false;
                core.Settings.AreBrowserAcceleratorKeysEnabled=true;
                core.Settings.IsPasswordAutosaveEnabled=false;
                core.Settings.IsGeneralAutofillEnabled=false;
                core.Settings.IsStatusBarEnabled=false;
                core.Settings.AreDefaultContextMenusEnabled=false;
                if(video) { InitializeVideo(environment); return; }
                string html=QuickReaderDocument.Html();
                string pageUri="data:text/html;charset=utf-8;base64,"+Convert.ToBase64String(Encoding.UTF8.GetBytes(html));
                core.AddWebResourceRequestedFilter("*",CoreWebView2WebResourceContext.All);
                core.WebResourceRequested += (sender,e) => { if(e.Request.Uri!="about:blank" && e.Request.Uri!=pageUri)e.Response=environment.CreateWebResourceResponse(null,403,"Blocked",""); };
                core.NavigationStarting += (sender,e) => { if(e.Uri!="about:blank" && e.Uri!=pageUri)e.Cancel=true; };
                core.FrameNavigationStarting += (sender,e) => { e.Cancel=true; };
                core.NewWindowRequested += (sender,e) => { e.Handled=true; };
                core.PermissionRequested += (sender,e) => { e.State=CoreWebView2PermissionState.Deny; };
                core.DownloadStarting += (sender,e) => { e.Cancel=true; };
                core.ProcessFailed += delegate { if(!closed) { loading.Text="阅览窗口暂时不可用，请关闭后重新打开，或在映序中打开。";loading.Show();loading.BringToFront(); } };
                web.ZoomFactorChanged += delegate { zoom.Text=Math.Round(web.ZoomFactor*100)+"%"; };
                string text = await content;
                if (closed) return;
                var payload = new JavaScriptSerializer { MaxJsonLength=32*1024*1024 }.Serialize(new {name=Path.GetFileName(path),content=text,markdown=Path.GetExtension(path).Equals(".md",StringComparison.OrdinalIgnoreCase)||Path.GetExtension(path).Equals(".markdown",StringComparison.OrdinalIgnoreCase)});
                core.NavigationCompleted += (sender,e) =>
                {
                    if(closed)return;
                    if(!e.IsSuccess) { loading.Text="阅览页面加载失败，请关闭后重试。"+e.WebErrorStatus;return; }
                    core.PostWebMessageAsJson(payload);
                    loading.Hide(); web.Focus();
                    Hub.Log("quick_reader_ready elapsed_ms="+startup.ElapsedMilliseconds);
                };
                core.NavigateToString(html);
            }
            catch(Exception error) { Hub.Log("quick_reader_error "+error.GetType().Name+": "+error.Message+" "+error.StackTrace); if(!closed)loading.Text="无法快速阅览\n\n"+error.Message+"\n\n可使用右上角按钮在映序中打开。"; }
        }
    }
}
