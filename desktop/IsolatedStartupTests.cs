using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Web.Script.Serialization;

namespace YingXu.Desktop
{
    internal static class IsolatedStartupTests
    {
        private static int passed;
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
        private static extern bool CreateHardLinkW(string newName,string existingName,IntPtr security);
        private static void Check(bool condition,string name){if(!condition)throw new Exception(name);passed++;Console.WriteLine("PASS "+name);}
        private static void Reject(Action action,string name){try{action();}catch(InvalidDataException){Check(true,name);return;}throw new Exception("accepted "+name);}
        private static void RejectCode(Action action,string code,string name){try{action();}catch(InvalidDataException error){Check(error.Message==code,name);return;}throw new Exception("accepted "+name);}
        private static string[] Replace(string[] args,string key,string value){var copy=(string[])args.Clone();copy[Array.IndexOf(copy,key)+1]=value;return copy;}
        [STAThread]
        private static int Main()
        {
            string temporary=Path.Combine(Path.GetTempPath(),"yingxu-isolated-unit-"+Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(temporary);
            try
            {
                string source=Path.Combine(temporary,"source"),run=Path.Combine(temporary,"private"),runtime=Path.Combine(temporary,"browser");
                foreach(string path in new[]{source,run,runtime,Path.Combine(source,"desktop"),Path.Combine(source,"frontend"),Path.Combine(run,"data"),Path.Combine(run,"projects"),Path.Combine(run,"evidence")})Directory.CreateDirectory(path);
                foreach(string path in new[]{Path.Combine(source,"server.py"),Path.Combine(source,"launcher.pyw"),Path.Combine(source,"desktop","IsolatedStartup.cs"),Path.Combine(runtime,"msedgewebview2.exe")})File.WriteAllText(path,"synthetic fixture; not executed");
                Environment.SetEnvironmentVariable("YINGXU_DATA_DIR",Path.Combine(run,"data"));
                var args=new[]{"--isolated-test","--root",source,"--test-root",run,"--data",Path.Combine(run,"data"),"--projects",Path.Combine(run,"projects"),"--evidence-root",Path.Combine(run,"evidence"),"--webview-root",runtime,"--port","23456","--test-id","8e71b706-61e1-4aa4-8e3e-46f12332bd82","--preflight-only"};
                var owner=new{schema="yingxu-native-isolated/1",test_id="8e71b706-61e1-4aa4-8e3e-46f12332bd82",program_root=source,data_root=Path.Combine(run,"data"),projects_root=Path.Combine(run,"projects")};
                string marker=Path.Combine(run,".yingxu-isolated-owner.json");
                Reject(()=>IsolatedStartup.Parse(args),"ownership marker required");
                File.WriteAllText(marker,new JavaScriptSerializer().Serialize(owner));
                var options=IsolatedStartup.Parse(args);
                Check(options.PreflightOnly && options.Port==23456 && options.Root==source,"explicit valid options");
                Check(!IsolatedStartup.Requested(new[]{"--root",source}),"normal invocation not redirected");
                Check(LaunchOptions.Parse(new[]{"--root",source},source).Root==source,"normal root parser preserved");
                foreach(string missing in new[]{"--root","--test-root","--data","--projects","--evidence-root","--webview-root","--port","--test-id"}){
                    var values=new List<string>(args);int at=values.IndexOf(missing);values.RemoveRange(at,2);Reject(()=>IsolatedStartup.Parse(values.ToArray()),"missing "+missing);
                }
                Reject(()=>IsolatedStartup.Parse(Replace(args,"--port","8791")),"formal port rejected");
                Reject(()=>IsolatedStartup.Parse(Replace(args,"--test-id","00000000-0000-0000-0000-000000000000")),"empty identity rejected");
                Reject(()=>IsolatedStartup.Parse(Replace(args,"--data","relative")),"relative data rejected");
                RejectCode(()=>IsolatedStartup.DirectoryPath(source.Substring(2)),"path_invalid","existing root-relative directory rejected before normalization");
                string previousDirectory=Environment.CurrentDirectory;
                try{
                    Environment.CurrentDirectory=temporary;
                    RejectCode(()=>IsolatedStartup.DirectoryPath(Path.GetPathRoot(source).TrimEnd('\\')+"source"),"path_invalid","existing drive-relative directory rejected before normalization");
                }finally{Environment.CurrentDirectory=previousDirectory;}
                Reject(()=>IsolatedStartup.Parse(Replace(args,"--projects",options.Data)),"overlapping private paths rejected");
                Reject(()=>IsolatedStartup.Parse(Replace(args,"--data",source)),"source used as data rejected");
                var unknown=new List<string>(args);unknown.Add("--register-open-with");Reject(()=>IsolatedStartup.Parse(unknown.ToArray()),"registration prohibited");
                File.Delete(Path.Combine(runtime,"msedgewebview2.exe"));Reject(()=>IsolatedStartup.Parse(args),"missing explicit runtime no fallback");File.WriteAllText(Path.Combine(runtime,"msedgewebview2.exe"),"fixture");
                var health=new Dictionary<string,object>{{"app","yingxu"},{"ok",true},{"version",BuildIdentity.Version},{"build_revision",BuildIdentity.BuildRevision},{"program_id",Hub.PathIdentity(Hub.NormalizeRoot(source))},{"instance_id",Hub.PathIdentity(Hub.NormalizeRoot(options.Data))}};
                options.CheckIdentity(health);Check(true,"matching four dimensional identity");
                foreach(string key in new[]{"version","build_revision","program_id","instance_id"}){
                    var changed=new Dictionary<string,object>(health);changed[key]="incorrect";Reject(()=>options.CheckIdentity(changed),"wrong "+key);
                    changed.Remove(key);Reject(()=>options.CheckIdentity(changed),"missing "+key);
                }
                var settings=new Dictionary<string,object>{{"capture_enabled",false},{"automatic_update_check",false},{"automatic_update_download",false}};
                options.CheckSettings(settings);Check(true,"explicit disabled side effects");
                foreach(string key in new[]{"capture_enabled","automatic_update_check","automatic_update_download"}){
                    var unsafeSettings=new Dictionary<string,object>(settings);unsafeSettings[key]=true;Reject(()=>options.CheckSettings(unsafeSettings),"enabled "+key);
                    unsafeSettings[key]="false";Reject(()=>options.CheckSettings(unsafeSettings),"nonboolean "+key);
                }
                string link=Path.Combine(run,"owner-linked.json");
                Check(CreateHardLinkW(link,marker,IntPtr.Zero),"private fixture hardlink created");
                Reject(()=>IsolatedStartup.Parse(args),"ownership hardlink rejected");File.Delete(link);
                File.WriteAllText(marker,"{}");Reject(()=>IsolatedStartup.Parse(args),"wrong ownership rejected");
                File.WriteAllText(marker,new JavaScriptSerializer().Serialize(owner));
                Check(options.AllowRequest(options.Url+"api/bootstrap","GET",0),"only original local GET allowed");
                Check(!options.AllowRequest("http://127.0.0.1:8791/api/bootstrap","GET",0),"formal port resources blocked");
                Check(!options.AllowRequest("https://example.invalid/","GET",0),"external resources blocked");
                Check(!options.AllowRequest(options.Url+"api/open","GET",0),"shell routes blocked");
                Check(!options.AllowRequest(options.Url+"api/updates","GET",0),"update routes blocked");
                Check(!options.AllowRequest(options.Url+"api/ai-tasks","POST",0),"task writes blocked");
                Check(options.AllowRequest(options.Url+"api/project-files/sync","POST",0),"one private index sync allowed");
                Check(!options.AllowRequest(options.Url+"api/project-files/sync","POST",1),"second index sync blocked");
                Check(!options.AllowRequest(options.Url+"api/project-files/sync?other=1","POST",0),"index sync query variation blocked");
                File.WriteAllText(Path.Combine(options.Evidence,"ready.json"),"old");Reject(()=>IsolatedStartup.Parse(args),"existing evidence rejects identity reuse");
                Console.WriteLine("RESULT "+passed+" checks passed; no GUI/backend/runtime executed");return 0;
            }
            catch(Exception error){Console.WriteLine("FAIL "+error.GetType().Name+" "+error.Message);return 1;}
            finally{
                string owned=Path.GetFullPath(temporary),parent=Path.GetFullPath(Path.GetTempPath()).TrimEnd(Path.DirectorySeparatorChar);
                if(Path.GetDirectoryName(owned)!=parent || !Path.GetFileName(owned).StartsWith("yingxu-isolated-unit-",StringComparison.Ordinal) || (File.GetAttributes(owned)&FileAttributes.ReparsePoint)!=0)throw new InvalidDataException("fixture_cleanup_invalid");
                Directory.Delete(owned,true);
            }
        }
    }
}
