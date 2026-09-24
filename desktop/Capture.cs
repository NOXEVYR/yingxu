using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.Drawing.Drawing2D;
using System.IO;
using System.Runtime.InteropServices;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace YingXu.Desktop
{
    internal sealed class CaptureHotkey : IDisposable
    {
        internal const int Message = 0x0312;
        internal const int Id = 0x5943;
        internal const string Default = "Ctrl+Alt+Shift+S";
        private readonly Func<IntPtr,int,uint,uint,bool> register;
        private readonly Action<IntPtr,int> unregister;
        private IntPtr window;
        private string current;
        internal bool Registered { get; private set; }
        internal CaptureHotkey(Func<IntPtr,int,uint,uint,bool> add = null, Action<IntPtr,int> remove = null)
        {
            register = add ?? RegisterHotKey;
            unregister = remove ?? ((handle,id) => { UnregisterHotKey(handle,id); });
        }
        internal static bool TryParse(string value, out uint modifiers, out uint key)
        {
            modifiers = 0; key = 0;
            if (String.IsNullOrWhiteSpace(value) || value.Length > 64) return false;
            string[] parts = value.Split('+');
            if (parts.Length < 3 || parts.Length > 4) return false;
            for (int i=0;i<parts.Length-1;i++)
            {
                string part = parts[i].Trim().ToUpperInvariant();
                uint flag = part == "CTRL" ? 2u : part == "ALT" ? 1u : part == "SHIFT" ? 4u : 0u;
                if (flag == 0 || (modifiers & flag) != 0) return false;
                modifiers |= flag;
            }
            string last = parts[parts.Length-1].Trim().ToUpperInvariant();
            if (last.Length == 1 && ((last[0] >= 'A' && last[0] <= 'Z') || (last[0] >= '0' && last[0] <= '9'))) key = last[0];
            else if (last.StartsWith("F",StringComparison.Ordinal))
            {
                int number;
                if (!Int32.TryParse(last.Substring(1),out number) || last != "F"+number || number < 1 || number > 24 || number == 12) return false;
                key = (uint)(0x70+number-1);
            }
            if (key == 0) return false;
            modifiers |= 0x4000; // MOD_NOREPEAT: no repeated capture while held.
            return true;
        }
        internal string Configure(IntPtr handle, bool enabled, string value)
        {
            if (enabled && Registered && window == handle && current == value) return null;
            Dispose();
            if (!enabled) return null;
            uint modifiers,key;
            if (!TryParse(value,out modifiers,out key)) return "截图快捷键格式无效，请重新设置。";
            if (!register(handle,Id,modifiers,key)) return "截图快捷键已被占用或不可用；可更换组合，或点击工作台的截图按钮。";
            window = handle; current = value; Registered = true;
            return null;
        }
        public void Dispose()
        {
            if (Registered) unregister(window,Id);
            Registered = false; window = IntPtr.Zero; current = null;
        }
        [DllImport("user32.dll",SetLastError=true)]
        private static extern bool RegisterHotKey(IntPtr handle,int id,uint modifiers,uint key);
        [DllImport("user32.dll",SetLastError=true)]
        private static extern bool UnregisterHotKey(IntPtr handle,int id);
    }

    internal sealed class CaptureContext
    {
        internal string ProjectId;
        internal string ItemId;
        private static bool IdOrEmpty(object value)
        {
            return value == null || (value is string && ((string)value == "" || Regex.IsMatch((string)value,"\\A[a-f0-9]{32}\\z")));
        }
        internal static bool TryRead(Dictionary<string,object> message,string requestId,out CaptureContext result)
        {
            result = null; object action,request,project,item;
            if (message == null || message.Count != 4 || requestId == null ||
                !message.TryGetValue("action",out action) || (action as string) != "capture-context" ||
                !message.TryGetValue("requestId",out request) || (request as string) != requestId ||
                !message.TryGetValue("projectId",out project) || !message.TryGetValue("itemId",out item) || !IdOrEmpty(project) || !IdOrEmpty(item)) return false;
            result = new CaptureContext { ProjectId = String.IsNullOrEmpty(project as string) ? null : (string)project };
            result.ItemId = result.ProjectId == null || String.IsNullOrEmpty(item as string) ? null : (string)item;
            return true;
        }
    }
    internal sealed class CaptureResult
    {
        public string action = "capture-result";
        public string requestId;
        public object item;
        public bool clipboardCopied;
        public bool cancelled;
        public string error;
        public string clipboardError;
        public string saveError;
    }
    internal sealed class CaptureServices
    {
        internal Func<Rectangle> ScreenBounds = () => Screen.FromPoint(Cursor.Position).Bounds;
        internal Func<Rectangle,Bitmap> Select;
        internal Func<Rectangle,string,Bitmap> SelectMode = CapturePlatform.Select;
        internal Action<Bitmap> Copy = CapturePlatform.Copy;
        internal Func<Bitmap,string,object> Upload = (image,project) => DesktopApi.UploadCapture(CapturePlatform.Encode(image),project);
    }

    // Allocates no image and starts no timer until an explicit capture request.
    internal sealed class CaptureCoordinator : IDisposable
    {
        private readonly Func<object,bool> send;
        private readonly Action<string,bool> notice;
        private readonly CaptureServices services;
        private readonly Queue<CaptureResult> completed = new Queue<CaptureResult>();
        private TaskCompletionSource<CaptureContext> context;
        private string requestId;
        private bool disposed;
        internal int ContextTimeoutMs = 5000;
        internal string Mode = "annotate";
        internal bool Busy { get; private set; }
        internal CaptureCoordinator(Func<object,bool> post,Action<string,bool> notify,CaptureServices implementation = null)
        {
            send = post; notice = notify; services = implementation ?? new CaptureServices();
        }
        internal bool Receive(Dictionary<string,object> message)
        {
            CaptureContext value;
            return Busy && context != null && CaptureContext.TryRead(message,requestId,out value) && context.TrySetResult(value);
        }
        internal void Flush()
        {
            while (!disposed && completed.Count != 0 && send(completed.Peek())) completed.Dequeue();
        }
        internal async Task StartAsync()
        {
            if (disposed || Busy) return;
            if (completed.Count >= 8) { notice("工作台尚未接收之前的截图结果，请先重新打开工作台。",true); return; }
            Busy = true; requestId = Guid.NewGuid().ToString("N");
            string mode = Mode == "quick" ? "quick" : "annotate";
            var result = new CaptureResult { requestId = requestId };
            try
            {
                Rectangle screen = services.ScreenBounds();
                context = new TaskCompletionSource<CaptureContext>();
                CaptureContext target = null;
                if (send(new { action = "capture-context-request", requestId = requestId }))
                {
                    using (var delay = new CancellationTokenSource())
                    {
                        var timeout = Task.Delay(ContextTimeoutMs,delay.Token);
                        if (await Task.WhenAny(context.Task,timeout) == context.Task) target = await context.Task;
                        delay.Cancel();
                    }
                }
                context = null;
                if (disposed) return;
                using (Bitmap image = services.Select == null ? services.SelectMode(screen,mode) : services.Select(screen))
                {
                    if (image == null) { result.cancelled = true; return; }
                    try { services.Copy(image); result.clipboardCopied = true; }
                    catch (Exception error) { result.clipboardError = "剪贴板写入失败："+error.Message; }
                    if (target != null && target.ProjectId != null)
                    {
                        try { result.item = await Task.Run(() => services.Upload(image,target.ProjectId)); }
                        catch (Exception error) { result.saveError = "项目附件保存失败："+error.Message; }
                    }
                    else result.saveError = target == null ? "未及时取得工作台项目，截图未存入项目。" : "未选择项目，截图未存入项目。";
                }
                result.error = String.Join("\n",new[] { result.clipboardError,result.saveError }).Trim();
                notice(result.item != null ? (result.clipboardCopied ? "截图已复制，并保存到项目的参考资料。" : "截图已保存到项目，但未能写入剪贴板。") : result.clipboardCopied ? "截图已复制到剪贴板；"+result.saveError : result.error,
                    !result.clipboardCopied || (target != null && target.ProjectId != null && result.item == null));
            }
            catch (Exception error) { result.error = "截图未完成："+error.Message; notice(result.error,true); }
            finally
            {
                context = null; requestId = null; Busy = false;
                if (!disposed) { completed.Enqueue(result); Flush(); }
            }
        }
        public void Dispose() { disposed = true; if (context != null) context.TrySetResult(null); completed.Clear(); }
    }

    internal static class CapturePlatform
    {
        internal const long MaxPixels = 40000000;
        internal static byte[] Encode(Bitmap image)
        {
            using (var output = new MemoryStream()) { image.Save(output,ImageFormat.Png); return output.ToArray(); }
        }
        internal static void Copy(Bitmap image)
        {
            var data = new DataObject(); data.SetData(DataFormats.Bitmap,true,image);
            Clipboard.SetDataObject(data,true,3,40);
        }
        internal static Rectangle Selection(Point first,Point last,Size size)
        {
            int x1=Math.Max(0,Math.Min(size.Width,first.X)),y1=Math.Max(0,Math.Min(size.Height,first.Y));
            int x2=Math.Max(0,Math.Min(size.Width,last.X)),y2=Math.Max(0,Math.Min(size.Height,last.Y));
            return Rectangle.FromLTRB(Math.Min(x1,x2),Math.Min(y1,y2),Math.Max(x1,x2),Math.Max(y1,y2));
        }
        internal static Bitmap Crop(Bitmap image,Rectangle area)
        {
            if (area.Width < 2 || area.Height < 2 || !new Rectangle(Point.Empty,image.Size).Contains(area)) return null;
            return image.Clone(area,PixelFormat.Format32bppRgb);
        }
        internal static Bitmap Select(Rectangle bounds,string mode = "annotate")
        {
            if (bounds.Width <= 0 || bounds.Height <= 0 || (long)bounds.Width*bounds.Height > MaxPixels) throw new InvalidOperationException("当前屏幕尺寸超过截图上限。");
            IntPtr foreground = GetForegroundWindow();
            try
            {
                using (var screen = new Bitmap(bounds.Width,bounds.Height,PixelFormat.Format32bppRgb))
                {
                    using (var graphics = Graphics.FromImage(screen)) graphics.CopyFromScreen(bounds.Location,Point.Empty,bounds.Size,CopyPixelOperation.SourceCopy);
                    using (var selector = new CaptureSelector(screen,bounds,mode,UiScale(bounds)))
                    {
                        if(selector.ShowDialog()!=DialogResult.OK)return null;
                        Bitmap result=selector.CreateResult();
                        if(result!=null && selector.PinRequested)
                        {
                            try { CapturePinWindow.Open(result,new Point(bounds.X+selector.Area.X,bounds.Y+selector.Area.Y),bounds); }
                            catch { result.Dispose();throw; }
                        }
                        return result;
                    }
                }
            }
            finally { if (foreground != IntPtr.Zero && IsWindow(foreground)) SetForegroundWindow(foreground); }
        }
        [DllImport("user32.dll")] private static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll")] private static extern bool IsWindow(IntPtr handle);
        [DllImport("user32.dll")] private static extern bool SetForegroundWindow(IntPtr handle);
        private static float UiScale(Rectangle bounds)
        {
            try {var area=new NativeArea {Left=bounds.Left,Top=bounds.Top,Right=bounds.Right,Bottom=bounds.Bottom};uint x,y;
                if(GetDpiForMonitor(MonitorFromRect(ref area,2),0,out x,out y)==0)return Math.Max(1,Math.Min(2.5f,x/96f));}
            catch(DllNotFoundException){}catch(EntryPointNotFoundException){}
            return 1;
        }
        [StructLayout(LayoutKind.Sequential)] private struct NativeArea {internal int Left,Top,Right,Bottom;}
        [DllImport("user32.dll")] private static extern IntPtr MonitorFromRect(ref NativeArea area,uint flags);
        [DllImport("shcore.dll")] private static extern int GetDpiForMonitor(IntPtr monitor,int kind,out uint x,out uint y);
    }
    internal sealed class CaptureStroke
    {
        internal string Tool;
        internal Color Color;
        internal float Width;
        internal readonly List<Point> Points = new List<Point>();
        internal void Draw(Graphics graphics,Bitmap pixels=null,Point origin=default(Point))
        {
            if (Points.Count==0) return;
            if(Tool=="mosaic")
            {
                if(pixels==null||Points.Count<2)return;
                Point first=Points[0],last=Points[Points.Count-1];
                var area=Rectangle.Intersect(new Rectangle(Point.Empty,pixels.Size),Rectangle.FromLTRB(Math.Min(first.X,last.X)-origin.X,Math.Min(first.Y,last.Y)-origin.Y,Math.Max(first.X,last.X)-origin.X+1,Math.Max(first.Y,last.Y)-origin.Y+1));
                if(area.Width<1||area.Height<1)return;
                int block=Math.Max(8,(int)Width*3);
                using(var reduced=new Bitmap(Math.Max(1,(area.Width+block-1)/block),Math.Max(1,(area.Height+block-1)/block)))
                {
                    // Only allocate the reduced tiles, never a full-screen copy on mouse move.
                    using(var small=Graphics.FromImage(reduced)) { small.InterpolationMode=InterpolationMode.HighQualityBilinear;small.DrawImage(pixels,new Rectangle(Point.Empty,reduced.Size),area.X,area.Y,area.Width,area.Height,GraphicsUnit.Pixel); }
                    var saved=graphics.Save();
                    try {graphics.InterpolationMode=InterpolationMode.NearestNeighbor;graphics.PixelOffsetMode=PixelOffsetMode.Half;graphics.DrawImage(reduced,new Rectangle(area.X+origin.X,area.Y+origin.Y,area.Width,area.Height),0,0,reduced.Width,reduced.Height,GraphicsUnit.Pixel);}
                    finally {graphics.Restore(saved);}
                }
                return;
            }
            using(var pen=new Pen(Color,Width))
            {
                pen.StartCap=LineCap.Round; pen.EndCap=LineCap.Round; pen.LineJoin=LineJoin.Round;
                Point first=Points[0],last=Points[Points.Count-1];
                if(Tool=="rectangle")
                {
                    int x=Math.Min(first.X,last.X),y=Math.Min(first.Y,last.Y);
                    graphics.DrawRectangle(pen,x,y,Math.Abs(first.X-last.X),Math.Abs(first.Y-last.Y));
                }
                else if(Tool=="arrow" && first!=last)
                {
                    using(var cap=new AdjustableArrowCap(4,5,true)) { pen.CustomEndCap=cap; graphics.DrawLine(pen,first,last); }
                }
                else if(Points.Count>1) graphics.DrawLines(pen,Points.ToArray());
                else using(var brush=new SolidBrush(Color)) graphics.FillEllipse(brush,first.X-Width/2,first.Y-Width/2,Width,Width);
            }
        }
    }
    // Each pin owns a copy; closing the selector or clipboard upload cannot invalidate it.
    internal sealed class CapturePinWindow : Form
    {
        private Bitmap image;
        private Point dragOrigin;
        private Point windowOrigin;
        private bool dragging;
        private static readonly List<CapturePinWindow> pins=new List<CapturePinWindow>();
        static CapturePinWindow() { Application.ApplicationExit+=(s,e)=>CloseAll(); }
        internal CapturePinWindow(Bitmap source,Point location,Rectangle desktop)
        {
            image=(Bitmap)source.Clone();TopMost=true;ShowInTaskbar=false;FormBorderStyle=FormBorderStyle.SizableToolWindow;
            Text="映序贴图 · 拖动移动 · Esc 关闭";AccessibleName="置顶截图";StartPosition=FormStartPosition.Manual;
            DoubleBuffered=true;KeyPreview=true;BackColor=Color.FromArgb(32,34,36);MinimumSize=new Size(96,72);
            double scale=Math.Min(1,Math.Min(Math.Min(800,Math.Max(96,desktop.Width-40))/(double)source.Width,Math.Min(600,Math.Max(72,desktop.Height-80))/(double)source.Height));
            ClientSize=new Size(Math.Max(64,(int)(source.Width*scale)),Math.Max(40,(int)(source.Height*scale)));
            Location=new Point(Math.Max(desktop.Left,Math.Min(location.X,desktop.Right-Width)),Math.Max(desktop.Top,Math.Min(location.Y,desktop.Bottom-Height)));
            Cursor=Cursors.SizeAll;ResizeRedraw=true;
        }
        internal static void Open(Bitmap source,Point location,Rectangle desktop)
        {
            if(pins.Count>=8)throw new InvalidOperationException("最多同时置顶 8 张截图，请先关闭不需要的贴图。");
            var pin=new CapturePinWindow(source,location,desktop);
            try {pins.Add(pin);pin.Show();}
            catch {pin.Dispose();throw;}
        }
        internal static void CloseAll() { foreach(var pin in pins.ToArray()) {pin.Close();pin.Dispose();} }
        protected override void OnPaint(PaintEventArgs e)
        {
            base.OnPaint(e);if(image==null)return;
            double scale=Math.Min(ClientSize.Width/(double)image.Width,ClientSize.Height/(double)image.Height);
            var size=new Size(Math.Max(1,(int)(image.Width*scale)),Math.Max(1,(int)(image.Height*scale)));
            e.Graphics.DrawImage(image,new Rectangle((ClientSize.Width-size.Width)/2,(ClientSize.Height-size.Height)/2,size.Width,size.Height));
        }
        protected override void OnMouseDown(MouseEventArgs e)
        {
            base.OnMouseDown(e);if(e.Button!=MouseButtons.Left)return;
            dragOrigin=PointToScreen(e.Location);windowOrigin=Location;dragging=true;Capture=true;
        }
        protected override void OnMouseMove(MouseEventArgs e)
        {
            base.OnMouseMove(e);if(!dragging)return;Point current=PointToScreen(e.Location);
            Location=new Point(windowOrigin.X+current.X-dragOrigin.X,windowOrigin.Y+current.Y-dragOrigin.Y);
        }
        protected override void OnMouseUp(MouseEventArgs e) {base.OnMouseUp(e);if(e.Button==MouseButtons.Left){dragging=false;Capture=false;}}
        protected override void OnMouseCaptureChanged(EventArgs e) {if(!Capture)dragging=false;base.OnMouseCaptureChanged(e);}
        protected override bool ProcessCmdKey(ref Message message,Keys keyData) {if(keyData==Keys.Escape){Close();return true;}return base.ProcessCmdKey(ref message,keyData);}
        protected override void Dispose(bool disposing)
        {
            if(disposing){pins.Remove(this);if(image!=null){image.Dispose();image=null;}}
            base.Dispose(disposing);
        }
    }
    internal static class CaptureVisuals
    {
        internal static readonly Color Ink=Color.FromArgb(35,38,42);
        internal static GraphicsPath Rounded(RectangleF bounds,float radius)
        {
            float d=Math.Min(radius*2,Math.Min(bounds.Width,bounds.Height));var path=new GraphicsPath();
            path.AddArc(bounds.Left,bounds.Top,d,d,180,90);path.AddArc(bounds.Right-d,bounds.Top,d,d,270,90);
            path.AddArc(bounds.Right-d,bounds.Bottom-d,d,d,0,90);path.AddArc(bounds.Left,bounds.Bottom-d,d,d,90,90);path.CloseFigure();return path;
        }
    }
    internal sealed class CaptureToolbar : FlowLayoutPanel
    {
        internal float UiScale=1;
        internal CaptureToolbar()
        {
            SetStyle(ControlStyles.UserPaint|ControlStyles.AllPaintingInWmPaint|ControlStyles.OptimizedDoubleBuffer|ControlStyles.SupportsTransparentBackColor,true);
            BackColor=Color.Transparent;Padding=new Padding(16,14,16,16);
        }
        protected override void OnPaintBackground(PaintEventArgs e)
        {
            base.OnPaintBackground(e);e.Graphics.SmoothingMode=SmoothingMode.AntiAlias;
            float scale=UiScale;
            // Inset the complete shadow; no clipped square edge at the panel boundary.
            for(int i=5;i>=1;i--)
                using(var shadow=CaptureVisuals.Rounded(new RectangleF((7-i)*scale,(8-i)*scale,Width-(14-i*2)*scale,Height-(17-i*2)*scale),15*scale))
                using(var shade=new SolidBrush(Color.FromArgb(4,22,26,32)))e.Graphics.FillPath(shade,shadow);
            using(var panel=CaptureVisuals.Rounded(new RectangleF(6*scale,4*scale,Width-12*scale,Height-14*scale),13*scale))
            using(var fill=new SolidBrush(Color.FromArgb(253,253,254)))using(var edge=new Pen(Color.FromArgb(220,223,229),scale))
            {e.Graphics.FillPath(fill,panel);e.Graphics.DrawPath(edge,panel);}
            // Preserve grouping when narrow monitors wrap the toolbar onto multiple rows.
            for(int first=0;first<Math.Min(4,Controls.Count);)
            {
                int last=first;while(last+1<4&&last+1<Controls.Count&&Controls[last+1].Top==Controls[first].Top)last++;
                Rectangle area=Rectangle.Union(Controls[first].Bounds,Controls[last].Bounds);area.Inflate((int)(3*scale),(int)(3*scale));
                using(var group=CaptureVisuals.Rounded(area,10*scale))using(var fill=new SolidBrush(Color.FromArgb(241,243,246)))e.Graphics.FillPath(fill,group);
                first=last+1;
            }
            using(var pen=new Pen(Color.FromArgb(229,231,235),scale))foreach(Control item in Controls)
                if(item.Margin.Left>=12*scale&&item.Left>Padding.Left+12*scale)e.Graphics.DrawLine(pen,item.Left-8*scale,item.Top+11*scale,item.Left-8*scale,item.Bottom-11*scale);
        }
    }
    internal sealed class CaptureToolButton : Button
    {
        internal string Icon;
        internal Color? Swatch;
        internal bool Chosen;
        internal bool Primary;
        internal float StrokeWidth;
        internal bool WidthMenuIndicator;
        internal float UiScale=1;
        private bool hover;
        private bool pressed;
        internal CaptureToolButton()
        {
            SetStyle(ControlStyles.UserPaint|ControlStyles.AllPaintingInWmPaint|ControlStyles.OptimizedDoubleBuffer|ControlStyles.SupportsTransparentBackColor,true);
            BackColor=Color.Transparent;UseVisualStyleBackColor=false;FlatStyle=FlatStyle.Flat;FlatAppearance.BorderSize=0;Size=new Size(40,40);Margin=new Padding(2);
            AccessibleRole=AccessibleRole.PushButton;Cursor=Cursors.Hand;
            // ButtonBase is opaque by default. Our partial, transparent painting
            // needs the parent background on every WM_PAINT before drawing icons;
            // otherwise the reused back buffer retains another button's pixels.
            SetStyle(ControlStyles.Opaque,false);
        }
        protected override void OnMouseEnter(EventArgs e) {hover=true;Invalidate();base.OnMouseEnter(e);}
        protected override void OnMouseLeave(EventArgs e) {hover=false;Invalidate();base.OnMouseLeave(e);}
        protected override void OnMouseDown(MouseEventArgs e) {if(e.Button==MouseButtons.Left){pressed=true;Invalidate();}base.OnMouseDown(e);}
        protected override void OnMouseUp(MouseEventArgs e) {pressed=false;Invalidate();base.OnMouseUp(e);}
        protected override void OnMouseCaptureChanged(EventArgs e) {if(!Capture){pressed=false;Invalidate();}base.OnMouseCaptureChanged(e);}
        protected override void OnGotFocus(EventArgs e) {Invalidate();base.OnGotFocus(e);}
        protected override void OnLostFocus(EventArgs e) {Invalidate();base.OnLostFocus(e);}
        protected override void OnPaint(PaintEventArgs e)
        {
            e.Graphics.SmoothingMode=SmoothingMode.AntiAlias;var original=e.Graphics.Save();e.Graphics.ScaleTransform(UiScale,UiScale);
            float width=Width/UiScale,height=Height/UiScale;
            bool solid=Primary||(Chosen&&!Swatch.HasValue);
            if(solid||hover||pressed)
                using(var shape=CaptureVisuals.Rounded(new RectangleF(1,1,width-3,height-3),9))
                using(var fill=new SolidBrush(solid?(pressed?Color.FromArgb(11,14,18):hover?Color.FromArgb(56,61,68):CaptureVisuals.Ink):pressed?Color.FromArgb(221,225,231):Color.FromArgb(232,235,240)))e.Graphics.FillPath(fill,shape);
            if(Swatch.HasValue)
            {
                var circle=new RectangleF((width-17)/2f,(height-17)/2f,17,17);
                using(var fill=new SolidBrush(Swatch.Value))e.Graphics.FillEllipse(fill,circle);
                using(var edge=new Pen(Color.FromArgb(35,0,0,0)))e.Graphics.DrawEllipse(edge,circle);
                if(Chosen)
                {
                    using(var ring=new Pen(CaptureVisuals.Ink,1.5f))e.Graphics.DrawEllipse(ring,circle.X-3,circle.Y-3,circle.Width+6,circle.Height+6);
                    using(var mark=new Pen(Swatch.Value.GetBrightness()>.65f?CaptureVisuals.Ink:Color.White,1.5f))
                    {mark.StartCap=LineCap.Round;mark.EndCap=LineCap.Round;e.Graphics.DrawLines(mark,new[]{new PointF(width/2-3,height/2),new PointF(width/2-1,height/2+2),new PointF(width/2+3,height/2-2)});}
                }
            }
            else if(Icon=="width")
            {
                Color ink=!Enabled?Color.Gray:solid?Color.White:CaptureVisuals.Ink;
                float center=WidthMenuIndicator?20:width/2;
                using(var pen=new Pen(ink,Math.Max(1.5f,Math.Min(StrokeWidth,12)))){pen.StartCap=LineCap.Round;pen.EndCap=LineCap.Round;e.Graphics.DrawLine(pen,center-8,height/2,center+8,height/2);}
                if(WidthMenuIndicator)using(var pen=new Pen(ink,1.4f))e.Graphics.DrawLines(pen,new[]{new PointF(38,height/2-2),new PointF(41,height/2+1),new PointF(44,height/2-2)});
            }
            else
            {
                var saved=e.Graphics.Save();e.Graphics.TranslateTransform((width-34)/2f,(height-32)/2f);
                CaptureSelector.DrawIcon(e.Graphics,Icon,!Enabled?Color.FromArgb(178,183,190):solid?Color.White:CaptureVisuals.Ink);e.Graphics.Restore(saved);
            }
            if(Focused&&ShowFocusCues)using(var outline=CaptureVisuals.Rounded(new RectangleF(2,2,width-5,height-5),7))using(var pen=new Pen(Color.FromArgb(111,119,131))){pen.DashStyle=DashStyle.Dot;e.Graphics.DrawPath(pen,outline);}
            e.Graphics.Restore(original);
        }
    }
    internal sealed class CaptureSelector : Form
    {
        private readonly Bitmap screen;
        private readonly bool quick;
        private readonly CaptureToolbar toolbar;
        private readonly CaptureToolbar widthPicker;
        private readonly CaptureToolButton widthButton;
        private readonly Button undo;
        private readonly ToolTip tips=new ToolTip();
        private readonly List<Button> toolButtons=new List<Button>();
        private readonly List<Button> colorButtons=new List<Button>();
        private readonly List<CaptureStroke> strokes=new List<CaptureStroke>();
        private Bitmap committed;
        private CaptureStroke pending;
        private Point first;
        private bool selecting;
        private bool shiftHeld;
        private readonly float uiScale;
        internal bool PinRequested { get; private set; }
        internal bool Annotating { get; private set; }
        internal string DrawingTool = "pen";
        internal Color DrawingColor = Color.FromArgb(238,74,79);
        internal float DrawingWidth = 4;
        internal int StrokeCount { get { return strokes.Count; } }
        internal Rectangle Area { get; private set; }
        internal CaptureSelector(Bitmap image,Rectangle bounds,string mode = "annotate",float scale=1)
        {
            screen=image; quick=mode=="quick"; AutoScaleMode=AutoScaleMode.None; FormBorderStyle=FormBorderStyle.None;
            StartPosition=FormStartPosition.Manual; Bounds=bounds; TopMost=true; ShowInTaskbar=false;
            // WinForms can clamp Bounds to the host's maximum window size, even
            // before a handle exists. Lay controls out for the actual viewport;
            // the frozen bitmap and selected output retain their original pixels.
            int layoutWidth=Math.Max(1,ClientSize.Width),layoutHeight=Math.Max(1,ClientSize.Height);
            uiScale=Math.Max(1,Math.Min(Math.Min(2.5f,scale),Math.Min(layoutWidth/320f,layoutHeight/240f)));
            DoubleBuffered=true; KeyPreview=true; Cursor=Cursors.Cross; Text="映序截图 · 拖动选择区域，Esc 取消";
            toolbar=new CaptureToolbar { UiScale=uiScale,Visible=false,Size=new Size(Math.Min(660,layoutWidth),70),WrapContents=true,AutoSize=true,AutoSizeMode=AutoSizeMode.GrowAndShrink,MaximumSize=new Size(layoutWidth,0),Cursor=Cursors.Default };
            string[] names={"画笔（按住 Shift 画直线）","矩形","箭头","马赛克（拖动框选区域）"};
            string[] tools={"pen","rectangle","arrow","mosaic"};
            for(int i=0;i<tools.Length;i++)
            {
                string id=tools[i];var button=IconButton(id,names[i]);button.Tag=id;toolButtons.Add(button);
                button.Click+=(s,e)=>{DrawingTool=id;RefreshChoices();};toolbar.Controls.Add(button);
            }
            Color[] palette={Color.FromArgb(238,74,79),Color.FromArgb(242,184,65),Color.FromArgb(71,164,110),Color.FromArgb(69,132,221),CaptureVisuals.Ink,Color.White};
            string[] colorNames={"红色","黄色","绿色","蓝色","黑色","白色"};
            for(int i=0;i<palette.Length;i++)
            {
                Color color=palette[i];var button=new CaptureToolButton {Width=28,AccessibleName=colorNames[i],Tag=color,Swatch=color};
                if(i==0)button.Margin=new Padding(14,2,2,2);tips.SetToolTip(button,colorNames[i]);colorButtons.Add(button);
                button.Click+=(s,e)=>{DrawingColor=color;RefreshChoices();};toolbar.Controls.Add(button);
            }
            widthButton=new CaptureToolButton {Width=52,Icon="width",WidthMenuIndicator=true,AccessibleName="画笔粗细：4 像素，展开选项",StrokeWidth=DrawingWidth,Margin=new Padding(14,2,2,2)};
            tips.SetToolTip(widthButton,widthButton.AccessibleName);widthButton.Click+=(s,e)=>{if(widthPicker.Visible)HideWidthPicker();else ShowWidthPicker();};
            widthPicker=new CaptureToolbar {UiScale=uiScale,Visible=false,AutoSize=true,AutoSizeMode=AutoSizeMode.GrowAndShrink,MaximumSize=new Size(layoutWidth,0),Cursor=Cursors.Default,AccessibleName="选择画笔粗细"};
            foreach(int value in new[]{2,4,8,12})
            {
                int pixels=value;var choice=new CaptureToolButton {Icon="width",StrokeWidth=pixels,AccessibleName=pixels+" 像素",Tag=pixels};
                tips.SetToolTip(choice,pixels+" 像素");
                choice.Click+=(s,e)=>{DrawingWidth=pixels;widthButton.StrokeWidth=pixels;widthButton.AccessibleName="画笔粗细："+pixels+" 像素，展开选项";tips.SetToolTip(widthButton,widthButton.AccessibleName);HideWidthPicker();};
                widthPicker.Controls.Add(choice);
            }
            undo=IconButton("undo","撤销（Ctrl+Z）");undo.Enabled=false;undo.Click+=(s,e)=>UndoStroke();
            undo.Margin=new Padding(14,2,2,2);
            var pin=IconButton("pin","确认并置顶到桌面（同时复制并保存到项目）");pin.Click+=(s,e)=>Confirm(true);
            var confirm=IconButton("confirm","确认（复制并保存到项目）");((CaptureToolButton)confirm).Primary=true;confirm.Click+=(s,e)=>Confirm();
            var cancel=IconButton("cancel","取消（Esc）");cancel.Click+=(s,e)=>CancelCapture();
            toolbar.Controls.AddRange(new Control[]{widthButton,undo,pin,confirm,cancel});
            foreach(Button button in toolButtons)button.Click+=(s,e)=>HideWidthPicker(false);
            foreach(Button button in colorButtons)button.Click+=(s,e)=>HideWidthPicker(false);
            undo.Click+=(s,e)=>HideWidthPicker(false);
            if(layoutWidth/uiScale<680)
            {
                // A compact density avoids an orphaned cancel button on small desktop displays.
                toolbar.Padding=new Padding(14,12,14,14);
                foreach(CaptureToolButton button in toolbar.Controls)
                {
                    Color? swatch=button.Swatch;button.Width=swatch.HasValue?24:button.Icon=="width"?48:36;button.Height=36;
                    button.Margin=new Padding(button.Margin.Left>=12?12:1,2,1,2);
                }
            }
            Controls.Add(toolbar);Controls.Add(widthPicker);RefreshChoices();
            if(uiScale!=1)
            {
                toolbar.MaximumSize=Size.Empty;toolbar.Scale(new SizeF(uiScale,uiScale));toolbar.MaximumSize=new Size(layoutWidth,0);
                foreach(CaptureToolButton button in toolbar.Controls)button.UiScale=uiScale;
                widthPicker.MaximumSize=Size.Empty;widthPicker.Scale(new SizeF(uiScale,uiScale));widthPicker.MaximumSize=new Size(layoutWidth,0);
                foreach(CaptureToolButton button in widthPicker.Controls)button.UiScale=uiScale;
            }
        }
        private void ShowWidthPicker()
        {
            widthPicker.PerformLayout();int edge=(int)(8*uiScale);
            int left=toolbar.Left+widthButton.Left+widthButton.Width-widthPicker.Width;
            int top=toolbar.Top-widthPicker.Height-edge;
            if(top<edge)top=toolbar.Bottom+edge;
            widthPicker.Location=new Point(Math.Max(0,Math.Min(left,ClientSize.Width-widthPicker.Width)),Math.Max(0,Math.Min(top,ClientSize.Height-widthPicker.Height)));
            CaptureToolButton selected=null;
            foreach(CaptureToolButton choice in widthPicker.Controls){choice.Chosen=choice.StrokeWidth==DrawingWidth;choice.Invalidate();if(choice.Chosen)selected=choice;}
            widthButton.Chosen=true;widthButton.Invalidate();widthPicker.Show();widthPicker.BringToFront();
            if(selected!=null)selected.Focus();
        }
        private void HideWidthPicker(bool restoreFocus=true)
        {
            bool visible=widthPicker.Visible;widthPicker.Hide();widthButton.Chosen=false;widthButton.Invalidate();
            if(visible&&restoreFocus)widthButton.Focus();
        }
        private Button IconButton(string icon,string name)
        {
            var button=new CaptureToolButton {AccessibleName=name,Icon=icon};tips.SetToolTip(button,name);return button;
        }
        internal static void DrawIcon(Graphics graphics,string icon,Color color)
        {
            graphics.SmoothingMode=SmoothingMode.AntiAlias;
            using(var pen=new Pen(color,1.7f))
            {
                pen.StartCap=LineCap.Round;pen.EndCap=LineCap.Round;
                if(icon=="rectangle")using(var rectangle=CaptureVisuals.Rounded(new RectangleF(8,8,17,15),2))graphics.DrawPath(pen,rectangle);
                else if(icon=="arrow") {graphics.DrawLine(pen,8,24,25,7);graphics.DrawLines(pen,new[]{new Point(16,7),new Point(25,7),new Point(25,16)});}
                else if(icon=="pen") {graphics.DrawLine(pen,10,23,23,9);graphics.DrawLine(pen,8,25,12,24);graphics.DrawLine(pen,20,8,24,12);}
                else if(icon=="mosaic") {for(int y=0;y<3;y++)for(int x=0;x<3;x++)using(var brush=new SolidBrush((x+y)%2==0?color:Color.FromArgb(100,color)))graphics.FillRectangle(brush,8+x*6,7+y*6,4.5f,4.5f);}
                else if(icon=="confirm")graphics.DrawLines(pen,new[]{new Point(8,16),new Point(14,22),new Point(25,9)});
                else if(icon=="cancel") {graphics.DrawLine(pen,10,9,24,23);graphics.DrawLine(pen,24,9,10,23);}
                else if(icon=="undo") {graphics.DrawArc(pen,10,10,16,14,210,230);graphics.DrawLines(pen,new[]{new Point(8,9),new Point(8,16),new Point(15,16)});}
                else if(icon=="pin") {graphics.DrawRectangle(pen,12,7,10,5);graphics.DrawLines(pen,new[]{new Point(13,12),new Point(10,20),new Point(24,20),new Point(21,12)});graphics.DrawLine(pen,17,20,17,27);}
            }
        }
        private void RefreshChoices()
        {
            foreach(CaptureToolButton button in toolButtons){button.Chosen=(string)button.Tag==DrawingTool;button.Invalidate();}
            foreach(CaptureToolButton button in colorButtons){button.Chosen=((Color)button.Tag).ToArgb()==DrawingColor.ToArgb();button.Invalidate();}
        }
        private void ClearCommitted() {if(committed!=null){committed.Dispose();committed=null;}}
        internal void UndoStroke()
        {
            if(pending!=null){pending=null;Capture=false;}
            else if(strokes.Count>0){strokes.RemoveAt(strokes.Count-1);ClearCommitted();}
            undo.Enabled=strokes.Count>0;Invalidate();
        }
        internal void Confirm(bool pin=false) { if(!Annotating||pending!=null)return;PinRequested=pin;DialogResult=DialogResult.OK;Close(); }
        private void CancelCapture() { pending=null;PinRequested=false;DialogResult=DialogResult.Cancel;Close(); }
        internal Bitmap CreateResult()
        {
            if(DialogResult!=DialogResult.OK)return null;
            if(quick)return CapturePlatform.Crop(screen,Area);
            // Return independent ownership: a pin, upload and selector never share a disposable bitmap.
            return (Bitmap)CommittedImage().Clone();
        }
        private Bitmap CommittedImage()
        {
            if(committed!=null)return committed;
            committed=CapturePlatform.Crop(screen,Area);
            try {foreach(var stroke in strokes)RenderStroke(committed,stroke);return committed;}
            catch {ClearCommitted();throw;}
        }
        private void RenderStroke(Bitmap pixels,CaptureStroke stroke)
        {
            using(var graphics=Graphics.FromImage(pixels))
            {
                graphics.TranslateTransform(-Area.X,-Area.Y);graphics.SetClip(Area);graphics.SmoothingMode=SmoothingMode.AntiAlias;
                stroke.Draw(graphics,pixels,Area.Location);
            }
        }
        private Point Clamp(Point point) { return new Point(Math.Max(Area.Left,Math.Min(Area.Right-1,point.X)),Math.Max(Area.Top,Math.Min(Area.Bottom-1,point.Y))); }
        private void Extend(Point point)
        {
            point=Clamp(point);
            bool straight=pending.Tool=="pen" && (shiftHeld||(ModifierKeys&Keys.Shift)!=0);
            if(straight && pending.Points.Count>2)pending.Points.RemoveRange(1,pending.Points.Count-1);
            if((pending.Tool!="pen"||straight) && pending.Points.Count>1)pending.Points[1]=point;
            else if(pending.Points.Count<4096 && pending.Points[pending.Points.Count-1]!=point)pending.Points.Add(point);
        }
        protected override void OnPaint(PaintEventArgs e)
        {
            e.Graphics.DrawImageUnscaled(screen,0,0);
            if(Annotating)
            {
                Bitmap pixels=CommittedImage();e.Graphics.DrawImageUnscaled(pixels,Area.Location);
                if(pending!=null)
                {
                    var saved=e.Graphics.Save();
                    try {e.Graphics.SetClip(Area);e.Graphics.SmoothingMode=SmoothingMode.AntiAlias;pending.Draw(e.Graphics,pixels,Area.Location);}
                    finally {e.Graphics.Restore(saved);}
                }
            }
            using (var shade=new SolidBrush(Color.FromArgb(100,0,0,0)))
            using (var outside=new Region(ClientRectangle))
            {
                if (Area.Width>0 && Area.Height>0) outside.Exclude(Area);
                e.Graphics.FillRegion(shade,outside);
            }
            e.Graphics.SmoothingMode=SmoothingMode.AntiAlias;
            e.Graphics.TextRenderingHint=System.Drawing.Text.TextRenderingHint.AntiAliasGridFit;
            if (Area.Width>0 && Area.Height>0)
            {
                using(var pen=new Pen(Color.FromArgb(230,235,240),1))e.Graphics.DrawRectangle(pen,Area.X,Area.Y,Math.Max(0,Area.Width-1),Math.Max(0,Area.Height-1));
                foreach(var point in new[]{Area.Location,new Point(Area.Right-1,Area.Top),new Point(Area.Left,Area.Bottom-1),new Point(Area.Right-1,Area.Bottom-1)})
                    using(var fill=new SolidBrush(Color.White))using(var edge=new Pen(Color.FromArgb(101,109,122)))
                    {e.Graphics.FillRectangle(fill,point.X-3,point.Y-3,6,6);e.Graphics.DrawRectangle(edge,point.X-3,point.Y-3,6,6);}
                DrawBadge(e.Graphics,Area.Width+" × "+Area.Height+" px",new Point(Math.Max(8,Area.Left),Math.Max(8,Area.Top-(int)(36*uiScale))),false);
            }
            string hint=Annotating?"Shift 画直线    ·    Ctrl+Z 撤销    ·    Esc 取消":"拖动选择截图区域    ·    Esc 取消";
            if(ClientSize.Width<400*uiScale)hint=Annotating?"Shift 直线   ·   Esc 取消":"拖动截图   ·   Esc 取消";
            DrawBadge(e.Graphics,hint,new Point(8,(int)(20*uiScale)),true);
        }
        private void DrawBadge(Graphics graphics,string text,Point location,bool help)
        {
            using(var font=new Font("Microsoft YaHei UI",(help?12:11)*uiScale,FontStyle.Regular,GraphicsUnit.Pixel))
            {
                SizeF textSize=graphics.MeasureString(text,font);float width=Math.Min(ClientSize.Width-16,textSize.Width+28*uiScale),height=(help?38:28)*uiScale;
                if(help)location.X=(int)((ClientSize.Width-width)/2);
                else location.X=Math.Max(8,Math.Min(location.X,(int)(ClientSize.Width-width-8)));
                using(var box=CaptureVisuals.Rounded(new RectangleF(location.X,location.Y,width,height),(help?11:7)*uiScale))
                using(var fill=new SolidBrush(Color.FromArgb(237,35,38,42)))graphics.FillPath(fill,box);
                using(var ink=new SolidBrush(Color.FromArgb(238,240,243)))graphics.DrawString(text,font,ink,location.X+14*uiScale,location.Y+(height-textSize.Height)/2);
            }
        }
        protected override void OnMouseDown(MouseEventArgs e)
        {
            if(widthPicker.Visible){HideWidthPicker();return;}
            if (e.Button==MouseButtons.Right) { CancelCapture(); return; }
            if (e.Button!=MouseButtons.Left) return;
            if(Annotating)
            {
                if(!Area.Contains(e.Location)||strokes.Count>=200)return;
                pending=new CaptureStroke {Tool=DrawingTool,Color=DrawingColor,Width=DrawingWidth};pending.Points.Add(e.Location);Capture=true;Invalidate();return;
            }
            first=e.Location; selecting=true; Capture=true; Area=Rectangle.Empty; Invalidate();
        }
        protected override void OnMouseMove(MouseEventArgs e)
        {
            if(pending!=null){Extend(e.Location);Invalidate();return;}
            if (!selecting) return; Area=CapturePlatform.Selection(first,e.Location,screen.Size); Invalidate();
        }
        protected override void OnMouseUp(MouseEventArgs e)
        {
            if(e.Button!=MouseButtons.Left)return;
            if(pending!=null){Extend(e.Location);RenderStroke(CommittedImage(),pending);strokes.Add(pending);pending=null;Capture=false;undo.Enabled=true;Invalidate();return;}
            if (!selecting) return;
            Area=CapturePlatform.Selection(first,e.Location,screen.Size); selecting=false; Capture=false;
            if (Area.Width<2 || Area.Height<2) { Area=Rectangle.Empty; Invalidate(); return; }
            if(quick){DialogResult=DialogResult.OK;Close();return;}
            Annotating=true;
            toolbar.PerformLayout();
            int gap=(int)(8*uiScale),edge=(int)(8*uiScale);
            int top=Area.Bottom+gap;
            if(top+toolbar.Height+edge>ClientSize.Height)top=Area.Top-toolbar.Height-gap;
            if(top<edge)top=Math.Max(0,ClientSize.Height-toolbar.Height-edge);
            int left=Area.Right-toolbar.Width;
            toolbar.Location=new Point(Math.Max(0,Math.Min(Math.Max(edge,left),ClientSize.Width-toolbar.Width-edge)),Math.Max(0,Math.Min(top,ClientSize.Height-toolbar.Height)));
            toolbar.Visible=true;toolbar.BringToFront();Invalidate();
        }
        protected override void OnKeyDown(KeyEventArgs e)
        {
            if(e.KeyCode==Keys.ShiftKey){shiftHeld=true;if(pending!=null&&pending.Tool=="pen"){Extend(pending.Points[pending.Points.Count-1]);Invalidate();}}
            else if (e.KeyCode==Keys.Escape) { e.Handled=true;if(widthPicker.Visible)HideWidthPicker();else CancelCapture(); }
            else if(e.Control&&e.KeyCode==Keys.Z&&Annotating){e.Handled=true;UndoStroke();}
            base.OnKeyDown(e);
        }
        protected override void OnKeyUp(KeyEventArgs e) {if(e.KeyCode==Keys.ShiftKey)shiftHeld=false;base.OnKeyUp(e);}
        protected override void OnDeactivate(EventArgs e) {shiftHeld=false;base.OnDeactivate(e);}
        protected override void OnMouseCaptureChanged(EventArgs e)
        {
            if(!Capture && pending!=null){pending=null;Invalidate();}
            if(!Capture && selecting){selecting=false;Area=Rectangle.Empty;Invalidate();}
            base.OnMouseCaptureChanged(e);
        }
        protected override bool ProcessCmdKey(ref Message message,Keys keyData)
        {
            // Dismiss the width choices first; a second Escape cancels the capture.
            if(keyData==Keys.Escape){if(widthPicker.Visible)HideWidthPicker();else CancelCapture();return true;}
            if(keyData==(Keys.Control|Keys.Z)&&Annotating){UndoStroke();return true;}
            return base.ProcessCmdKey(ref message,keyData);
        }
        protected override void Dispose(bool disposing)
        {
            if(disposing){ClearCommitted();tips.Dispose();}
            base.Dispose(disposing);
        }
    }
}
