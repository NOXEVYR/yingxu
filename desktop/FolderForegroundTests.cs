using System;
using System.Collections.Generic;

namespace YingXu.Desktop
{
    internal static class FolderForegroundTests
    {
        private sealed class Fake : IFolderFocus
        {
            internal int Tick,Activations,Restores,Finds;
            internal bool Minimized,Appear=true,ActivateWorks=true;
            internal Func<int,FolderFocusState> State=t=>new FolderFocusState { Handle=new IntPtr(10),Process=1,Input=5 };
            public FolderFocusState ReadState() { return State(Tick); }
            public IEnumerable<IntPtr> Find(string path) { Finds++;return Appear ? new[] { new IntPtr(20) } : new IntPtr[0]; }
            public bool IsMinimized(IntPtr handle) { return Minimized; }
            public void Restore(IntPtr handle) { Restores++;Minimized=false; }
            public void Activate(IntPtr handle) { Activations++;if(ActivateWorks)State=t=>new FolderFocusState { Handle=new IntPtr(20),Process=2,Input=5,Explorer=true }; }
            public void Wait() { Tick++; }
        }
        private static int passed;
        private static void Check(bool condition,string name) { if(!condition)throw new Exception(name);passed++;Console.WriteLine("PASS "+name); }
        internal static void Main()
        {
            var f=new Fake();FolderForeground.Run("synthetic",1,5,f,()=>false);
            Check(f.Activations==1&&f.Tick==1,"activation verified on later pass");
            f=new Fake { Minimized=true };FolderForeground.Run("synthetic",1,5,f,()=>false);
            Check(f.Restores==1&&f.Activations==1,"restore minimized matching window");
            f=new Fake { State=t=>new FolderFocusState { Handle=new IntPtr(15),Process=2,Input=5,Explorer=true } };FolderForeground.Run("synthetic",1,5,f,()=>false);
            Check(f.Activations==1,"Explorer foreground during navigation is allowed");
            f=new Fake { State=t=>new FolderFocusState { Handle=new IntPtr(15),Process=2,Input=6,Explorer=true } };FolderForeground.Run("synthetic",1,5,f,()=>false);
            Check(f.Activations==0&&f.Finds==0,"new user action in Explorer prevents delayed focus");
            f=new Fake { State=t=>new FolderFocusState { Handle=new IntPtr(30),Process=3,Input=5 } };FolderForeground.Run("synthetic",1,5,f,()=>false);
            Check(f.Activations==0&&f.Finds==0,"other application foreground never stolen");
            f=new Fake { Appear=false };FolderForeground.Run("synthetic",1,5,f,()=>false);
            Check(f.Tick==40&&f.Activations==0,"window lookup retry budget");
            f=new Fake { ActivateWorks=false };FolderForeground.Run("synthetic",1,5,f,()=>false);
            Check(f.Activations==40,"failed activation bounded and rechecked");
            f=new Fake();FolderForeground.Run("synthetic",1,5,f,()=>true);
            Check(f.Finds==0,"closed or superseded request stops before shell lookup");
            f=new Fake { State=t=>new FolderFocusState { Handle=new IntPtr(20),Process=2,Input=5,Explorer=true } };FolderForeground.Run("synthetic",1,5,f,()=>false);
            Check(f.Activations==0,"already foreground target no redundant activation");
            Check(FolderForeground.Normalize(@"C:\synthetic\folder\")==FolderForeground.Normalize(@"C:\synthetic\folder"),"matching paths ignore trailing separators");
            Console.WriteLine("Folder foreground synthetic checks: "+passed);
        }
    }
}
