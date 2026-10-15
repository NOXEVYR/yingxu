using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
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
    internal static class QuickReaderTests
    {
        private static int count;
        private static void Check(bool value,string name)
        {
            if(!value)throw new Exception("FAILED: "+name);
            Console.WriteLine("PASS "+name);count++;
        }
        private static object Field(object value,string name)
        {
            return value.GetType().GetField(name,BindingFlags.NonPublic|BindingFlags.Instance).GetValue(value);
        }
        private static void Pump(Task work)
        {
            var until=DateTime.UtcNow.AddSeconds(25);
            while(!work.IsCompleted && DateTime.UtcNow<until) { Application.DoEvents();Thread.Sleep(10); }
            if(!work.IsCompleted)throw new TimeoutException("Reader integration timed out");
            work.GetAwaiter().GetResult();
        }
        private static void RunOnUiMessageLoop(Func<Task> action)
        {
            Exception failure=null;
            using(var loop=new Form {ShowInTaskbar=false,Opacity=0,WindowState=FormWindowState.Minimized,Text="YingXu native fixture loop"})
            {
                loop.Shown+=async delegate
                {
                    try { await action(); }
                    catch(Exception error) { failure=error; }
                    finally { loop.Close(); }
                };
                Application.Run(loop);
            }
            if(failure!=null)throw new InvalidOperationException("Native WebView fixtures failed: "+failure);
        }
        private static string Script(WebView2 web,string code)
        {
            var task=web.CoreWebView2.ExecuteScriptAsync(code);Pump(task);return task.Result;
        }
        private static Task<string> ScriptAsync(CoreWebView2 web,string code) { return web.ExecuteScriptAsync(code); }
        private static async Task WaitForScriptAsync(CoreWebView2 web,string expression,string name,int seconds=20)
        {
            DateTime until=DateTime.UtcNow.AddSeconds(seconds);string last="";
            while(DateTime.UtcNow<until)
            {
                last=await ScriptAsync(web,expression);
                if(last=="true")return;
                await Task.Delay(20);
            }
            throw new TimeoutException("Timed out waiting for "+name+"; last WebView result="+last);
        }
        private static async Task WaitForConditionAsync(Func<bool> condition,string name,int seconds=20)
        {
            DateTime until=DateTime.UtcNow.AddSeconds(seconds);
            while(DateTime.UtcNow<until) { if(condition())return;await Task.Delay(20); }
            throw new TimeoutException("Timed out waiting for "+name);
        }
        private static void WaitFor(Func<bool> condition,string name,int seconds=20)
        {
            DateTime until=DateTime.UtcNow.AddSeconds(seconds);
            while(DateTime.UtcNow<until) { Application.DoEvents();if(condition())return;Thread.Sleep(20); }
            throw new TimeoutException("Timed out waiting for "+name);
        }
        private static bool CanOpenExclusively(string path)
        {
            try { using(var stream=new FileStream(path,FileMode.Open,FileAccess.ReadWrite,FileShare.None))return true; }
            catch(IOException) { return false; }
        }
        private static string QuoteArgument(string value) { return "\""+value.Replace("\\","\\\\").Replace("\"","\\\"")+"\""; }
        private static string CreateVideoFixture(string temporary,string runtimeRoot)
        {
            string ffmpeg=Path.Combine(runtimeRoot,"ffmpeg","bin","ffmpeg.exe");
            if(!File.Exists(ffmpeg))throw new FileNotFoundException("The locked test runtime must include FFmpeg.",ffmpeg);
            string file=Path.Combine(temporary,"synthetic-video.mp4");
            var start=new ProcessStartInfo(ffmpeg,"-hide_banner -loglevel error -y -f lavfi -i \"testsrc2=size=160x90:rate=25\" -t 6 -an -c:v libx264 -preset ultrafast -crf 30 -pix_fmt yuv420p -movflags +faststart "+QuoteArgument(file))
                {UseShellExecute=false,CreateNoWindow=true,WindowStyle=ProcessWindowStyle.Hidden,RedirectStandardOutput=true,RedirectStandardError=true};
            using(var process=Process.Start(start))
            {
                if(!process.WaitForExit(30000)){try{process.Kill();}catch{}throw new TimeoutException("Synthetic video generation exceeded 30 seconds.");}
                string errors=process.StandardError.ReadToEnd();
                if(process.ExitCode!=0||!File.Exists(file)||new FileInfo(file).Length<1024)throw new InvalidOperationException("Locked FFmpeg could not generate the native video fixture: "+errors);
            }
            Check(File.Exists(file)&&new FileInfo(file).Length>1024,"locked FFmpeg generated isolated H.264 playback fixture");
            return file;
        }
        private static string Json(object value) { return new JavaScriptSerializer().Serialize(value); }
        private static async Task ClickTrusted(CoreWebView2 web,string selector)
        {
            var serializer=new JavaScriptSerializer();
            string raw=await web.ExecuteScriptAsync("JSON.stringify((()=>{const r=document.querySelector("+Json(selector)+").getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2}})())");
            var point=serializer.Deserialize<Dictionary<string,object>>(serializer.Deserialize<string>(raw));
            double x=Convert.ToDouble(point["x"]),y=Convert.ToDouble(point["y"]);
            await web.CallDevToolsProtocolMethodAsync("Input.dispatchMouseEvent",serializer.Serialize(new{type="mousePressed",x=x,y=y,button="left",buttons=1,clickCount=1}));
            await web.CallDevToolsProtocolMethodAsync("Input.dispatchMouseEvent",serializer.Serialize(new{type="mouseReleased",x=x,y=y,button="left",buttons=0,clickCount=1}));
        }
        private static async Task<double[]> BoundsAsync(CoreWebView2 web,string selector)
        {
            var serializer=new JavaScriptSerializer();
            string raw=await web.ExecuteScriptAsync("JSON.stringify((()=>{const n=document.querySelector("+Json(selector)+");if(!n)throw Error('missing '+"+Json(selector)+");const r=n.getBoundingClientRect();return [r.left,r.top,r.width,r.height]})())");
            return serializer.Deserialize<double[]>(serializer.Deserialize<string>(raw));
        }
        private static async Task MouseAsync(CoreWebView2 web,string type,double x,double y,string button,int buttons)
        {
            await web.CallDevToolsProtocolMethodAsync("Input.dispatchMouseEvent",
                new JavaScriptSerializer().Serialize(new{type=type,x=x,y=y,button=button,buttons=buttons,clickCount=1}));
        }
        private static async Task KeyTrusted(CoreWebView2 web,string key,string code,int virtualKey)
        {
            var serializer=new JavaScriptSerializer();
            await web.CallDevToolsProtocolMethodAsync("Input.dispatchKeyEvent",serializer.Serialize(new{type="keyDown",key=key,code=code,windowsVirtualKeyCode=virtualKey,nativeVirtualKeyCode=virtualKey}));
            await web.CallDevToolsProtocolMethodAsync("Input.dispatchKeyEvent",serializer.Serialize(new{type="keyUp",key=key,code=code,windowsVirtualKeyCode=virtualKey,nativeVirtualKeyCode=virtualKey}));
        }
        private static void RejectOpenedPath(string path,string name)
        {
            bool rejected=false;
            try { Hub.ResolveOpenedFilePath(path); }
            catch(ArgumentException) { rejected=true; }
            catch(InvalidDataException) { rejected=true; }
            catch(IOException) { rejected=true; }
            catch(UnauthorizedAccessException) { rejected=true; }
            Check(rejected,name);
        }
        private static void JunctionPaths(string temporary)
        {
            string fixture=Path.Combine(temporary,"junction-fixture");Directory.CreateDirectory(fixture);
            string target=Path.Combine(fixture,"真实 文稿");Directory.CreateDirectory(target);
            string alias=Path.Combine(fixture,"别名 资料");
            string original="# 联接中文标题\r\n\r\n保留 **正文** 与 BOM。\r\n";
            string file=Path.Combine(target,"中文 笔记.md"),opened=Path.Combine(alias,"中文 笔记.md");
            File.WriteAllText(file,original,new UTF8Encoding(true));byte[] before=File.ReadAllBytes(file);
            string command="$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path '"+alias.Replace("'","''")+"' -Target '"+target.Replace("'","''")+"' | Out-Null";
            var start=new ProcessStartInfo(Path.Combine(Environment.SystemDirectory,"WindowsPowerShell","v1.0","powershell.exe"),
                "-NoProfile -NonInteractive -EncodedCommand "+Convert.ToBase64String(Encoding.Unicode.GetBytes(command))) {
                UseShellExecute=false,CreateNoWindow=true,WindowStyle=ProcessWindowStyle.Hidden,RedirectStandardOutput=true,RedirectStandardError=true };
            try
            {
                using(var process=Process.Start(start))
                {
                    if(!process.WaitForExit(15000))throw new TimeoutException("Junction fixture creation did not complete");
                    string errors=process.StandardError.ReadToEnd();
                    Check(process.ExitCode==0,"temporary directory junction created without elevation: "+errors);
                }
                Check((File.GetAttributes(alias)&FileAttributes.ReparsePoint)!=0,"fixture is a real filesystem junction");
                string canonical=Hub.ResolveOpenedFilePath(file);
                Check(String.Equals(canonical,Path.GetFullPath(file),StringComparison.OrdinalIgnoreCase),"ordinary shell path remains canonical");
                Check(String.Equals(Hub.ResolveOpenedFilePath(opened),canonical,StringComparison.OrdinalIgnoreCase),"shell junction path resolves to real file");
                Check(LaunchOptions.Parse(new[]{"--open",opened},Hub.Root).Paths.Single()==canonical,"explicit shell open canonicalizes junction");
                Check(LaunchOptions.Parse(new[]{opened},Hub.Root).Paths.Single()==canonical,"positional shell open canonicalizes junction");
                Check(OpenInbox.Decode(OpenInbox.Encode(new[]{opened})).Single()==canonical,"IPC decode canonicalizes junction path");
                Check(QuickReaderDocument.Read(opened)==original,"junction reader retains exact Chinese text");
                bool rejected=false;try { Hub.ValidateNativeFilePath(opened); } catch(InvalidDataException) { rejected=true; }
            catch(IOException) { rejected=true; }
                Check(rejected,"registered native path validator still rejects junctions");
                string[] handed=null;
                using(var window=new QuickReaderWindow(opened,paths=>handed=paths,initialize:false))
                {
                    window.Show();Application.DoEvents();
                    var button=window.StudioButton;
                    button.PerformClick();
                    Check(handed!=null&&handed.Length==1&&handed[0]==canonical,"junction reader workspace handoff uses canonical file");
                }
                string executable=Path.Combine(target,"blocked.exe");File.WriteAllBytes(executable,new byte[]{77,90});
                string directory=Path.Combine(target,"folder.md");Directory.CreateDirectory(directory);
                RejectOpenedPath("relative.md","relative shell file rejected");
                RejectOpenedPath(@"\\localhost\share\note.md","UNC shell file rejected before access");
                RejectOpenedPath(file+":other.md","ADS shell file rejected");
                RejectOpenedPath(@"\\?\"+file,"extended device shell path rejected");
                RejectOpenedPath(@"\\.\NUL","device shell path rejected");
                RejectOpenedPath(Path.Combine(alias,"blocked.exe"),"executable shell file rejected");
                RejectOpenedPath(Path.Combine(alias,"folder.md"),"directory disguised as Markdown rejected");
                RejectOpenedPath(Path.Combine(alias,"missing.md"),"missing shell file rejected");
                Check(before.SequenceEqual(File.ReadAllBytes(file)),"junction routing and handoff preserve original bytes and BOM");
            }
            finally
            {
                // Never recursively delete a junction: unlink this fixture alias
                // only, before the build runner removes the ordinary temp root.
                if(Directory.Exists(alias))Directory.Delete(alias);
            }
            Check(File.Exists(file)&&before.SequenceEqual(File.ReadAllBytes(file)),"junction cleanup leaves target and original bytes intact");
        }
        [STAThread]
        private static int Main(string[] args)
        {
            string fixtureRoot=Path.GetFullPath(args.Length>2?args[2]:Path.GetTempPath());
            if(!Directory.Exists(fixtureRoot)||(File.GetAttributes(fixtureRoot)&FileAttributes.ReparsePoint)!=0)throw new InvalidDataException("Native test fixture root must be an existing ordinary directory.");
            string temporary=Path.GetFullPath(Path.Combine(fixtureRoot,"yingxu-lifecycle-reader-"+Guid.NewGuid().ToString("N")));
            Directory.CreateDirectory(temporary);
            Hub.Root=args[0];Hub.Data=Path.Combine(temporary,"data");Hub.Cache=Path.Combine(temporary,"cache");
            Directory.CreateDirectory(Hub.Data);Directory.CreateDirectory(Hub.Cache);
            Environment.SetEnvironmentVariable("YINGXU_DATA_DIR",Hub.Data);
            Environment.SetEnvironmentVariable("YINGXU_PROJECTS_DIR",Path.Combine(temporary,"projects"));
            Hub.Url="http://127.0.0.1:1/";Hub.Port=1;
            Program.InstanceKey=Guid.NewGuid().ToString("N");Program.InitialFiles=new string[0];
            Program.ActivateEvent=new EventWaitHandle(false,EventResetMode.AutoReset);
            try
            {
                Application.EnableVisualStyles();
                typeof(Program).GetMethod("PrepareLibraries",BindingFlags.NonPublic|BindingFlags.Static).Invoke(null,null);
                Run(temporary,args.Length>1?Path.GetFullPath(args[1]):Path.Combine(Hub.Root,"runtime"));
                Console.WriteLine("Quick reader tests passed: "+count);
                return 0;
            }
            catch (Exception error) { Console.WriteLine("READER_ERROR "+error.GetType().Name+": "+error.Message); return 1; }
            finally
            {
                Program.ActivateEvent.Dispose();
                // Build runner removes only this isolated fixture after WebView exits.
                Console.WriteLine("ZOOM_FIXTURE_CLEANUP_AFTER_EXIT="+temporary);
            }
        }
        private static async Task NativeQuickVideoTests(string file,string browserRoot)
        {
            string previous=Environment.GetEnvironmentVariable("YINGXU_WEBVIEW2_MODE");
            Environment.SetEnvironmentVariable("YINGXU_WEBVIEW2_MODE","bundled");
            QuickReaderWindow window=null;
            try
            {
                VideoPlayback handed=null;string[] handedPaths=null;
                window=new QuickReaderWindow(file,paths=>handedPaths=paths,initialize:false,openVideoStudio:(paths,playback)=>{handedPaths=paths;handed=playback;});
                window.Show();Application.DoEvents();
                var web=(WebView2)Field(window,"web");
                var environment=await CoreWebView2Environment.CreateAsync(browserRoot,Path.Combine(Hub.Cache,"native-quick-video-profile"),new CoreWebView2EnvironmentOptions { Language="zh-CN",AdditionalBrowserArguments="--autoplay-policy=no-user-gesture-required" });
                await web.EnsureCoreWebView2Async(environment);
                int mediaRequests=0,rangeRequests=0;
                web.CoreWebView2.WebResourceRequested+=(sender,e)=>{if(e.Request.Uri.EndsWith("/media",StringComparison.Ordinal)){mediaRequests++;if(e.Request.Headers.Contains("Range"))rangeRequests++;}};
                typeof(QuickReaderWindow).GetMethod("InitializeVideo",BindingFlags.Instance|BindingFlags.NonPublic).Invoke(window,new object[]{environment});
                await WaitForScriptAsync(web.CoreWebView2,"(()=>{const v=document.querySelector('video');return !!v&&v.readyState>=2&&!v.paused&&v.currentTime>.2})()","real WebView2 QuickReader playback",25);
                Check(await ScriptAsync(web.CoreWebView2,"document.querySelector('video').duration>5") == "true","native reader decodes synthetic video metadata");
                Check(mediaRequests>0,"native reader serves video through its bounded media route");
                await ScriptAsync(web.CoreWebView2,"document.querySelector('video').currentTime=2.2");
                await WaitForScriptAsync(web.CoreWebView2,"(()=>{const v=document.querySelector('video');return !v.seeking&&Math.abs(v.currentTime-2.2)<.2})()","native media seek across the local range route");
                Check(rangeRequests>0,"native WebView media seek requests byte ranges");

                // Exercise the controls as a desktop user does. A native CDP
                // pointer traverses the real WebView hit area; no DOM event is
                // dispatched from script to fake a hover or slider gesture.
                var screen=await BoundsAsync(web.CoreWebView2,".video-screen");
                await WaitForScriptAsync(web.CoreWebView2,"document.querySelector('.video-screen').classList.contains('controls-hidden')","playing quick reader auto-hides its controls",10);
                await MouseAsync(web.CoreWebView2,"mouseMoved",screen[0]+screen[2]*.5,screen[1]+screen[3]*.22,"none",0);
                await MouseAsync(web.CoreWebView2,"mouseMoved",screen[0]+screen[2]*.5,screen[1]+screen[3]*.96,"none",0);
                await WaitForScriptAsync(web.CoreWebView2,"(()=>{const s=document.querySelector('.video-screen'),c=document.querySelector('.video-controls');return !s.classList.contains('controls-hidden')&&Number(getComputedStyle(c).opacity)>.95})()","moving the real pointer down reveals the quick-player seek controls",5);
                Check(await ScriptAsync(web.CoreWebView2,"(()=>{const s=document.querySelector('.video-screen').getBoundingClientRect(),c=document.querySelector('.video-seek').getBoundingClientRect();return c.width>100&&c.top>=s.top&&c.bottom<=s.bottom})()")=="true","revealed progress slider remains visible inside the quick-player viewport");

                // Drag the actual range input with mousePressed/mouseMoved/
                // mouseReleased and verify the media clock follows the drag.
                await ScriptAsync(web.CoreWebView2,"document.querySelector('video').currentTime=.2");
                await WaitForScriptAsync(web.CoreWebView2,"(()=>{const v=document.querySelector('video');return !v.seeking&&!v.paused&&Math.abs(v.currentTime-.2)<.08})()","reset the quick-player clock before the drag",5);
                var seek=await BoundsAsync(web.CoreWebView2,".video-seek");
                double seekY=seek[1]+seek[3]/2;
                await MouseAsync(web.CoreWebView2,"mouseMoved",seek[0]+seek[2]*.34,seekY,"none",0);
                await MouseAsync(web.CoreWebView2,"mousePressed",seek[0]+seek[2]*.34,seekY,"left",1);
                await WaitForScriptAsync(web.CoreWebView2,"document.querySelector('video').paused","native seek drag temporarily pauses playback",2);
                await MouseAsync(web.CoreWebView2,"mouseMoved",seek[0]+seek[2]*.72,seekY,"left",1);
                await WaitForScriptAsync(web.CoreWebView2,"(()=>{const v=document.querySelector('video'),s=document.querySelector('.video-seek');return v.paused&&Math.abs(Number(s.value)-v.duration*.72)<.35})()","real pointer drag moves the quick-player seek thumb while playback is held",3);
                await MouseAsync(web.CoreWebView2,"mouseReleased",seek[0]+seek[2]*.72,seekY,"left",0);
                await WaitForScriptAsync(web.CoreWebView2,"(()=>{const v=document.querySelector('video');return !v.seeking&&!v.paused&&Math.abs(v.currentTime-v.duration*.72)<.35})()","real range drag scrubs to its thumb and resumes playback",3);
                Check(await ScriptAsync(web.CoreWebView2,"!document.querySelector('video').paused")=="true","quick-player drag scrub resumes its prior playing state");

                // Focus the player and send a trusted keyboard seek after the
                // controls have auto-hidden again. The paused state must keep
                // the controls available without another pointer gesture.
                await ScriptAsync(web.CoreWebView2,"document.querySelector('video').currentTime=.4;document.activeElement.blur()");
                await MouseAsync(web.CoreWebView2,"mouseMoved",screen[0]+screen[2]*.5,screen[1]+screen[3]*.22,"none",0);
                await WaitForScriptAsync(web.CoreWebView2,"document.querySelector('.video-screen').classList.contains('controls-hidden')","playing controls hide before the focus test",10);
                await ScriptAsync(web.CoreWebView2,"document.querySelector('.video-screen').focus({preventScroll:true})");
                await WaitForScriptAsync(web.CoreWebView2,"!document.querySelector('.video-screen').classList.contains('controls-hidden')","keyboard focus reveals quick-player controls",5);
                await KeyTrusted(web.CoreWebView2,"ArrowLeft","ArrowLeft",37);
                await WaitForScriptAsync(web.CoreWebView2,"(()=>{const v=document.querySelector('video');return v.currentTime<.2})()","focused quick player responds to a trusted left-arrow seek",5);
                Check(await ScriptAsync(web.CoreWebView2,"document.activeElement===document.querySelector('.video-screen')&&!document.querySelector('video').paused")=="true","focused keyboard seek preserves the playing state");
                await ScriptAsync(web.CoreWebView2,"document.querySelector('video').currentTime=.5");
                await ClickTrusted(web.CoreWebView2,"[data-command=play]");
                await WaitForScriptAsync(web.CoreWebView2,"document.querySelector('video').paused","quick-player pause button pauses playback",5);
                await Task.Delay(2400);
                Check(await ScriptAsync(web.CoreWebView2,"(()=>{const s=document.querySelector('.video-screen'),c=document.querySelector('.video-controls');return !s.classList.contains('controls-hidden')&&Number(getComputedStyle(c).opacity)>.95})()") == "true","paused quick-player controls stay visible after the hide interval");

                await ScriptAsync(web.CoreWebView2,"document.querySelector('video').currentTime=.5");
                await ClickTrusted(web.CoreWebView2,"[data-command=play]");
                await WaitForScriptAsync(web.CoreWebView2,"!document.querySelector('video').paused","quick-player resumes before the End-key boundary test",5);
                await ScriptAsync(web.CoreWebView2,"document.querySelector('.video-seek').focus({preventScroll:true})");
                await KeyTrusted(web.CoreWebView2,"End","End",35);
                await Task.Delay(250);
                Check(await ScriptAsync(web.CoreWebView2,"(()=>{const v=document.querySelector('video');return v.currentTime>=v.duration-.08&&v.paused})()") == "true","End on a playing quick video stops at the end without restarting from zero");

                await ClickTrusted(web.CoreWebView2,"[data-command=fullscreen]");
                await WaitForConditionAsync(()=>window.FormBorderStyle==FormBorderStyle.None&&!window.StudioButton.Parent.Visible,"real video fullscreen changes the native reader frame");
                Check(await ScriptAsync(web.CoreWebView2,"document.fullscreenElement===document.querySelector('.video-screen')")=="true","quick-player button enters actual WebView fullscreen");
                await ClickTrusted(web.CoreWebView2,"[data-command=fullscreen]");
                await WaitForConditionAsync(()=>window.FormBorderStyle==FormBorderStyle.Sizable&&window.StudioButton.Parent.Visible,"real video fullscreen restores native reader chrome");
                await ScriptAsync(web.CoreWebView2,"document.querySelector('video').currentTime=1.3");
                await ClickTrusted(web.CoreWebView2,"[data-command=play]");
                await WaitForScriptAsync(web.CoreWebView2,"(()=>{const v=document.querySelector('video');return !v.paused&&v.currentTime>1.4})()","resume at a mid-clip point before the workspace handoff");
                double before=Convert.ToDouble(await ScriptAsync(web.CoreWebView2,"document.querySelector('video').currentTime"));
                window.StudioButton.PerformClick();
                await WaitForConditionAsync(()=>handed!=null&&handedPaths!=null,"video workspace handoff");
                Check(handedPaths.Length==1&&String.Equals(handedPaths[0],Path.GetFullPath(file),StringComparison.OrdinalIgnoreCase),"video workspace handoff retains the canonical source path");
                Check(handed.playing&&Math.Abs(handed.time-before)<.08,"handoff pauses playback while preserving its exact current time and playing intent");
                await WaitForScriptAsync(web.CoreWebView2,"document.querySelector('video').paused","reader pauses after video handoff");
                Check(!CanOpenExclusively(file),"open quick reader pins the video source against replacement");
                var exited=new TaskCompletionSource<bool>();environment.BrowserProcessExited+=(sender,e)=>exited.TrySetResult(true);
                window.Close();window=null;
                if(await Task.WhenAny(exited.Task,Task.Delay(15000))!=exited.Task)throw new TimeoutException("WebView browser process did not exit after the reader closed.");
                await exited.Task;
                Check(CanOpenExclusively(file),"closing quick reader releases its video source handle after WebView exit");
            }
            finally
            {
                if(window!=null)window.Close();
                Environment.SetEnvironmentVariable("YINGXU_WEBVIEW2_MODE",previous);
            }
        }
        private static async Task NativeReviewFrameUiTests(string file,string browserRoot,string temporary)
        {
            string frontend=Path.Combine(Hub.Root,"frontend");
            string playerSource=File.ReadAllText(Path.Combine(frontend,"video-player.js"),new UTF8Encoding(false,true));
            string reviewSource=File.ReadAllText(Path.Combine(frontend,"video-review.js"),new UTF8Encoding(false,true));
            string framesSource=File.ReadAllText(Path.Combine(frontend,"video-frames.js"),new UTF8Encoding(false,true));
            string video="data:video/mp4;base64,"+Convert.ToBase64String(File.ReadAllBytes(file));
            string png;
            using(var bitmap=new Bitmap(16,16))using(var graphics=Graphics.FromImage(bitmap))using(var memory=new MemoryStream())
            { graphics.Clear(Color.DarkOliveGreen);graphics.FillRectangle(Brushes.White,0,0,8,8);bitmap.Save(memory,System.Drawing.Imaging.ImageFormat.Png);png="data:image/png;base64,"+Convert.ToBase64String(memory.ToArray()); }
            var serializer=new JavaScriptSerializer();
            string setup="window.nativeFixture={calls:[],indexStarted:0,indexEnded:0,loops:0,lastTime:0,image:"+serializer.Serialize(png)+"};"+
                "window.nativeVideoReviewPlayer=YingXuVideo.mount(document.getElementById('player'),{name:'native fixture',src:"+serializer.Serialize(video)+",autoplay:true});"+
                "window.nativeVideoReviewPlayer.video.addEventListener('timeupdate',()=>{const t=window.nativeVideoReviewPlayer.video.currentTime;if(document.querySelector('[data-review=ab]')?.getAttribute('aria-pressed')==='true'&&t<window.nativeFixture.lastTime-.12)window.nativeFixture.loops++;window.nativeFixture.lastTime=t});"+
                "window.nativeApi=async(url,options={})=>{const method=options.method||'GET',body=options.body||{};window.nativeFixture.calls.push({url,method,body});"+
                "if(url==='/api/video/frames/capability')return {available:true,reason:'本机合成原生 WebView 验收'};"+
                "if(url==='/api/video/review/file/native-video')return {revision:'fixture-r1',review:{markers:[],in:null,out:null,a:null,b:null}};"+
                "if(url.startsWith('/api/video/exports?'))return {jobs:[]};"+
                "if(url==='/api/video/frames/index'&&method==='POST'){window.nativeFixture.indexStarted=window.nativeVideoReviewPlayer.video.currentTime;await new Promise(r=>setTimeout(r,750));window.nativeFixture.indexEnded=window.nativeVideoReviewPlayer.video.currentTime;return {id:'index-fixture'}};"+
                "if(url==='/api/video/frames/jobs/index-fixture')return {state:'ready',kind:'index',count:20,frames:20,verified_frames:20,elapsed:.75};"+
                "if(url==='/api/video/frames/index/index-fixture/frame'&&method==='POST')return {id:'frame-'+body.ordinal};"+
                "if(url.startsWith('/api/video/frames/jobs/frame-')){const ordinal=Number(url.slice(url.lastIndexOf('-')+1));return {state:'done',count:1,ordinal,pts:String(ordinal),time_base:'1/25',source_sha256:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',decode_mode:'verified-prefix',resume_seconds_approximate:.8+ordinal*.04,image_url:window.nativeFixture.image}};"+
                "if(url.includes('/api/video/frames/index/index-fixture/cancel')||url.includes('/api/video/frames/jobs/frame-'))return {cancelled:true};"+
                "return {error:'unexpected_fixture_api'};};"+
                "window.nativeReviewTab={id:'native-video',source:'file',videoReviewOpen:true};window.nativeReview=YingXuVideoReview.mount(window.nativeVideoReviewPlayer,{tab:window.nativeReviewTab,api:window.nativeApi,container:document.querySelector('#fixture'),onDirty:()=>{}});";
            string html="<!doctype html><html><head><meta charset='utf-8'><style>html,body,#fixture,#player{height:100%;margin:0}.video-screen{height:75vh}.video-frame-inspector,.video-review-content{font:14px sans-serif}.frame-actions{display:flex;gap:4px}</style></head><body><main id='fixture'><div id='player'></div></main><script>"+playerSource+"</script><script>"+reviewSource+"</script><script>"+framesSource+"</script><script>"+setup+"</script></body></html>";
            using(var host=new Form {Text="YingXu native video review fixture",StartPosition=FormStartPosition.CenterScreen,ClientSize=new Size(900,680),ShowInTaskbar=false})
            using(var web=new WebView2 {Dock=DockStyle.Fill,DefaultBackgroundColor=Color.Black})
            {
                host.Controls.Add(web);host.Show();Application.DoEvents();
                var environment=await CoreWebView2Environment.CreateAsync(browserRoot,Path.Combine(Hub.Cache,"native-frame-review-profile"),new CoreWebView2EnvironmentOptions { Language="zh-CN",AdditionalBrowserArguments="--autoplay-policy=no-user-gesture-required" });
                await web.EnsureCoreWebView2Async(environment);
                var navigation=new TaskCompletionSource<bool>();web.CoreWebView2.NavigationCompleted+=(sender,e)=>navigation.TrySetResult(e.IsSuccess);web.NavigateToString(html);
                if(await Task.WhenAny(navigation.Task,Task.Delay(15000))!=navigation.Task)throw new TimeoutException("Native review fixture navigation timed out.");
                if(!await navigation.Task)throw new InvalidOperationException("Native review fixture navigation failed.");
                await WaitForScriptAsync(web.CoreWebView2,"(()=>{const v=document.querySelector('video');return !!v&&v.readyState>=2&&!v.paused&&v.currentTime>.2})()","native review fixture media playback",25);
                await WaitForScriptAsync(web.CoreWebView2,"document.querySelector('.review-status').textContent.includes('审片记录已载入')","real WebView loads the review controls");
                await ScriptAsync(web.CoreWebView2,"document.querySelector('[data-review-tab=markers]').click()");
                await WaitForScriptAsync(web.CoreWebView2,"document.querySelector('[data-review-tab=markers]').getAttribute('aria-selected')==='true'","review fixture opens the visible marker and A/B tools");
                await ScriptAsync(web.CoreWebView2,"window.nativeVideoReviewPlayer.seek(.45)");
                await WaitForScriptAsync(web.CoreWebView2,"Math.abs(window.nativeVideoReviewPlayer.video.currentTime-.45)<.08","review fixture A point seek");
                await ScriptAsync(web.CoreWebView2,"document.querySelector('[data-review=a]').click()");
                await ScriptAsync(web.CoreWebView2,"window.nativeVideoReviewPlayer.seek(1.1)");
                await WaitForScriptAsync(web.CoreWebView2,"Math.abs(window.nativeVideoReviewPlayer.video.currentTime-1.1)<.1","review fixture B point seek");
                await ScriptAsync(web.CoreWebView2,"document.querySelector('[data-review=b]').click();document.querySelector('[data-review=ab]').click()");
                await WaitForScriptAsync(web.CoreWebView2,"window.nativeFixture.loops>0","real video element follows the configured A/B loop");
                string abRaw=await ScriptAsync(web.CoreWebView2,"JSON.stringify({pressed:document.querySelector('[data-review=ab]').getAttribute('aria-pressed'),range:window.nativeReviewTab.videoReviewDraft&&[window.nativeReviewTab.videoReviewDraft.a,window.nativeReviewTab.videoReviewDraft.b],status:document.querySelector('.review-status').textContent})");
                var abState=serializer.Deserialize<Dictionary<string,object>>(serializer.Deserialize<string>(abRaw));
                Check((string)abState["pressed"]=="true","native review UI exposes the active A/B loop state");
                await ScriptAsync(web.CoreWebView2,"document.querySelector('[data-review=ab]').click()");
                double before=Convert.ToDouble(await ScriptAsync(web.CoreWebView2,"window.nativeVideoReviewPlayer.video.currentTime"));
                await ScriptAsync(web.CoreWebView2,"document.querySelector('[data-frame=start]').click()");
                await WaitForScriptAsync(web.CoreWebView2,"window.nativeFixture.indexEnded>window.nativeFixture.indexStarted+.2","frame indexing request leaves native video playback running",10);
                await WaitForScriptAsync(web.CoreWebView2,"document.querySelector('.frame-status').textContent.includes('索引已就绪')","strict frame index becomes ready",10);
                double after=Convert.ToDouble(await ScriptAsync(web.CoreWebView2,"window.nativeVideoReviewPlayer.video.currentTime"));
                Check(await ScriptAsync(web.CoreWebView2,"!window.nativeVideoReviewPlayer.video.paused") == "true"&&after>before+.2,"background index readiness does not pause or reset playback");
                await ScriptAsync(web.CoreWebView2,"document.querySelector('[data-frame=enter]').click()");
                await WaitForScriptAsync(web.CoreWebView2,"!document.querySelector('.frame-overlay').hidden","explicit frame inspection displays the decoded still",10);
                Check(await ScriptAsync(web.CoreWebView2,"window.nativeVideoReviewPlayer.video.paused") == "true","entering strict frame inspection pauses the native video");
                await ScriptAsync(web.CoreWebView2,"document.querySelector('[data-frame=next]').click()");
                await WaitForScriptAsync(web.CoreWebView2,"document.querySelector('.frame-overlay-label').textContent.includes('第 1 帧 ·')","strict next-frame button advances by displayed ordinal",10);
                Check(await ScriptAsync(web.CoreWebView2,"window.nativeVideoReviewPlayer.video.paused") == "true","strict adjacent frame remains a still while inspection is active");
                await ScriptAsync(web.CoreWebView2,"document.querySelector('[data-frame=resume]').click()");
                await WaitForScriptAsync(web.CoreWebView2,"document.querySelector('.frame-overlay').hidden&&!window.nativeVideoReviewPlayer.video.paused","return-to-video resumes at the approximate seek",10);
                await ScriptAsync(web.CoreWebView2,"document.querySelector('[data-frame=cancel]').click()");
                await WaitForScriptAsync(web.CoreWebView2,"document.querySelector('.frame-status').textContent.includes('已关闭')","strict frame session cancellation reaches the test API",10);
                Check(await ScriptAsync(web.CoreWebView2,"window.nativeFixture.calls.some(x=>x.url==='/api/video/frames/index/index-fixture/cancel'&&x.method==='POST')") == "true","closing strict inspection cancels its synthetic index session");
                Console.WriteLine("NATIVE_FRAME_UI_TIMES="+await ScriptAsync(web.CoreWebView2,"JSON.stringify({indexed:window.nativeFixture.indexEnded-window.nativeFixture.indexStarted,playhead:window.nativeVideoReviewPlayer.video.currentTime})"));
                await web.CoreWebView2.ExecuteScriptAsync("window.nativeReview.destroy();window.nativeVideoReviewPlayer.destroy()");
                host.Close();Application.DoEvents();
            }
        }
        private static void VideoFrameAndRangeTests(string temporary,string runtimeRoot)
        {
            long start,end;
            Check(QuickVideoResource.Range("bytes=2-5",10,out start,out end)&&start==2&&end==5,"bounded media Range");
            Check(QuickVideoResource.Range("bytes=-3",10,out start,out end)&&start==7&&end==9,"suffix media Range");
            Check(QuickVideoResource.Range("bytes=8-",10,out start,out end)&&start==8&&end==9,"open media Range");
            foreach(string value in new[]{"bytes=-","bytes=-0","bytes=10-","bytes=5-2","bytes=0-1,4-5","bytes=999999999999999999999-"})
                Check(!QuickVideoResource.Range(value,10,out start,out end),"invalid media Range rejected "+value);
            foreach(string ext in new[]{".mp4",".m4v",".mov",".webm",".mkv",".avi"})Check(QuickVideoResource.Supports("synthetic"+ext),"video quick routing "+ext);
            string file=Path.Combine(temporary,"range-fixture.mp4");File.WriteAllBytes(file,new byte[]{0,1,2,3,4,5,6,7,8,9});
            using(var source=new QuickVideoFile(file))
            using(var slice=new QuickVideoStream(source,2,5,()=>false)) {
                var buffer=new byte[10];Check(slice.Read(buffer,0,10)==4 && buffer[0]==2 && buffer[3]==5,"response stream stays inside range");
                Check(slice.Read(buffer,0,10)==0,"response stream ends at selected byte");slice.Seek(-1,SeekOrigin.End);
                Check(slice.Read(buffer,0,1)==1&&buffer[0]==5,"range stream relative seek");
            }
            using(var window=new QuickReaderWindow(file,paths=>{},initialize:false)) {
                window.Show();Application.DoEvents();var bounds=window.Bounds;
                var caption=window.StudioButton.Parent;
                Check(caption.Top==0 && caption.Visible,"workspace entry is in the top caption row");
                window.SetPlayerFullScreen(true);Application.DoEvents();
                Check(window.FormBorderStyle==FormBorderStyle.None && !caption.Visible,"native fullscreen removes frame and caption controls");
                window.SetPlayerFullScreen(false);Application.DoEvents();
                Check(window.Bounds==bounds && caption.Visible,"native fullscreen restores previous normal bounds");
                window.WindowState=FormWindowState.Maximized;window.SetPlayerFullScreen(true);window.SetPlayerFullScreen(false);Application.DoEvents();
                Check(window.WindowState==FormWindowState.Maximized,"native fullscreen restores maximized state");window.Close();
            }
            string browserRoot=Path.Combine(runtimeRoot,"webview2");
            if(File.Exists(Path.Combine(browserRoot,"msedgewebview2.exe")))
            {
                string synthetic=CreateVideoFixture(temporary,runtimeRoot);
                RunOnUiMessageLoop(async delegate
                {
                    await NativeQuickVideoTests(synthetic,browserRoot);
                    await NativeReviewFrameUiTests(synthetic,browserRoot,temporary);
                });
            }
            else Console.WriteLine("SKIP native video playback and review UI: no fixed WebView2 runtime was supplied");
        }

        private static void Run(string temporary,string runtimeRoot)
        {
            CoreWebView2Environment.SetLoaderDllFolderPath(Program.LoaderFolder);
            JunctionPaths(temporary);
            VideoFrameAndRangeTests(temporary,runtimeRoot);
            string file=Path.Combine(temporary,"中文 阅览.md");
            string original="# 中文标题\r\n\r\n正文 **加粗**\r\n\r\n<script>window.injected=true</script>\r\n![remote](https://example.invalid/image.png)\r\n";
            foreach(var encoding in new Encoding[] { new UTF8Encoding(true),new UnicodeEncoding(false,true),new UnicodeEncoding(true,true),Encoding.GetEncoding(54936) })
            {
                File.WriteAllText(file,original,encoding);
                byte[] before=File.ReadAllBytes(file);
                Check(QuickReaderDocument.Read(file)==original,"exact Chinese text and mixed formatting decoded "+encoding.WebName);
                Check(before.SequenceEqual(File.ReadAllBytes(file)),"reader never rewrites source "+encoding.WebName);
            }
            foreach(string ext in new[]{".md",".MARKDOWN",".txt",".json",".csv",".srt",".vtt"})Check(QuickReaderDocument.Supports("note"+ext),"quick routing "+ext);
            foreach(string ext in new[]{".docx",".pdf",".html",".svg",".png",".excalidraw",".xmind"})Check(!QuickReaderDocument.Supports("note"+ext),"workspace routing "+ext);
            string large=Path.Combine(temporary,"large.txt");
            using(var stream=File.Create(large))stream.SetLength(QuickReaderDocument.MaximumBytes+1);
            bool rejected=false;try { QuickReaderDocument.Read(large); } catch(InvalidDataException){rejected=true;}
            Check(rejected,"oversized file rejected before reading content");
            string binary=Path.Combine(temporary,"binary.txt");File.WriteAllBytes(binary,new byte[]{65,0,66});
            rejected=false;try { QuickReaderDocument.Read(binary); } catch(InvalidDataException){rejected=true;}
            Check(rejected,"binary disguised as text rejected");
            File.WriteAllText(file,original,new UTF8Encoding(true));
            byte[] saved=File.ReadAllBytes(file);
            string[] handed=null;
            using(var window=new QuickReaderWindow(file,paths=>handed=paths,initialize:false))
            {
                // Initialize without Shown to retain explicit completion in the test.
                window.Show();Application.DoEvents();
                SynchronizationContext.SetSynchronizationContext(new WindowsFormsSynchronizationContext());
                Pump(window.InitializeAsync());
                Console.WriteLine("READER_STATUS "+((Label)Field(window,"loading")).Text);
                var web=(WebView2)Field(window,"web");
                Check(web.CoreWebView2!=null,"reader browser initialized");
                DateTime until=DateTime.UtcNow.AddSeconds(15);
                while(DateTime.UtcNow<until)
                {
                    Application.DoEvents();Thread.Sleep(30);
                    if(Script(web,"!!document.querySelector('#document h1')")=="true")break;
                }
                Console.WriteLine("READER_DOM "+Script(web,"JSON.stringify({text:document.body.innerText.slice(0,300),html:document.documentElement.outerHTML.slice(0,200),ready:typeof window.YingXuPreview,url:location.href})"));
                if(File.Exists(Path.Combine(Hub.Data,"desktop.log")))Console.WriteLine(File.ReadAllText(Path.Combine(Hub.Data,"desktop.log")));
                Console.WriteLine("READER_AFTER_NAV "+((Label)Field(window,"loading")).Text);
                Check(new System.Web.Script.Serialization.JavaScriptSerializer().Deserialize<string>(Script(web,"document.querySelector('#document h1').textContent"))=="中文标题","real WebView renders Markdown heading");
                Check(new System.Web.Script.Serialization.JavaScriptSerializer().Deserialize<string>(Script(web,"document.querySelector('#document strong').textContent"))=="加粗","real WebView renders Markdown emphasis");
                Check(Script(web,"!!window.injected || !!document.querySelector('#document img')")=="false","HTML and remote images cannot execute or load");
                Check(Script(web,"document.querySelector('#document').isContentEditable")=="false","quick reader is readonly");
                Script(web,"document.querySelector('#mode').click()");
                Check(Script(web,"document.querySelector('#document').textContent.includes('<script>window.injected=true</script>')")=="true","source mode retains literal text");
                var plus=window.Controls.OfType<ToolStrip>().Single().Items.OfType<ToolStripButton>().Single(item=>item.ToolTipText=="放大文字");
                for(int i=0;i<5;i++)plus.PerformClick();
                until=DateTime.UtcNow.AddSeconds(3);
                while(((ToolStripButton)Field(window,"zoom")).Text!="150%" && DateTime.UtcNow<until) { Application.DoEvents();Thread.Sleep(20); }
                Console.WriteLine("ZOOM "+web.ZoomFactor+" "+((ToolStripButton)Field(window,"zoom")).Text);
                Check(Math.Abs(web.ZoomFactor-1.5)<0.01 && ((ToolStripButton)Field(window,"zoom")).Text=="150%","zoom changes text scale and indicator");
                Check(window.MaximizeBox && window.FormBorderStyle==FormBorderStyle.Sizable,"reader has native resize and maximize controls");
                var button=window.StudioButton;
                Check(button.Parent is Panel && button.Right < button.Parent.Width,"workspace button is in the caption left of window controls");
                button.PerformClick();
                Check(handed!=null && handed.Length==1 && handed[0]==file,"workspace handoff keeps original file path");
                Check(!File.Exists(Path.Combine(Hub.Data,"server.pid.json")) && !Directory.Exists(Path.Combine(temporary,"projects")),"reader never starts project backend or scans projects");
                Check(saved.SequenceEqual(File.ReadAllBytes(file)),"native reader leaves BOM and source bytes intact");
                var browserExited=new TaskCompletionSource<bool>();
                web.CoreWebView2.Environment.BrowserProcessExited+=(sender,eventArgs)=>browserExited.TrySetResult(true);
                window.Close();Application.DoEvents();
                Pump(browserExited.Task);
                Check(window.IsDisposed,"close destroys reader instead of hiding in tray");
            }
            // Routing is exercised inside one real message loop, with no workspace.
            Program.InitialFiles=new[]{file};
            using(var context=new DesktopContext(() => new StudioWindow(initialize:false,routed:true)))
            {
                var timer=new System.Windows.Forms.Timer { Interval=100 };
                timer.Tick += delegate
                {
                    timer.Stop();
                    var first=Application.OpenForms.OfType<QuickReaderWindow>().Single();
                    context.Open(new[]{file});
                    Check(Application.OpenForms.OfType<QuickReaderWindow>().Count()==1,"reopening the same file reuses its reader window");
                    Check(!Application.OpenForms.OfType<StudioWindow>().Any(),"file launch does not show the workspace");
                    context.OpenStudio(new[]{file});
                    var studio=Application.OpenForms.OfType<StudioWindow>().Single();
                    context.OpenStudio(new[]{file});
                    Check(Application.OpenForms.OfType<StudioWindow>().Count()==1 && Application.OpenForms.OfType<StudioWindow>().Single()==studio,"handoff reuses the same workspace instance");
                    Check(((System.Collections.Generic.Queue<string[]>)Field(studio,"pendingFiles")).Count==2,"workspace queues both handoffs until its editor is ready");
                    context.Open(new[]{Path.Combine(temporary,"synthetic.pdf")});
                    Check(((System.Collections.Generic.HashSet<string[]>)Field(studio,"workspaceRequests")).Count==2,"non-text shell opens retain the existing document-only preference");
                    studio.GetType().GetField("exitApproved",BindingFlags.NonPublic|BindingFlags.Instance).SetValue(studio,true);
                    studio.Close();
                    Check(!first.IsDisposed,"closing workspace leaves quick readers available");
                    first.Close();
                };
                timer.Start();Application.Run(context);timer.Dispose();
                Check(!Application.OpenForms.OfType<QuickReaderWindow>().Any(),"last reader closes application context");
            }
        }
    }
}
