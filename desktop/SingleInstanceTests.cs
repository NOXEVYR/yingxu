using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Threading;

namespace YingXu.Desktop
{
    // Launch the real desktop entry point, but own its gate in this isolated
    // fixture. Secondary launches must return before touching WebView or a server.
    internal static class SingleInstanceTests
    {
        private static void Check(bool value, string name)
        {
            if (!value) throw new Exception("FAILED: " + name);
            Console.WriteLine("PASS " + name);
        }

        private static int Main(string[] args)
        {
            string temporary = Path.GetFullPath(Path.Combine(Path.GetTempPath(), "yingxu-instance-" + Guid.NewGuid().ToString("N")));
            Directory.CreateDirectory(temporary);
            var children = new List<Process>();
            try
            {
                Hub.Data = Path.Combine(temporary, "data");
                Directory.CreateDirectory(Hub.Data);
                string id = Hub.InstanceId().Substring(0,24);
                string[] roots = { Path.Combine(temporary,"installed"), Path.Combine(temporary,"another-version") };
                foreach (string root in roots)
                {
                    Directory.CreateDirectory(Path.Combine(root,"frontend"));
                    foreach (string file in new[] { "server.py", "launcher.pyw", "frontend/index.html" })
                        File.WriteAllText(Path.Combine(root,file), "synthetic fixture");
                }
                Hub.Root = roots[0];
                Check(id == Hub.InstanceId().Substring(0,24), "first installation uses catalogue identity");
                Hub.Root = roots[1];
                Check(id == Hub.InstanceId().Substring(0,24), "another installation shares catalogue identity");
                string data = Hub.Data;
                Hub.Data = Path.Combine(temporary,"independent-data");
                Check(id != Hub.InstanceId().Substring(0,24), "independent catalogues have separate identities");
                Hub.Data = data;
                var received = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                using (var gate = new Mutex(false, @"Local\YingXu-desktop-" + id))
                using (var activation = new EventWaitHandle(false, EventResetMode.AutoReset, @"Local\YingXu-desktop-show-" + id))
                using (var inbox = new OpenInbox(id, files => { lock(received) { foreach(string file in files) received.Add(file); } }))
                {
                    Check(gate.WaitOne(0), "fixture owns the single desktop gate");
                    try
                    {
                        for(int index=0;index<8;index++)
                        {
                            string file = Path.Combine(temporary,"笔记 " + index + ".md");
                            File.WriteAllText(file,"# synthetic");
                            var start = new ProcessStartInfo(args[0], "--root " + Hub.Quote(roots[index%2]) +
                                (index<2 ? "" : " --open " + Hub.Quote(file)));
                            start.UseShellExecute=false; start.CreateNoWindow=true; start.WindowStyle=ProcessWindowStyle.Hidden;
                            start.EnvironmentVariables["YINGXU_DATA_DIR"] = data;
                            start.EnvironmentVariables["YINGXU_PROJECTS_DIR"] = Path.Combine(temporary,"projects");
                            children.Add(Process.Start(start));
                        }
                        foreach(var child in children)
                            Check(child.WaitForExit(15000) && child.ExitCode==0, "repeated desktop launch exits after handoff");
                        Check(activation.WaitOne(2000), "repeated launch wakes the existing window");
                        lock(received)
                        {
                            Check(received.Count==6, "all six concurrent file requests reach the existing inbox");
                            for(int index=2;index<8;index++)
                                Check(received.Contains(Path.Combine(temporary,"笔记 " + index + ".md")), "Unicode file request preserved " + index);
                        }
                        Check(!Directory.Exists(Path.Combine(data,"desktop","loader")), "secondary launches never initialize WebView");
                        Check(!File.Exists(Path.Combine(data,"server.pid.json")), "secondary launches never start another backend");
                    }
                    finally { gate.ReleaseMutex(); }
                }
                using(var gate = new Mutex(false, @"Local\YingXu-desktop-" + id))
                {
                    Check(gate.WaitOne(0), "desktop gate is reusable after the owner exits");
                    gate.ReleaseMutex();
                }
                return 0;
            }
            finally
            {
                foreach(var child in children)
                {
                    // Only fixture-owned children, never a user's existing app.
                    if(!child.HasExited) { child.Kill(); child.WaitForExit(5000); }
                    child.Dispose();
                }
                if(Path.GetDirectoryName(temporary).TrimEnd('\\') != Path.GetFullPath(Path.GetTempPath()).TrimEnd('\\') ||
                    !Path.GetFileName(temporary).StartsWith("yingxu-instance-") ||
                    (File.GetAttributes(temporary)&FileAttributes.ReparsePoint)!=0) throw new IOException("Invalid fixture directory");
                Directory.Delete(temporary,true);
            }
        }
    }
}
