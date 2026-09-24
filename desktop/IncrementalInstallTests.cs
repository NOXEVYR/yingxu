using System;
using System.Diagnostics;
using System.IO;
using System.Collections.Generic;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading.Tasks;
using System.Reflection;
using System.Runtime.Serialization;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;

namespace YingXu.Desktop
{
    internal static class IncrementalInstallTests
    {
        private static int checks;
        private static void Check(bool value,string name){if(!value)throw new Exception(name);checks++;Console.WriteLine("PASS "+name);}
        private static object Field(object value,string name){return value.GetType().GetField(name,BindingFlags.NonPublic|BindingFlags.Instance).GetValue(value);}
        private static void Field(object value,string name,object setting){value.GetType().GetField(name,BindingFlags.NonPublic|BindingFlags.Instance).SetValue(value,setting);}
        private static bool Receive(StudioWindow window,string source,object value){return (bool)window.GetType().GetMethod("ReceiveDesktopRequest",BindingFlags.NonPublic|BindingFlags.Instance).Invoke(window,new object[]{source,new JavaScriptSerializer().Serialize(value)});}
        private static void StatusProtocol()
        {
            string previous=Hub.Url;var listener=new TcpListener(IPAddress.Loopback,0);listener.Start();
            Hub.Url="http://127.0.0.1:"+((IPEndPoint)listener.LocalEndpoint).Port+"/";
            var server=Task.Run(()=>{
                using(var client=listener.AcceptTcpClient())using(var stream=client.GetStream())
                {
                    client.ReceiveTimeout=5000;var header=new List<byte>();int value;
                    while((value=stream.ReadByte())>=0){header.Add((byte)value);int n=header.Count;if(n>=4&&header[n-4]==13&&header[n-3]==10&&header[n-2]==13&&header[n-1]==10)break;if(n>16384)throw new IOException("fixture header too long");}
                    string text=Encoding.ASCII.GetString(header.ToArray());
                    Check(text.StartsWith("GET /api/updates/install/status HTTP/"),"lost prepare status uses GET");
                    Check(text.Contains("Origin: "+Hub.Url.TrimEnd('/'))&&text.Contains("X-YingXu-Token: fixture-token"),"status GET carries Origin and token");
                    byte[] body=Encoding.UTF8.GetBytes("{\"prepared\":false,\"committed\":false}"),head=Encoding.ASCII.GetBytes("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "+body.Length+"\r\nConnection: close\r\n\r\n");stream.Write(head,0,head.Length);stream.Write(body,0,body.Length);
                }
            });
            try{var result=DesktopApi.Request("/api/updates/install/status",null,"fixture-token");server.GetAwaiter().GetResult();Check(result.ContainsKey("prepared")&&!(bool)result["prepared"],"status response parsed");}
            finally{listener.Stop();Hub.Url=previous;}
        }
        [STAThread]
        internal static int Main(string[] args)
        {
            string temporary=Path.Combine(Path.GetTempPath(),"yingxu-incremental-native-"+Guid.NewGuid().ToString("N"));Directory.CreateDirectory(temporary);
            try
            {
                Hub.Root=args[0];Hub.Data=temporary;Hub.Cache=Path.Combine(temporary,"desktop");Directory.CreateDirectory(Hub.Cache);Hub.Url="http://127.0.0.1:28791/";
                string id=new string('a',32),ticket=new string('b',32);string job=IncrementalUpdateBridge.Folder(ticket);Directory.CreateDirectory(job);
                Check(IncrementalUpdateBridge.ValidId(id),"valid plan ID");Check(!IncrementalUpdateBridge.ValidId("../bad"),"traversal rejected");
                Check(!IncrementalUpdateBridge.ValidId(id+"\n"),"newline rejected");
                File.WriteAllText(Path.Combine(job,"commit.json"),new JavaScriptSerializer().Serialize(new {committed=true,ticket=ticket,native_pid=Process.GetCurrentProcess().Id,backend_pid=321}));
                Check(IncrementalUpdateBridge.Receipt(ticket,321),"durable receipt resolves lost response");
                Check(!IncrementalUpdateBridge.Receipt(ticket,322),"another backend receipt rejected");
                File.WriteAllText(Path.Combine(job,"commit.json"),new JavaScriptSerializer().Serialize(new {committed=true,ticket=ticket,native_pid=-1,backend_pid=321}));
                Check(!IncrementalUpdateBridge.Receipt(ticket,321),"another native receipt rejected");
                string large=Path.Combine(job,"oversize.json");File.WriteAllText(large,new string('a',16385));
                bool refused=false;try{IncrementalUpdateBridge.Read(large);}catch(InvalidDataException){refused=true;}Check(refused,"bounded receipts");
                StatusProtocol();
                using(var window=new StudioWindow(false,null,true))
                {
                    window.Show();Field(window,"pageReady",true);
                    Check(!Receive(window,"https://example.com",new {action="install-update",planId=id}),"foreign install message rejected");
                    Check(!Receive(window,Hub.Url,new {action="install-update",planId="../bad"}),"invalid install ID rejected");
                    Check(Receive(window,Hub.Url,new {action="install-update",planId=id}),"install begins draft confirmation");
                    string request=Field(window,"exitRequest") as string;
                    Check(request!=null&&!Program.IncrementalInstalling&&!(bool)Field(window,"exitApproved"),"no helper/close before draft allow");
                    Receive(window,Hub.Url,new {action="exit-response",requestId=request,allow=false});
                    Check(Field(window,"pendingInstallPlan")==null&&!Program.IncrementalInstalling,"draft cancel cancels update");
                    using(var other=new Form())
                    {
                        other.Show();Receive(window,Hub.Url,new {action="install-update",planId=id});
                        Check(Field(window,"exitRequest")==null,"other reader window prevents installation");other.Close();
                    }
                    Receive(window,Hub.Url,new {action="install-update",planId=id});request=Field(window,"exitRequest") as string;
                    Receive(window,Hub.Url,new {action="exit-response",requestId="stale",allow=true});
                    Check(!Program.IncrementalInstalling,"stale draft response cannot prepare");
                    Receive(window,Hub.Url,new {action="exit-response",requestId=request,allow=true});
                    Check(Program.IncrementalInstalling&&!(bool)Field(window,"exitApproved"),"allow freezes editing but keeps window until commit");
                    Field(window,"pageReady",false);Receive(window,Hub.Url,new {action="desktop-ready"});
                    Check(!(bool)Field(window,"pageReady"),"late ready cannot reopen input during installation");
                    window.Close();Check(!window.IsDisposed,"titlebar cannot bypass pending installation");
                    // Do not pump the queued network operation in this protocol
                    // test. The parent integration suite supplies HTTP tests.
                    Program.IncrementalInstalling=false;Field(window,"exitApproved",true);window.Close();
                }
                Program.IncrementalInstalling=true;
                var context=(DesktopContext)FormatterServices.GetUninitializedObject(typeof(DesktopContext));
                refused=false;try{context.Open(new string[]{"unknown.md"});}catch(IOException){refused=true;}
                Check(refused,"reader inbox cannot open another draft during install");
                Program.IncrementalInstalling=false;
                Console.WriteLine("Incremental native checks: "+checks);return 0;
            }
            catch(Exception error){Console.WriteLine(error);return 1;}
            finally{Program.IncrementalInstalling=false;Directory.Delete(temporary,true);}
        }
    }
}
