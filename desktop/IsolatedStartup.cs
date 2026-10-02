using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Win32.SafeHandles;

namespace YingXu.Desktop
{
    // Explicit development-only entry. Never discovers installations or starts a backend.
    internal sealed class IsolatedStartup
    {
        internal string Root, TestRoot, Data, Projects, Browser, Evidence, TestId;
        internal int Port, Timeout = 90;
        internal bool PreflightOnly;
        internal string Url { get { return "http://127.0.0.1:" + Port + "/"; } }
        internal string Namespace { get { return "YingXu.Isolated." + TestId; } }
        internal bool AllowRequest(string address,string method,int indexSyncPosts)
        {
            Uri uri;
            if(!Uri.TryCreate(address,UriKind.Absolute,out uri) || uri.Scheme!="http" || uri.Host!="127.0.0.1" || uri.Port!=Port || !String.IsNullOrEmpty(uri.UserInfo))return false;
            if(method=="POST")return indexSyncPosts==0 && uri.AbsolutePath=="/api/project-files/sync" && String.IsNullOrEmpty(uri.Query);
            return method=="GET" && !uri.AbsolutePath.StartsWith("/api/updates",StringComparison.Ordinal) &&
                !uri.AbsolutePath.StartsWith("/api/open",StringComparison.Ordinal) && !uri.AbsolutePath.StartsWith("/api/external",StringComparison.Ordinal);
        }
        internal static bool Requested(string[] args) { return Array.IndexOf(args, "--isolated-test") >= 0; }
        internal static bool Within(string child, string parent)
        {
            return child.StartsWith(parent.TrimEnd('\\') + "\\", StringComparison.OrdinalIgnoreCase);
        }
        internal static string DirectoryPath(string value)
        {
            if (String.IsNullOrEmpty(value) || !System.Text.RegularExpressions.Regex.IsMatch(value,@"\A[A-Za-z]:[\\/]") || value.StartsWith(@"\\") ||
                value.IndexOf(':', 2) >= 0 || value.Length < 4) throw new InvalidDataException("path_invalid");
            string full = Path.GetFullPath(value).TrimEnd('\\');
            if (!Directory.Exists(full) || full == Path.GetPathRoot(full).TrimEnd('\\')) throw new InvalidDataException("path_missing");
            for (string current = full; current != null; current = Path.GetDirectoryName(current))
                if ((File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0) throw new InvalidDataException("path_link");
            return full;
        }
        internal static bool Overlaps(string a, string b) { return String.Equals(a,b,StringComparison.OrdinalIgnoreCase) || Within(a,b) || Within(b,a); }
        [StructLayout(LayoutKind.Sequential)]
        private struct FileIdentity
        {
            internal uint Attributes;
            internal System.Runtime.InteropServices.ComTypes.FILETIME Creation, Access, Write;
            internal uint Volume, SizeHigh, SizeLow, Links, IndexHigh, IndexLow;
        }
        [DllImport("kernel32.dll",SetLastError=true)]
        private static extern bool GetFileInformationByHandle(SafeFileHandle handle,out FileIdentity identity);
        internal static void OrdinaryFile(FileStream stream)
        {
            FileIdentity identity;
            if(!GetFileInformationByHandle(stream.SafeFileHandle,out identity) || identity.Links!=1 || (identity.Attributes & (uint)FileAttributes.ReparsePoint)!=0)throw new InvalidDataException("control_file_link");
        }
        internal void CheckOwnership()
        {
            string marker=Path.Combine(TestRoot,".yingxu-isolated-owner.json");
            if(!File.Exists(marker) || (File.GetAttributes(marker)&FileAttributes.ReparsePoint)!=0)throw new InvalidDataException("ownership_missing");
            Dictionary<string,object> value;
            using(var stream=new FileStream(marker,FileMode.Open,FileAccess.Read,FileShare.Read))
            {
                OrdinaryFile(stream);if(stream.Length>2048)throw new InvalidDataException("ownership_invalid");
                using(var reader=new StreamReader(stream,new UTF8Encoding(false,true)))value=new JavaScriptSerializer().Deserialize<Dictionary<string,object>>(reader.ReadToEnd());
            }
            object schema,id,program,data,projects;
            if(value==null || value.Count!=5 || !value.TryGetValue("schema",out schema) || (schema as string)!="yingxu-native-isolated/1" ||
                !value.TryGetValue("test_id",out id) || (id as string)!=TestId || !value.TryGetValue("program_root",out program) || (program as string)!=Root ||
                !value.TryGetValue("data_root",out data) || (data as string)!=Data || !value.TryGetValue("projects_root",out projects) || (projects as string)!=Projects)throw new InvalidDataException("ownership_mismatch");
        }
        internal static IsolatedStartup Parse(string[] args)
        {
            var values = new Dictionary<string,string>(StringComparer.Ordinal);
            var result = new IsolatedStartup();
            bool marker = false;
            foreach (string argument in args) if (argument == "--isolated-test") { if(marker)throw new InvalidDataException("duplicate_option"); marker=true; }
            if (!marker) throw new InvalidDataException("test_marker_missing");
            for(int i=0;i<args.Length;i++)
            {
                string name=args[i];
                if(name=="--isolated-test")continue;
                if(name=="--preflight-only") { if(result.PreflightOnly)throw new InvalidDataException("duplicate_option"); result.PreflightOnly=true;continue; }
                if(Array.IndexOf(new[]{"--root","--test-root","--data","--projects","--port","--test-id","--webview-root","--evidence-root","--timeout-seconds"},name)<0 ||
                    values.ContainsKey(name) || ++i==args.Length || args[i].StartsWith("--",StringComparison.Ordinal)) throw new InvalidDataException("option_invalid");
                values.Add(name,args[i]);
            }
            foreach(string name in new[]{"--root","--test-root","--data","--projects","--port","--test-id","--webview-root","--evidence-root"})
                if(!values.ContainsKey(name))throw new InvalidDataException("option_missing");
            Guid identity;
            if(!Guid.TryParseExact(values["--test-id"],"D",out identity) || identity==Guid.Empty || identity.ToString("D")!=values["--test-id"])throw new InvalidDataException("test_id_invalid");
            result.TestId=values["--test-id"];
            if(!Int32.TryParse(values["--port"],out result.Port) || result.Port<1024 || result.Port>65535 || result.Port==8791)throw new InvalidDataException("port_invalid");
            if(values.ContainsKey("--timeout-seconds") && (!Int32.TryParse(values["--timeout-seconds"],out result.Timeout) || result.Timeout<20 || result.Timeout>180))throw new InvalidDataException("timeout_invalid");
            result.Root=DirectoryPath(values["--root"]);result.TestRoot=DirectoryPath(values["--test-root"]);
            result.Data=DirectoryPath(values["--data"]);result.Projects=DirectoryPath(values["--projects"]);
            result.Browser=DirectoryPath(values["--webview-root"]);result.Evidence=DirectoryPath(values["--evidence-root"]);
            if(!Within(result.Data,result.TestRoot) || !Within(result.Projects,result.TestRoot) || !Within(result.Evidence,result.TestRoot) ||
                Overlaps(result.Data,result.Projects) || Overlaps(result.Data,result.Evidence) || Overlaps(result.Projects,result.Evidence) ||
                Overlaps(result.Root,result.TestRoot) || Overlaps(result.Browser,result.TestRoot) || Overlaps(result.Browser,result.Root))throw new InvalidDataException("path_overlap");
            string defaultData=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"YingXu");
            string defaultProjects=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments),"YingXu","Projects");
            foreach(string path in new[]{result.Root,result.TestRoot,result.Data,result.Projects,result.Evidence})
                if(Overlaps(path,defaultData) || Overlaps(path,defaultProjects))throw new InvalidDataException("protected_path");
            if(!File.Exists(Path.Combine(result.Root,"desktop","IsolatedStartup.cs")) || !File.Exists(Path.Combine(result.Root,"server.py")) ||
                !File.Exists(Path.Combine(result.Root,"launcher.pyw")) || !Directory.Exists(Path.Combine(result.Root,"frontend")))throw new InvalidDataException("candidate_root_invalid");
            if(!File.Exists(Path.Combine(result.Browser,"msedgewebview2.exe")))throw new InvalidDataException("browser_missing");
            if((File.GetAttributes(Path.Combine(result.Browser,"msedgewebview2.exe")) & FileAttributes.ReparsePoint)!=0)throw new InvalidDataException("browser_link");
            foreach(string name in new[]{"ready.json","preview.png","failure.json","exit.request","preflight.json"})
                if(File.Exists(Path.Combine(result.Evidence,name)) || Directory.Exists(Path.Combine(result.Evidence,name)))throw new InvalidDataException("evidence_exists");
            result.CheckOwnership();
            return result;
        }
        internal Dictionary<string,object> Read(string path)
        {
            var request=(HttpWebRequest)WebRequest.Create(Url.TrimEnd('/')+path);request.Proxy=null;request.AllowAutoRedirect=false;request.Timeout=3000;request.ReadWriteTimeout=3000;
            using(var response=request.GetResponse())using(var stream=response.GetResponseStream())using(var reader=new StreamReader(stream,Encoding.UTF8))
            {var buffer=new char[65537];int count=reader.ReadBlock(buffer,0,buffer.Length);if(count==buffer.Length)throw new InvalidDataException("response_large");
                var value=new JavaScriptSerializer().Deserialize<Dictionary<string,object>>(new string(buffer,0,count));if(value==null)throw new InvalidDataException("response_invalid");return value;}
        }
        internal void CheckIdentity(Dictionary<string,object> value)
        {
            object app,ok,version,build,program,data;
            if(!value.TryGetValue("app",out app) || (app as string)!="yingxu" || !value.TryGetValue("ok",out ok) || !(ok is bool) || !(bool)ok ||
                !value.TryGetValue("version",out version) || (version as string)!=BuildIdentity.Version ||
                !value.TryGetValue("build_revision",out build) || (build as string)!=BuildIdentity.BuildRevision ||
                !value.TryGetValue("program_id",out program) || (program as string)!=Hub.PathIdentity(Hub.NormalizeRoot(Root)) ||
                !value.TryGetValue("instance_id",out data) || (data as string)!=Hub.PathIdentity(Hub.NormalizeRoot(Data)))throw new InvalidDataException("health_identity_mismatch");
        }
        internal void CheckSettings(Dictionary<string,object> settings)
        {
            object nested; if(settings.TryGetValue("settings",out nested)){settings=nested as Dictionary<string,object>;if(settings==null)throw new InvalidDataException("settings_invalid");}
            foreach(string key in new[]{"capture_enabled","automatic_update_check","automatic_update_download"})
            {object value;if(!settings.TryGetValue(key,out value) || !(value is bool) || (bool)value)throw new InvalidDataException("settings_not_isolated");}
        }
        internal void Preflight() { DirectoryPath(Root);DirectoryPath(Data);DirectoryPath(Projects);DirectoryPath(Browser);DirectoryPath(Evidence);CheckOwnership();CheckIdentity(Read("/api/health"));CheckSettings(Read("/api/settings")); }
        internal object Summary(string state) {return new {schema="yingxu-native-isolated/1",state=state,test_id=TestId,version=BuildIdentity.Version,build_revision=BuildIdentity.BuildRevision,program_id=Hub.PathIdentity(Hub.NormalizeRoot(Root)),instance_id=Hub.PathIdentity(Hub.NormalizeRoot(Data)),root=Root,data=Data,projects=Projects,port=Port,browser_root=Browser,isolated=true,backend_started=false,tray=false,global_hotkey=false};}
        internal void WriteNew(string name,object value)
        { DirectoryPath(Evidence);using(var stream=new FileStream(Path.Combine(Evidence,name),FileMode.CreateNew,FileAccess.Write,FileShare.None))using(var writer=new StreamWriter(stream,new UTF8Encoding(false)))writer.Write(new JavaScriptSerializer().Serialize(value)); }
        internal static int Run(string[] args)
        {
            IsolatedStartup options=null;
            try
            {
                options=Parse(args);
                Environment.SetEnvironmentVariable("YINGXU_DATA_DIR",options.Data);Environment.SetEnvironmentVariable("YINGXU_PROJECTS_DIR",options.Projects);
                Hub.Root=options.Root;Hub.Data=options.Data;Hub.Port=options.Port;Hub.Url=options.Url;Hub.Cache=Path.Combine(options.Data,"desktop-isolated");
                options.Preflight();
                if(options.PreflightOnly){options.WriteNew("preflight.json",options.Summary("preflight_passed"));Console.WriteLine(new JavaScriptSerializer().Serialize(options.Summary("preflight_passed")));return 0;}
                using(var mutex=new Mutex(false,@"Local\"+options.Namespace))using(var activation=new EventWaitHandle(false,EventResetMode.AutoReset,@"Local\"+options.Namespace+".event"))
                {
                    bool acquired=false;try{acquired=mutex.WaitOne(0);}catch(AbandonedMutexException){acquired=true;}
                    if(!acquired)throw new InvalidDataException("test_identity_busy");
                    try
                    {
                        Program.Isolated=options;Program.InitialFiles=new string[0];Program.InstanceKey=options.Namespace;Program.ActivateEvent=activation;
                        Directory.CreateDirectory(Hub.Cache);Program.PrepareIsolatedLibraries();Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);
                        Program.SetIsolatedAppId(options.Namespace);
                        return Program.RunIsolatedWindow();
                    }finally{mutex.ReleaseMutex();}
                }
            }
            catch(Exception error)
            {
                string code=error is InvalidDataException ? error.Message : "isolated_startup_failed";
                if(!System.Text.RegularExpressions.Regex.IsMatch(code,"\\A[a-z_]+\\z"))code="isolated_startup_failed";
                var failure=new {schema="yingxu-native-isolated/1",state="rejected",code=code};Console.WriteLine(new JavaScriptSerializer().Serialize(failure));
                if(options!=null)try{options.WriteNew("failure.json",failure);}catch{}
                return 2;
            }
        }
    }
}
