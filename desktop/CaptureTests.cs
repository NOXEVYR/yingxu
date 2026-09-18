using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

namespace YingXu.Desktop
{
    internal static class CaptureTests
    {
        private const string Project="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
        private const string Item="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
        private static int passed;
        private static readonly JavaScriptSerializer Json=new JavaScriptSerializer();
        private static void Check(bool value,string name) { if(!value)throw new Exception("FAILED: "+name); passed++; Console.WriteLine("PASS "+name); }
        private static Dictionary<string,object> Context(string request,string project=Project,string item=Item)
        {
            return new Dictionary<string,object>{{"action","capture-context"},{"requestId",request},{"projectId",project},{"itemId",item}};
        }
        private static Bitmap Picture(int width=64,int height=32)
        {
            var image=new Bitmap(width,height); using(var graphics=Graphics.FromImage(image))graphics.Clear(Color.FromArgb(46,120,78));return image;
        }
        private sealed class Fixture : IDisposable
        {
            internal CaptureCoordinator Coordinator;
            internal CaptureServices Services;
            internal int Screens,Selections,Copies,Uploads;
            internal bool Reply=true,Deliver=true,Cancel,CopyFails,UploadFails;
            internal string Target=Project,Request;
            internal readonly List<CaptureResult> Results=new List<CaptureResult>();
            internal Fixture()
            {
                Services=new CaptureServices {
                    ScreenBounds=()=>{Screens++;return new Rectangle(-100,20,64,32);},
                    Select=bounds=>{Selections++;Check(bounds.X==-100,"locked screen keeps negative physical origin");return Cancel?null:Picture();},
                    Copy=image=>{Copies++;if(CopyFails)throw new IOException("clipboard busy");},
                    Upload=(image,project)=>{Uploads++;Check(project==Project,"attachment uses locked project");if(UploadFails)throw new IOException("upload failed");return new {id=Item,project_id=Project,kind="image"};}
                };
                Coordinator=new CaptureCoordinator(message=>{
                    var result=message as CaptureResult;
                    if(result!=null){if(Deliver)Results.Add(result);return Deliver;}
                    var payload=Json.Deserialize<Dictionary<string,object>>(Json.Serialize(message));Request=(string)payload["requestId"];
                    if(Reply)Check(Coordinator.Receive(Context(Request,Target)),"matching context accepted");return true;
                },(message,error)=>{},Services);
                Coordinator.ContextTimeoutMs=15;
            }
            public void Dispose(){Coordinator.Dispose();}
        }
        private static async Task Workflow()
        {
            using(var f=new Fixture())
            {
                Check(f.Screens==0&&f.Selections==0&&!f.Coordinator.Busy,"idle coordinator has no screen reads or bitmap allocation");
                await f.Coordinator.StartAsync();Check(f.Copies==1&&f.Uploads==1&&f.Results[0].clipboardCopied&&f.Results[0].item!=null,"clipboard and attachment independently complete");
                Check(!f.Coordinator.Receive(Context(f.Request)),"late context after completion rejected");
            }
            using(var f=new Fixture()){f.Target=null;await f.Coordinator.StartAsync();Check(f.Copies==1&&f.Uploads==0&&f.Results[0].saveError!=null,"empty project copies without guessing a target");}
            using(var f=new Fixture()){f.Reply=false;await f.Coordinator.StartAsync();Check(f.Selections==1&&f.Copies==1&&f.Uploads==0,"finite context timeout still permits clipboard capture");}
            using(var f=new Fixture()){f.Cancel=true;await f.Coordinator.StartAsync();Check(f.Results[0].cancelled&&f.Copies==0&&f.Uploads==0,"cancelled selection does not touch clipboard or project");}
            using(var f=new Fixture()){f.CopyFails=true;await f.Coordinator.StartAsync();Check(!f.Results[0].clipboardCopied&&f.Results[0].clipboardError!=null&&f.Results[0].item!=null,"clipboard failure preserves successful attachment");}
            using(var f=new Fixture()){f.UploadFails=true;await f.Coordinator.StartAsync();Check(f.Results[0].clipboardCopied&&f.Results[0].saveError!=null&&f.Results[0].item==null,"upload failure preserves clipboard success");}
            using(var f=new Fixture())
            {
                f.Reply=false;var first=f.Coordinator.StartAsync();await f.Coordinator.StartAsync();Check(f.Screens==1,"single-task gate rejects repeated triggers");
                Check(!f.Coordinator.Receive(Context("stale")),"stale handshake cannot redirect capture");
                Check(f.Coordinator.Receive(Context(f.Request)),"current handshake completes pending capture");await first;
            }
            using(var f=new Fixture()){f.Deliver=false;await f.Coordinator.StartAsync();Check(f.Results.Count==0,"unready frontend retains result metadata");f.Deliver=true;f.Coordinator.Flush();f.Coordinator.Flush();Check(f.Results.Count==1,"queued capture result delivered exactly once after ready");}
            using(var f=new Fixture()){f.Reply=false;var pending=f.Coordinator.StartAsync();f.Coordinator.Dispose();await pending;Check(f.Selections==0&&f.Results.Count==0,"disposal during handshake prevents screen capture");}
            using(var f=new Fixture()){f.Services.Select=bounds=>{throw new IOException("synthetic capture failure");};await f.Coordinator.StartAsync();Check(!f.Coordinator.Busy&&f.Results[0].error.Contains("synthetic capture failure"),"capture failure releases busy gate");}
        }
        private static void GeometryAndHotkeys()
        {
            uint modifiers,key;
            Check(CaptureHotkey.TryParse(CaptureHotkey.Default,out modifiers,out key)&&modifiers==0x4007&&key=='S',"default hotkey has no-repeat modifiers");
            Check(CaptureHotkey.TryParse("Shift+Ctrl+F24",out modifiers,out key)&&key==0x87,"custom modifier order and F24 supported");
            foreach(string value in new[]{"Win+Shift+S","Ctrl+S","Ctrl+Alt+F12","Ctrl+Ctrl+S","Ctrl+Alt+F25","Ctrl+Alt+é","Ctrl+Alt+F01"})
                Check(!CaptureHotkey.TryParse(value,out modifiers,out key),"reserved or malformed hotkey rejected");
            int registered=0,removed=0;bool available=true;
            using(var hotkey=new CaptureHotkey((h,id,m,k)=>{registered++;return available;},(h,id)=>removed++))
            {
                Check(hotkey.Configure(new IntPtr(1),true,CaptureHotkey.Default)==null&&hotkey.Registered,"hotkey registration succeeds");
                hotkey.Configure(new IntPtr(1),true,CaptureHotkey.Default);Check(registered==1,"unchanged settings do not register twice");
                available=false;Check(hotkey.Configure(new IntPtr(1),true,"Ctrl+Shift+F10")!=null&&!hotkey.Registered&&removed==1,"occupied hotkey leaves disabled state and explains conflict");
                hotkey.Configure(new IntPtr(1),false,CaptureHotkey.Default);Check(registered==2,"disabled hotkey has no registration work");
            }
            CaptureContext context;
            Check(CaptureContext.TryRead(Context("r",null),"r",out context)&&context.ProjectId==null&&context.ItemId==null,"empty project also clears insertion item");
            Check(!CaptureContext.TryRead(Context("r","../../secret"),"r",out context),"context rejects arbitrary project path");
            var extra=Context("r");extra["path"]="not allowed";Check(!CaptureContext.TryRead(extra,"r",out context),"context rejects extra path field");
            Check(CapturePlatform.Selection(new Point(80,40),new Point(-5,-10),new Size(64,32))==new Rectangle(0,0,64,32),"reverse drag clamps to the original screen only");
            using(var image=Picture())
            {
                image.SetPixel(8,9,Color.Red);
                using(var crop=CapturePlatform.Crop(image,new Rectangle(8,9,10,11)))
                {
                    Check(crop.Width==10&&crop.Height==11&&crop.GetPixel(0,0).ToArgb()==Color.Red.ToArgb(),"crop uses exact bitmap pixels without DPI scaling");
                    byte[] png=CapturePlatform.Encode(crop);using(var stream=new MemoryStream(png))using(var decoded=new Bitmap(stream))Check(decoded.GetPixel(0,0).ToArgb()==Color.Red.ToArgb(),"PNG roundtrip preserves synthetic pixels");
                }
                Check(CapturePlatform.Crop(image,new Rectangle(0,0,1,1))==null&&CapturePlatform.Crop(image,new Rectangle(-1,0,10,10))==null,"empty or out-of-screen regions are rejected");
            }
        }
        private static async Task UploadProtocol()
        {
            var listener=new TcpListener(IPAddress.Loopback,0);listener.Start();Hub.Url="http://127.0.0.1:"+((IPEndPoint)listener.LocalEndpoint).Port+"/";
            byte[] png;using(var image=Picture())png=CapturePlatform.Encode(image);
            var server=Task.Run(()=>{
                for(int requestIndex=0;requestIndex<2;requestIndex++)using(var client=listener.AcceptTcpClient())using(var stream=client.GetStream())
                {
                    client.ReceiveTimeout=5000;var header=new List<byte>();int value;
                    while((value=stream.ReadByte())>=0){header.Add((byte)value);int n=header.Count;if(n>=4&&header[n-4]==13&&header[n-3]==10&&header[n-2]==13&&header[n-1]==10)break;if(n>16384)throw new IOException("fixture header too long");}
                    string text=Encoding.ASCII.GetString(header.ToArray());
                    if(text.IndexOf("Expect: 100-continue",StringComparison.OrdinalIgnoreCase)>=0){byte[] interim=Encoding.ASCII.GetBytes("HTTP/1.1 100 Continue\r\n\r\n");stream.Write(interim,0,interim.Length);}
                    if(requestIndex==1)
                    {
                        Check(text.Contains("POST /api/upload?project="+Project+"&category=references&name="),"binary upload uses project reference attachment endpoint");
                        Check(text.Contains("Origin: "+Hub.Url.TrimEnd('/'))&&text.Contains("X-YingXu-Token: fixture-token"),"upload carries same-origin and fresh fixture token");
                        var body=new byte[png.Length];int read=0;while(read<body.Length){int n=stream.Read(body,read,body.Length-read);if(n==0)throw new IOException("short upload");read+=n;}
                        Check(Convert.ToBase64String(body)==Convert.ToBase64String(png),"PNG is sent as original binary bytes");
                    }
                    string json=requestIndex==0?"{\"token\":\"fixture-token\"}":Json.Serialize(new{id=Item,project_id=Project,kind="image"});
                    byte[] data=Encoding.UTF8.GetBytes(json),headers=Encoding.ASCII.GetBytes("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "+data.Length+"\r\nConnection: close\r\n\r\n");stream.Write(headers,0,headers.Length);stream.Write(data,0,data.Length);
                }
            });
            try{var result=await Task.Run(()=>DesktopApi.UploadCapture(png,Project));await server;Check(result is Dictionary<string,object>,"upload returns the resource metadata directly");}finally{listener.Stop();}
        }
        private static void SelectorEvents()
        {
            Action<CaptureSelector,string,object> fire=(form,method,args)=>typeof(CaptureSelector).GetMethod(method,BindingFlags.Instance|BindingFlags.NonPublic).Invoke(form,new[]{args});
            using(var image=Picture(640,480))
            using(var selector=new CaptureSelector(image,new Rectangle(100,100,640,480),"quick"))
            using(var painted=new Bitmap(640,480))
            {
                fire(selector,"OnMouseDown",new MouseEventArgs(MouseButtons.Left,1,100,100,0));
                fire(selector,"OnMouseMove",new MouseEventArgs(MouseButtons.Left,0,300,300,0));
                selector.DrawToBitmap(painted,new Rectangle(0,0,640,480));
                Check(selector.Area==new Rectangle(100,100,200,200),"native selector mouse events produce expected pixel region");
                Check(painted.GetPixel(150,150).ToArgb()==image.GetPixel(150,150).ToArgb()&&painted.GetPixel(500,400).G<image.GetPixel(500,400).G,"native selector preserves selected pixels and shades only outside synthetic image");
                fire(selector,"OnMouseUp",new MouseEventArgs(MouseButtons.Left,1,300,300,0));
                Check(selector.DialogResult==DialogResult.OK,"native selector mouse release confirms region");
            }
            using(var image=Picture())using(var selector=new CaptureSelector(image,new Rectangle(100,100,64,32)))
            {
                fire(selector,"OnKeyDown",new KeyEventArgs(Keys.Escape));
                Check(selector.DialogResult==DialogResult.Cancel,"native selector Escape cancels without output");
            }
        }
        private static void Fire(CaptureSelector selector,string method,object args)
        {
            typeof(CaptureSelector).GetMethod(method,BindingFlags.Instance|BindingFlags.NonPublic).Invoke(selector,new[]{args});
        }
        private static void Drag(CaptureSelector selector,Point first,Point last)
        {
            Fire(selector,"OnMouseDown",new MouseEventArgs(MouseButtons.Left,1,first.X,first.Y,0));
            Fire(selector,"OnMouseMove",new MouseEventArgs(MouseButtons.Left,0,last.X,last.Y,0));
            Fire(selector,"OnMouseUp",new MouseEventArgs(MouseButtons.Left,1,last.X,last.Y,0));
        }
        private static async Task AnnotationWorkflow()
        {
            foreach(string tool in new[]{"pen","arrow","rectangle"})
            using(var image=Picture(640,480))using(var selector=new CaptureSelector(image,new Rectangle(-640,0,640,480)))
            {
                Drag(selector,new Point(50,60),new Point(350,260));
                Check(selector.Annotating&&selector.DialogResult==DialogResult.None&&selector.CreateResult()==null,"default mode stops after selection until explicit confirmation");
                selector.DrawingTool=tool;selector.DrawingColor=Color.Blue;selector.DrawingWidth=8;
                Drag(selector,new Point(100,100),new Point(200,tool=="rectangle"?180:100));
                selector.DrawingColor=Color.Red;selector.DrawingWidth=2;
                Drag(selector,new Point(100,210),new Point(200,210));
                Check(selector.StrokeCount==2,"annotation stores separate undoable strokes: "+tool);
                selector.UndoStroke();Check(selector.StrokeCount==1,"undo removes only the latest annotation");
                selector.Confirm();using(var result=selector.CreateResult())
                {
                    Check(result.Width==300&&result.Height==200,"annotation crop retains physical selected pixel size");
                    Color marked=result.GetPixel(100,40);
                    Check(marked.B>200&&marked.R<50,"confirmed crop contains selected annotation color and geometry: "+tool);
                    Check(result.GetPixel(100,150).ToArgb()==image.GetPixel(150,210).ToArgb(),"undo restores original pixels instead of painting over them");
                }
                Check(image.GetPixel(150,100).ToArgb()==Color.FromArgb(46,120,78).ToArgb(),"annotation never mutates the original frozen bitmap");
            }
            using(var image=Picture(640,480))using(var selector=new CaptureSelector(image,new Rectangle(0,0,640,480)))
            {
                Drag(selector,new Point(50,60),new Point(350,260));Drag(selector,new Point(100,100),new Point(800,900));
                Check(selector.StrokeCount==1,"drawing outside screen clamps to the selected region");
                Fire(selector,"OnMouseDown",new MouseEventArgs(MouseButtons.Left,1,120,120,0));
                Fire(selector,"OnKeyDown",new KeyEventArgs(Keys.Control|Keys.Z));
                Check(selector.StrokeCount==1&&!selector.Capture,"undo during a held stroke cancels it and releases mouse capture without losing committed marks");
                Fire(selector,"OnKeyDown",new KeyEventArgs(Keys.Escape));
                Check(selector.DialogResult==DialogResult.Cancel&&selector.CreateResult()==null,"Escape after annotation never produces an output bitmap");
            }
            using(var image=Picture(640,480))using(var selector=new CaptureSelector(image,new Rectangle(0,0,640,480)))
            {
                Drag(selector,new Point(50,60),new Point(350,260));
                Fire(selector,"OnMouseDown",new MouseEventArgs(MouseButtons.Left,1,100,100,0));selector.Capture=false;
                Check(selector.StrokeCount==0,"lost mouse capture discards an incomplete mark");
                object[] args={new Message(),Keys.Escape};
                bool consumed=(bool)typeof(CaptureSelector).GetMethod("ProcessCmdKey",BindingFlags.Instance|BindingFlags.NonPublic).Invoke(selector,args);
                Check(consumed&&selector.DialogResult==DialogResult.Cancel&&selector.CreateResult()==null,"toolbar command processing consumes Escape and cancels before output");
            }
            using(var f=new Fixture())
            {
                f.Services.Select=bounds=>{using(var image=Picture(640,480))using(var selector=new CaptureSelector(image,new Rectangle(0,0,640,480))) {
                    Drag(selector,new Point(50,60),new Point(350,260));Drag(selector,new Point(100,100),new Point(200,100));Fire(selector,"OnKeyDown",new KeyEventArgs(Keys.Escape));return selector.CreateResult(); }};
                await f.Coordinator.StartAsync();Check(f.Copies==0&&f.Uploads==0&&f.Results[0].cancelled,"annotation cancellation reaches coordinator without clipboard or upload side effects");
            }
            using(var f=new Fixture())
            {
                f.Reply=false;f.Coordinator.Mode="quick";f.Services.Select=null;string received=null;
                f.Services.SelectMode=(bounds,mode)=>{received=mode;return null;};
                var task=f.Coordinator.StartAsync();f.Coordinator.Mode="annotate";f.Coordinator.Receive(Context(f.Request));await task;
                Check(received=="quick","capture locks configured mode before asynchronous context handshake");
            }
        }
        private static void ExtendedAnnotation(string previewPath=null)
        {
            using(var image=Picture(640,480))using(var selector=new CaptureSelector(image,new Rectangle(0,0,640,480)))
            {
                Drag(selector,new Point(40,60),new Point(540,360));
                var toolbar=(FlowLayoutPanel)selector.Controls[0];
                Check(toolbar.Controls.Count==15&&toolbar.Controls[0].AccessibleName.Contains("Shift")&&toolbar.Controls[1].AccessibleName=="矩形"&&toolbar.Controls[2].AccessibleName=="箭头"&&toolbar.Controls[3].AccessibleName.Contains("马赛克"),"drawing tools are individual accessible buttons, not hidden in a menu");
                Check(((CaptureToolButton)toolbar.Controls[4]).Swatch==selector.DrawingColor&&((CaptureToolButton)toolbar.Controls[9]).Swatch==Color.White,"annotation colors are visible swatches");
                typeof(Button).GetMethod("OnClick",BindingFlags.Instance|BindingFlags.NonPublic).Invoke(toolbar.Controls[1],new object[]{EventArgs.Empty});Check(selector.DrawingTool=="rectangle","rectangle icon selects its drawing tool");
                typeof(Button).GetMethod("OnClick",BindingFlags.Instance|BindingFlags.NonPublic).Invoke(toolbar.Controls[7],new object[]{EventArgs.Empty});Check(selector.DrawingColor==((CaptureToolButton)toolbar.Controls[7]).Swatch,"color swatch directly selects drawing color");
                Check(toolbar.Controls[13].Text==""&&toolbar.Controls[14].Text==""&&toolbar.Controls[13].AccessibleName.StartsWith("确认")&&toolbar.Controls[14].AccessibleName.StartsWith("取消"),"confirm and cancel use drawn icons with accessible names");
                selector.DrawingTool="pen";selector.DrawingColor=Color.Blue;selector.DrawingWidth=4;
                Fire(selector,"OnKeyDown",new KeyEventArgs(Keys.ShiftKey));
                Fire(selector,"OnMouseDown",new MouseEventArgs(MouseButtons.Left,1,100,100,0));
                Fire(selector,"OnMouseMove",new MouseEventArgs(MouseButtons.Left,0,150,200,0));
                Fire(selector,"OnMouseMove",new MouseEventArgs(MouseButtons.Left,0,180,80,0));
                Fire(selector,"OnMouseUp",new MouseEventArgs(MouseButtons.Left,1,240,100,0));
                Fire(selector,"OnKeyUp",new KeyEventArgs(Keys.ShiftKey));
                typeof(Button).GetMethod("OnClick",BindingFlags.Instance|BindingFlags.NonPublic).Invoke(toolbar.Controls[10],new object[]{EventArgs.Empty});
                Check(selector.DrawingWidth==8&&((CaptureToolButton)toolbar.Controls[10]).StrokeWidth==8,"width preview button changes the actual brush width");
                selector.Confirm(true);
                using(var result=selector.CreateResult())
                {
                    Check(result.GetPixel(140,40).B>200&&result.GetPixel(110,140).ToArgb()==image.GetPixel(150,200).ToArgb(),"Shift pen creates a straight segment despite a curved cursor path");
                    Check(selector.PinRequested,"pin button confirmation records the requested output action");
                    using(var pin=new CapturePinWindow(result,new Point(30,40),new Rectangle(0,0,640,480)))
                    {
                        Check(pin.TopMost&&!pin.ShowInTaskbar&&pin.FormBorderStyle==FormBorderStyle.SizableToolWindow,"desktop pin is topmost with a closeable resizeable tool window");
                        Point before=pin.Location;
                        typeof(CapturePinWindow).GetMethod("OnMouseDown",BindingFlags.Instance|BindingFlags.NonPublic).Invoke(pin,new object[]{new MouseEventArgs(MouseButtons.Left,1,20,20,0)});
                        typeof(CapturePinWindow).GetMethod("OnMouseMove",BindingFlags.Instance|BindingFlags.NonPublic).Invoke(pin,new object[]{new MouseEventArgs(MouseButtons.Left,0,30,35,0)});
                        typeof(CapturePinWindow).GetMethod("OnMouseUp",BindingFlags.Instance|BindingFlags.NonPublic).Invoke(pin,new object[]{new MouseEventArgs(MouseButtons.Left,1,30,35,0)});
                        Check(pin.Location==new Point(before.X+10,before.Y+15)&&!pin.Capture,"dragging a pin moves the window and releases pointer capture");
                        Bitmap owned=(Bitmap)typeof(CapturePinWindow).GetField("image",BindingFlags.Instance|BindingFlags.NonPublic).GetValue(pin);
                        result.SetPixel(140,40,Color.Magenta);
                        Check(owned.GetPixel(140,40).B>200&&owned.GetPixel(140,40).R<50,"pin owns independent pixels after result changes");
                        pin.Dispose();bool released=false;try{owned.GetPixel(0,0);}catch(ArgumentException){released=true;}
                        Check(released,"closing a pin releases its owned bitmap");
                    }
                }
            }
            foreach(bool undoMosaic in new[]{false,true})
            using(var image=Picture(320,240))using(var selector=new CaptureSelector(image,new Rectangle(0,0,320,240)))
            {
                for(int y=0;y<240;y++)for(int x=0;x<320;x++)image.SetPixel(x,y,(x+y)%2==0?Color.Black:Color.White);
                Drag(selector,new Point(20,30),new Point(280,200));selector.DrawingTool="mosaic";
                Drag(selector,new Point(50,60),new Point(150,160));Check(selector.StrokeCount==1,"mosaic is one undoable annotation");
                if(undoMosaic)selector.UndoStroke();
                selector.Confirm();using(var result=selector.CreateResult())
                {
                    Color first=result.GetPixel(44,44),second=result.GetPixel(45,44);
                    Check(undoMosaic?first.ToArgb()!=second.ToArgb():first.ToArgb()==second.ToArgb(),undoMosaic?"undo mosaic restores exact original detail":"mosaic removes fine pixel detail in the final bitmap");
                    Check(result.GetPixel(5,5).ToArgb()==image.GetPixel(25,35).ToArgb(),"mosaic preserves pixels outside the drawn rectangle");
                    byte[] png=CapturePlatform.Encode(result);using(var stream=new MemoryStream(png))using(var decoded=new Bitmap(stream))Check(decoded.GetPixel(44,44).ToArgb()==first.ToArgb(),"encoded output contains the mosaic pixels");
                }
                Check(image.GetPixel(64,74).ToArgb()!=image.GetPixel(65,74).ToArgb(),"mosaic never changes the frozen source image");
            }
            using(var image=Picture(240,320))using(var selector=new CaptureSelector(image,new Rectangle(0,0,240,320)))
            using(var painted=new Bitmap(240,320))
            {
                Drag(selector,new Point(10,20),new Point(230,240));selector.DrawToBitmap(painted,new Rectangle(0,0,240,320));
                var toolbar=(FlowLayoutPanel)selector.Controls[0];bool inside=true;
                foreach(Control control in toolbar.Controls)inside&=toolbar.ClientRectangle.Contains(control.Bounds);
                Check(inside&&selector.ClientRectangle.Contains(toolbar.Bounds),"small-screen toolbar wraps all tools including confirm and cancel inside the screen");
                Fire(selector,"OnKeyDown",new KeyEventArgs(Keys.Escape));Check(!selector.PinRequested&&selector.CreateResult()==null,"cancel cannot request a desktop pin");
            }
            using(var image=Picture())
            {
                var pin=new CapturePinWindow(image,new Point(0,0),new Rectangle(0,0,640,480));
                var tracked=(List<CapturePinWindow>)typeof(CapturePinWindow).GetField("pins",BindingFlags.Static|BindingFlags.NonPublic).GetValue(null);
                tracked.Add(pin);CapturePinWindow.CloseAll();
                Check(pin.IsDisposed&&tracked.Count==0,"application-exit cleanup disposes all tracked pins");
            }
            using(var image=Picture(1060,730))using(var selector=new CaptureSelector(image,new Rectangle(0,0,1060,730),"annotate",1.5f))
            {
                Drag(selector,new Point(80,100),new Point(970,550));var toolbar=(CaptureToolbar)selector.Controls[0];
                Check(toolbar.Controls[0].Width==60&&toolbar.Controls[0].Height==60,"150-percent DPI scales icon hit targets with their artwork");
                bool inside=selector.ClientRectangle.Contains(toolbar.Bounds);foreach(Control button in toolbar.Controls)inside&=toolbar.ClientRectangle.Contains(button.Bounds);
                Check(inside,"scaled screenshot toolbar keeps all actions inside the monitor");
            }
            using(var image=Picture(640,480))using(var selector=new CaptureSelector(image,new Rectangle(0,0,640,480)))
            {
                Drag(selector,new Point(40,50),new Point(540,400));selector.DrawingColor=Color.Blue;
                Fire(selector,"OnKeyDown",new KeyEventArgs(Keys.ShiftKey));Fire(selector,"OnDeactivate",EventArgs.Empty);
                Fire(selector,"OnMouseDown",new MouseEventArgs(MouseButtons.Left,1,100,100,0));
                Fire(selector,"OnMouseMove",new MouseEventArgs(MouseButtons.Left,0,150,200,0));Fire(selector,"OnMouseUp",new MouseEventArgs(MouseButtons.Left,1,240,100,0));
                selector.Confirm();using(var result=selector.CreateResult())Check(result.GetPixel(110,150).B>200,"losing focus clears Shift so a later brush stroke remains freehand");
            }
            foreach(var size in new[]{new Size(640,480),new Size(1024,768),new Size(1920,1080)})
            foreach(float scale in new[]{1f,1.5f,2.5f})
            using(var image=Picture(size.Width,size.Height))
            using(var selector=new CaptureSelector(image,new Rectangle(-size.Width,0,size.Width,size.Height),"annotate",scale))
            {
                // Full-screen selections force an inside placement. Controls must remain usable,
                // while the output still contains the exact screenshot, never toolbar pixels.
                Drag(selector,Point.Empty,new Point(size.Width,size.Height));var toolbar=(CaptureToolbar)selector.Controls[0];
                bool visible=selector.ClientRectangle.Contains(toolbar.Bounds);
                foreach(Control button in toolbar.Controls)visible&=toolbar.ClientRectangle.Contains(button.Bounds);
                Check(visible,"full-screen selection keeps every action on screen at "+size+" scale "+scale);
                selector.Confirm();using(var result=selector.CreateResult())
                {
                    bool clean=result.Size==size;
                    foreach(Control button in toolbar.Controls)
                    {
                        int x=toolbar.Left+button.Left+button.Width/2,y=toolbar.Top+button.Top+button.Height/2;
                        clean&=result.GetPixel(x,y).ToArgb()==image.GetPixel(x,y).ToArgb();
                    }
                    Check(clean,"floating controls never enter confirmed crop at "+size+" scale "+scale);
                }
            }
            using(var image=Picture(1060,730))using(var selector=new CaptureSelector(image,new Rectangle(0,0,1060,730)))
            {
                Drag(selector,new Point(122,122),new Point(938,590));var toolbar=(CaptureToolbar)selector.Controls[0];
                Check(toolbar.Right==selector.Area.Right&&toolbar.Top>selector.Area.Bottom,"normal selection anchors toolbar to selection's lower right without obscuring pixels");
            }
            if(previewPath!=null)
            {
                PreviewAnnotation(previewPath);
                string folder=Path.GetDirectoryName(Path.GetFullPath(previewPath)),name=Path.GetFileNameWithoutExtension(previewPath);
                PreviewAnnotation(Path.Combine(folder,name+"-150.png"),1060,730,1.5f);
                PreviewAnnotation(Path.Combine(folder,name+"-narrow.png"),640,480,1f);
            }
        }
        private static void PreviewAnnotation(string path,int width=1060,int height=730,float scale=1)
        {
            using(var image=new Bitmap(width,height))
            {
                using(var g=Graphics.FromImage(image))
                using(var title=new Font("Microsoft YaHei UI",25,FontStyle.Bold,GraphicsUnit.Pixel))
                using(var body=new Font("Microsoft YaHei UI",13,FontStyle.Regular,GraphicsUnit.Pixel))
                using(var ink=new SolidBrush(Color.FromArgb(48,53,62)))
                using(var muted=new SolidBrush(Color.FromArgb(127,134,145)))
                using(var paper=new SolidBrush(Color.FromArgb(247,248,250)))
                {
                    g.ScaleTransform(width/1060f,height/730f);g.SmoothingMode=System.Drawing.Drawing2D.SmoothingMode.AntiAlias;g.TextRenderingHint=System.Drawing.Text.TextRenderingHint.AntiAliasGridFit;g.Clear(Color.FromArgb(231,234,239));
                    using(var card=CaptureVisuals.Rounded(new RectangleF(100,102,860,510),18))g.FillPath(Brushes.White,card);
                    g.DrawString("创作工作台",title,ink,142,142);g.DrawString("项目素材 / 视觉参考",body,muted,144,188);
                    using(var line=new Pen(Color.FromArgb(232,235,239)))g.DrawLine(line,144,222,916,222);
                    string[] labels={"构图与光影","角色设计","制作信息"};
                    for(int i=0;i<3;i++)
                    {
                        int x=145+i*258;using(var tile=CaptureVisuals.Rounded(new RectangleF(x,246,230,238),12))g.FillPath(paper,tile);
                        using(var grad=new System.Drawing.Drawing2D.LinearGradientBrush(new Rectangle(x+12,258,206,146),Color.FromArgb(208-i*12,215-i*8,226),Color.FromArgb(101+i*15,118+i*12,141),45))
                        using(var art=CaptureVisuals.Rounded(new RectangleF(x+12,258,206,146),8))g.FillPath(grad,art);
                        using(var glow=new SolidBrush(Color.FromArgb(80,255,255,255)))g.FillEllipse(glow,x+40,276,110,110);
                        using(var mountain=new SolidBrush(Color.FromArgb(79+i*10,92+i*8,112)))g.FillPolygon(mountain,new[]{new Point(x+12,404),new Point(x+81,317),new Point(x+136,365),new Point(x+177,324),new Point(x+218,404)});
                        g.DrawString(labels[i],body,ink,x+16,423);g.DrawString("待整理  ·  本地素材",body,muted,x+16,450);
                    }
                    g.DrawString("素材备注",body,ink,145,523);g.DrawString("scene_reference_001  /  internal_notes",body,muted,145,550);
                }
                using(var selector=new CaptureSelector(image,new Rectangle(0,0,image.Width,image.Height),"annotate",scale))
                {
                    Func<int,int,Point> point=(x,y)=>new Point(x*width/1060,y*height/730);
                    Drag(selector,point(122,122),point(938,590));selector.DrawingTool="rectangle";selector.DrawingWidth=3;
                    Drag(selector,point(136,235),point(385,494));selector.DrawingTool="arrow";
                    Drag(selector,point(640,185),point(397,252));selector.DrawingTool="mosaic";selector.DrawingWidth=4;
                    Drag(selector,point(144,545),point(408,570));selector.DrawingTool="arrow";
                    var toolbar=(CaptureToolbar)selector.Controls[0];
                    typeof(Button).GetMethod("OnClick",BindingFlags.Instance|BindingFlags.NonPublic).Invoke(toolbar.Controls[2],new object[]{EventArgs.Empty});
                    using(var preview=new Bitmap(image.Width,image.Height))
                    {
                        selector.DrawToBitmap(preview,new Rectangle(Point.Empty,preview.Size));Point location=toolbar.Location;Color backdrop=preview.GetPixel(location.X,location.Y);toolbar.Parent=null;toolbar.BackColor=backdrop;
                        try
                        {
                            using(var rendered=new Bitmap(toolbar.Width,toolbar.Height))
                            {toolbar.DrawToBitmap(rendered,new Rectangle(Point.Empty,rendered.Size));using(var g=Graphics.FromImage(preview))g.DrawImageUnscaled(rendered,location);}
                        }
                        finally {toolbar.BackColor=Color.Transparent;toolbar.Parent=selector;toolbar.Location=location;}
                        preview.Save(path,System.Drawing.Imaging.ImageFormat.Png);
                    }
                }
            }
        }
        [STAThread]
        private static int Main(string[] args)
        {
            WindowsFormsSynchronizationContext.AutoInstall=false;
            if(args.Length==3&&args[0]=="--upload-fixture")
            {
                Uri url;if(!Uri.TryCreate(args[1],UriKind.Absolute,out url)||url.Scheme!="http"||url.Host!="127.0.0.1")throw new ArgumentException("Fixture must use loopback HTTP");
                Hub.Url=url.AbsoluteUri;using(var image=Picture(128,64))Console.WriteLine(Json.Serialize(DesktopApi.UploadCapture(CapturePlatform.Encode(image),args[2])));return 0;
            }
            GeometryAndHotkeys();SelectorEvents();Workflow().GetAwaiter().GetResult();AnnotationWorkflow().GetAwaiter().GetResult();ExtendedAnnotation(args.Length==2&&args[0]=="--preview"?args[1]:null);UploadProtocol().GetAwaiter().GetResult();
            var timer=Stopwatch.StartNew();long bytes;
            using(var image=Picture(3840,2160)){using(var region=CapturePlatform.Crop(image,new Rectangle(100,100,1920,1080)))bytes=CapturePlatform.Encode(region).Length;}
            Console.WriteLine("synthetic_4k_crop_png_ms="+timer.ElapsedMilliseconds+" png_bytes="+bytes+" full_frame_bytes="+(3840L*2160*4));
            Console.WriteLine("Capture tests passed: "+passed+"; no screen or clipboard content read or changed.");return 0;
        }
    }
}
