using System;
using System.Drawing;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace YingXu.Desktop
{
    // One custom caption row replaces WS_CAPTION; WS_THICKFRAME keeps the native
    // resize/non-client machinery. Document tools are separate from this caption.
    internal class QuickWindowChrome : Form
    {
        private readonly Panel caption;
        private readonly Label captionText;
        private readonly Button minimize, maximize, close;
        internal readonly Button StudioButton;
        private bool fullScreen;
        private Rectangle savedBounds;
        private FormWindowState savedState;
        private Size savedMinimum;
        internal bool IsPlayerFullScreen { get { return fullScreen; } }
        [DllImport("user32.dll")] private static extern bool ReleaseCapture();
        [DllImport("user32.dll")] private static extern IntPtr SendMessage(IntPtr hwnd,int msg,IntPtr wparam,IntPtr lparam);

        protected override CreateParams CreateParams
        {
            get { var value=base.CreateParams; if(!fullScreen)value.Style &= ~0x00C00000; return value; }
        }

        protected QuickWindowChrome()
        {
            AutoScaleMode=AutoScaleMode.Dpi;
            FormBorderStyle=FormBorderStyle.Sizable;
            caption=new Panel { Dock=DockStyle.Top,Height=38,BackColor=Color.FromArgb(246,247,249) };
            captionText=new Label { AutoEllipsis=true,TextAlign=ContentAlignment.MiddleLeft,ForeColor=Color.FromArgb(40,42,47) };
            StudioButton=CaptionButton("在映序中打开 ↗","在映序主工作台中打开当前文件");
            minimize=CaptionButton("−","最小化");maximize=CaptionButton("□","最大化 / 还原");close=CaptionButton("×","关闭");
            caption.Controls.AddRange(new Control[]{captionText,StudioButton,minimize,maximize,close});
            Controls.Add(caption);
            minimize.Click+=delegate { WindowState=FormWindowState.Minimized; };
            maximize.Click+=delegate { ToggleMaximize(); };
            close.Click+=delegate { Close(); };
            MouseEventHandler drag=delegate(object sender,MouseEventArgs e) {
                if(e.Button!=MouseButtons.Left || fullScreen)return;
                if(e.Clicks==2){ToggleMaximize();return;}
                ReleaseCapture();SendMessage(Handle,0x00A1,new IntPtr(2),IntPtr.Zero);
            };
            caption.MouseDown+=drag;captionText.MouseDown+=drag;
            caption.Resize+=delegate { LayoutCaption(); };
            TextChanged+=delegate { captionText.Text=Text; };
            Resize+=delegate { maximize.Text=WindowState==FormWindowState.Maximized?"❐":"□"; };
            DpiChanged+=delegate { LayoutCaption(); };
            LayoutCaption();
        }

        private Button CaptionButton(string text,string accessible)
        {
            var button=new Button { Text=text,AccessibleName=accessible,FlatStyle=FlatStyle.Flat,TabStop=true,UseVisualStyleBackColor=false,BackColor=Color.FromArgb(246,247,249),ForeColor=Color.FromArgb(40,42,47) };
            button.FlatAppearance.BorderSize=0;button.FlatAppearance.MouseOverBackColor=Color.FromArgb(225,228,233);
            return button;
        }
        private void LayoutCaption()
        {
            if(caption==null)return;
            int scale=Math.Max(96,DeviceDpi),h=38*scale/96,w=46*scale/96,entry=166*scale/96,pad=12*scale/96;
            caption.Height=h;
            close.SetBounds(caption.Width-w,0,w,h);maximize.SetBounds(caption.Width-2*w,0,w,h);minimize.SetBounds(caption.Width-3*w,0,w,h);
            StudioButton.SetBounds(caption.Width-3*w-entry,0,entry,h);
            captionText.SetBounds(pad,0,Math.Max(0,StudioButton.Left-2*pad),h);
        }
        protected void FinishCaptionLayout() { caption.SendToBack(); LayoutCaption(); }
        private void ToggleMaximize() { if(!fullScreen)WindowState=WindowState==FormWindowState.Maximized?FormWindowState.Normal:FormWindowState.Maximized; }
        internal void SetPlayerFullScreen(bool value)
        {
            if(fullScreen==value || IsDisposed)return;
            SuspendLayout();
            if(value)
            {
                savedState=WindowState; savedBounds=WindowState==FormWindowState.Normal?Bounds:RestoreBounds;
                var monitor=Screen.FromHandle(Handle).Bounds;
                savedMinimum=MinimumSize;MinimumSize=Size.Empty;
                fullScreen=true;WindowState=FormWindowState.Normal;caption.Hide();FormBorderStyle=FormBorderStyle.None;Bounds=monitor;
            }
            else
            {
                fullScreen=false;WindowState=FormWindowState.Normal;FormBorderStyle=FormBorderStyle.Sizable;MinimumSize=savedMinimum;caption.Show();Bounds=savedBounds;
                WindowState=savedState==FormWindowState.Maximized?FormWindowState.Maximized:FormWindowState.Normal;
            }
            ResumeLayout(true);
        }
    }
}
