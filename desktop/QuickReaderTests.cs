using System;
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
