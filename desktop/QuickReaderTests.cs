using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
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
        private static string Script(WebView2 web,string code)
        {
            var task=web.CoreWebView2.ExecuteScriptAsync(code);Pump(task);return task.Result;
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
                    var button=window.Controls.OfType<ToolStrip>().Single().Items.OfType<ToolStripButton>().Single(item=>item.Text.StartsWith("在映序"));
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
            string temporary=Path.GetFullPath(Path.Combine(Path.GetTempPath(),"yingxu-lifecycle-reader-"+Guid.NewGuid().ToString("N")));
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
                Run(temporary);
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
        private static void Run(string temporary)
        {
            CoreWebView2Environment.SetLoaderDllFolderPath(Program.LoaderFolder);
            JunctionPaths(temporary);
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
            foreach(string ext in new[]{".docx",".pdf",".html",".svg",".png",".excalidraw"})Check(!QuickReaderDocument.Supports("note"+ext),"workspace routing "+ext);
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
                var button=window.Controls.OfType<ToolStrip>().Single().Items.OfType<ToolStripButton>().Single(item=>item.Text.StartsWith("在映序"));
                Check(button.Alignment==ToolStripItemAlignment.Right,"workspace button sits at the upper right");
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
