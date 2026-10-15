using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace YingXu.Desktop
{
    // End-to-end verifier for an extracted release root. The backend is started
    // and stopped by tools/verify_release_native.py with the bundle's Python.
    internal static class VideoFrameTests
    {
        private static readonly JavaScriptSerializer Json = new JavaScriptSerializer();
        private static readonly List<string> Passed = new List<string>();
        private static string evidence;
        private static int result = 1;

        private static void Check(bool condition, string name)
        {
            if (!condition) throw new InvalidOperationException("FAILED: " + name);
            Passed.Add(name);
            Console.WriteLine("PASS " + name);
        }

        private static object Field(object instance, string name)
        {
            var field = instance.GetType().GetField(name, BindingFlags.Instance | BindingFlags.NonPublic);
            if (field == null) throw new MissingFieldException(instance.GetType().FullName, name);
            return field.GetValue(instance);
        }

        private static async Task<string> Script(CoreWebView2 web, string source)
        {
            return await web.ExecuteScriptAsync(source);
        }

        private static async Task WaitFor(CoreWebView2 web, string expression, string name, int seconds)
        {
            DateTime until = DateTime.UtcNow.AddSeconds(seconds);
            string last = "";
            while (DateTime.UtcNow < until)
            {
                last = await Script(web, expression);
                if (last == "true") return;
                await Task.Delay(100);
            }
            throw new TimeoutException("Timed out waiting for " + name + "; last DOM result=" + last);
        }

        private static async Task Click(CoreWebView2 web, string selector)
        {
            string encoded = Json.Serialize(selector);
            string raw = await Script(web,
                "JSON.stringify((()=>{const n=document.querySelector(" + encoded + ");if(!n)throw Error('missing '+" + encoded + ");n.scrollIntoView({block:'center',inline:'center'});const r=n.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2}})())");
            var point = Json.Deserialize<Dictionary<string, object>>(Json.Deserialize<string>(raw));
            double x = Convert.ToDouble(point["x"]), y = Convert.ToDouble(point["y"]);
            await web.CallDevToolsProtocolMethodAsync("Input.dispatchMouseEvent",
                Json.Serialize(new { type = "mousePressed", x = x, y = y, button = "left", buttons = 1, clickCount = 1 }));
            await web.CallDevToolsProtocolMethodAsync("Input.dispatchMouseEvent",
                Json.Serialize(new { type = "mouseReleased", x = x, y = y, button = "left", buttons = 0, clickCount = 1 }));
        }

        private static async Task<double[]> Bounds(CoreWebView2 web, string selector)
        {
            string encoded = Json.Serialize(selector);
            string raw = await Script(web,
                "JSON.stringify((()=>{const n=document.querySelector(" + encoded + ");if(!n)throw Error('missing '+" + encoded + ");n.scrollIntoView({block:'center',inline:'center'});const r=n.getBoundingClientRect();return [r.left,r.top,r.width,r.height]})())");
            return Json.Deserialize<double[]>(Json.Deserialize<string>(raw));
        }

        private static async Task Mouse(CoreWebView2 web, string type, double x, double y, string button, int buttons)
        {
            await web.CallDevToolsProtocolMethodAsync("Input.dispatchMouseEvent",
                Json.Serialize(new { type = type, x = x, y = y, button = button, buttons = buttons, clickCount = 1 }));
        }

        private static async Task Key(CoreWebView2 web, string key, string code, int virtualKey)
        {
            await web.CallDevToolsProtocolMethodAsync("Input.dispatchKeyEvent",
                Json.Serialize(new { type = "keyDown", key = key, code = code,
                    windowsVirtualKeyCode = virtualKey, nativeVirtualKeyCode = virtualKey }));
            await web.CallDevToolsProtocolMethodAsync("Input.dispatchKeyEvent",
                Json.Serialize(new { type = "keyUp", key = key, code = code,
                    windowsVirtualKeyCode = virtualKey, nativeVirtualKeyCode = virtualKey }));
        }

        private static async Task ReplaceTextTrusted(CoreWebView2 web, string selector, string value)
        {
            await Click(web, selector);
            await Script(web, "document.querySelector(" + Json.Serialize(selector) + ").select()");
            await web.CallDevToolsProtocolMethodAsync("Input.insertText", Json.Serialize(new { text = value }));
            await Key(web, "Tab", "Tab", 9);
        }

        private static async Task Capture(CoreWebView2 web, string fileName, string label)
        {
            string path = Path.Combine(evidence, fileName);
            using (var output = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                await web.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, output);
            Check(new FileInfo(path).Length > 2048, label + " captured at " + Path.GetFileName(path));
        }

        private static async Task WaitForToastsToClear(CoreWebView2 web, string label)
        {
            await WaitFor(web, "document.querySelectorAll('#toastRegion .toast').length===0", label, 8);
        }

        private static async Task<string> FindIndexSession(CoreWebView2 web)
        {
            string raw = await Script(web,
                "JSON.stringify(performance.getEntriesByType('resource').map(x=>x.name).filter(x=>x.includes('/api/video/frames/jobs/')))" );
            string encoded = Json.Deserialize<string>(raw);
            var names = Json.Deserialize<List<string>>(encoded);
            if (names != null) foreach (string name in names)
            {
                Uri uri;
                if (!Uri.TryCreate(name, UriKind.Absolute, out uri)) continue;
                string[] parts = uri.AbsolutePath.Split('/');
                if (parts.Length > 5 && parts[1] == "api" && parts[2] == "video" && parts[3] == "frames" &&
                    parts[4] == "jobs" && parts[5].Length == 32) return parts[5];
            }
            throw new InvalidDataException("The real workbench did not poll its frame-index session.");
        }

        private static async Task<string> FindVideoExportId(CoreWebView2 web)
        {
            string raw = await Script(web,
                "JSON.stringify(performance.getEntriesByType('resource').map(x=>x.name).filter(x=>x.includes('/api/video/exports/')))");
            string encoded = Json.Deserialize<string>(raw);
            var names = Json.Deserialize<List<string>>(encoded);
            if (names != null) foreach (string name in names)
            {
                Uri uri;
                if (!Uri.TryCreate(name, UriKind.Absolute, out uri)) continue;
                string[] parts = uri.AbsolutePath.Split('/');
                if (parts.Length >= 5 && parts[1] == "api" && parts[2] == "video" &&
                    parts[3] == "exports" && parts[4].Length == 32) return parts[4];
            }
            throw new InvalidDataException("The real workbench did not poll its rough-cut export job.");
        }

        private static async Task CheckNoHorizontalOverflow(CoreWebView2 web, string label)
        {
            Check(await Script(web,
                "(()=>{const w=document.querySelector('#workspace');return document.documentElement.scrollWidth<=window.innerWidth+1&&document.body.scrollWidth<=window.innerWidth+1&&w&&w.getBoundingClientRect().right<=window.innerWidth+1&&w.scrollWidth<=w.clientWidth+1})()") == "true",
                label + " has no horizontal page or workspace overflow");
        }

        private static async Task RunChecks(StudioWindow window, string videoPath, string dataRoot)
        {
            var web = (WebView2)Field(window, "web");
            var core = web.CoreWebView2;
            if (core == null) throw new InvalidOperationException("Studio WebView2 controller was not initialized.");
            Check(Hub.IsLocalPage(core.Source, Hub.Url), "StudioWindow navigates to the release-root local workbench");

            // Exercise the same desktop file-open bridge used by Open With and
            // Quick Reader handoff; the API registers the fixture in the real app.
            window.OpenFromReader(new[] { videoPath }, true, null);
            await WaitFor(core,
                "(()=>{const v=document.querySelector('#editorContent .video-player video');return !!v&&v.readyState>=2&&Number.isFinite(v.duration)})()",
                "real external-video route and WebView decode", 35);
            Check(await Script(core, "document.querySelector('#editorContent video').duration>20") == "true",
                "bundled WebView decodes the backend-registered synthetic H.264 fixture");
            await Click(core, "#editorContent [data-command=play]");
            await WaitFor(core, "(()=>{const v=document.querySelector('#editorContent video');return !v.paused&&v.currentTime>.15})()",
                "actual workbench video playback", 10);
            Check(await Script(core, "!!document.querySelector('#inspector .video-review')") == "true",
                "full workbench mounts its real review and frame controls");
            Check(await Script(core,
                "document.querySelector('#inspector .video-review')===document.querySelector('#editorContent .video-player .video-review')") == "false",
                "video tools occupy the workspace inspector instead of a button wall below the player");
            Check(await Script(core,
                "(()=>{const p=document.querySelector('#inspector .video-review'),t=document.querySelector('#editorContent .video-trim-timeline');return !!p&&!!t&&t.parentElement===document.querySelector('#editorContent .video-player')})()") == "true",
                "review tools mount in the inspector while the trim timeline stays under the player");
            await WaitFor(core, "document.querySelector('.review-status')?.textContent.includes('审片记录已载入')",
                "real backend review record loads", 15);
            Check(await Script(core,
                "(()=>{const b=document.querySelector('[data-review-tab=trim]'),p=document.querySelector('[data-review-panel=trim]');return b?.getAttribute('aria-selected')==='true'&&p&&!p.hidden})()") == "true",
                "inspector opens on the selected trim tool tab");

            await Click(core, "#inspector [data-review-tab=file]");
            await WaitFor(core, "document.querySelector('[data-review-tab=file]')?.getAttribute('aria-selected')==='true'",
                "file properties tab opens", 5);
            Check(await Script(core,
                "(()=>{const p=document.querySelector('[data-review-panel=file]');return !!p&&!p.hidden&&p.textContent.includes('synthetic-cfr-25fps.mp4')})()") == "true",
                "file tab retains the real synthetic video's properties");
            await Click(core, "#inspector [data-review-tab=markers]");
            await WaitFor(core, "document.querySelector('[data-review-tab=markers]')?.getAttribute('aria-selected')==='true'",
                "marker tool tab opens", 5);
            Check(await Script(core,
                "(()=>{const p=document.querySelector('#inspector [data-review-panel=markers]');return !!p&&!p.hidden&&!!p.querySelector('.video-marker-note')&&!!p.querySelector('.video-markers')})()") == "true",
                "marker tab exposes its editable note field and saved marker list");

            // A/B uses the actual review UI and player. Seek the native media
            // clock, then set the markers with CDP-driven button input.
            await Script(core, "document.querySelector('#editorContent video').currentTime=.45");
            await WaitFor(core, "Math.abs(document.querySelector('#editorContent video').currentTime-.45)<.1",
                "A marker seek", 8);
            await Click(core, "#inspector [data-review=a]");
            await Script(core, "document.querySelector('#editorContent video').currentTime=.9");
            await WaitFor(core, "Math.abs(document.querySelector('#editorContent video').currentTime-.9)<.1",
                "B marker seek", 8);
            await Click(core, "#inspector [data-review=b]");
            await Click(core, "#inspector [data-review=ab]");
            await WaitFor(core,
                "(()=>{const v=document.querySelector('#editorContent video');return document.querySelector('#inspector [data-review=ab]').getAttribute('aria-pressed')==='true'&&!v.paused&&v.currentTime<.7})()",
                "A/B controls loop the real synthetic video", 10);
            Check(true, "CDP clicks activate the workbench A/B loop");
            await Click(core, "#inspector [data-review=ab]");
            await WaitFor(core, "document.querySelector('#inspector [data-review=ab]').getAttribute('aria-pressed')==='false'",
                "stop the short loop before extending its B point", 5);
            await Script(core, "document.querySelector('#editorContent video').currentTime=7");
            await WaitFor(core, "Math.abs(document.querySelector('#editorContent video').currentTime-7)<.1",
                "position the B point beyond the trim preview end", 8);
            await Click(core, "#inspector [data-review=b]");
            await Click(core, "#inspector [data-review=ab]");
            await WaitFor(core, "document.querySelector('#inspector [data-review=ab]').getAttribute('aria-pressed')==='true'",
                "reactivate the A/B loop around the selected trim interval", 5);

            // A real editable trim range lives directly below the video. Set
            // its points through the same buttons and text input used by an
            // editor, then verify preview is confined to the selected range.
            await Click(core, "#inspector [data-review-tab=trim]");
            await Script(core, "(()=>{const v=document.querySelector('#editorContent video');v.pause();v.currentTime=.45;return true})()");
            await WaitFor(core, "(()=>{const v=document.querySelector('#editorContent video');return v.paused&&Math.abs(v.currentTime-.45)<.1&&document.querySelector('#inspector [data-review=ab]').getAttribute('aria-pressed')==='true'})()",
                "position playhead at the range start", 8);
            await Click(core, "#editorContent [data-review=in]");
            await Script(core, "document.querySelector('#editorContent video').currentTime=2.1");
            await WaitFor(core, "Math.abs(document.querySelector('#editorContent video').currentTime-2.1)<.1",
                "position playhead at the range end", 8);
            await Click(core, "#editorContent [data-review=out]");
            var outBounds = await Bounds(core, "#editorContent .trim-handle-out");
            double outY = outBounds[1] + outBounds[3] / 2;
            double outFrom = outBounds[0] + 6 + (2.1 / 30.0) * (outBounds[2] - 12);
            double outTo = outBounds[0] + 6 + (6.0 / 30.0) * (outBounds[2] - 12);
            await Mouse(core, "mouseMoved", outFrom, outY, "none", 0);
            string outFromX = outFrom.ToString(System.Globalization.CultureInfo.InvariantCulture);
            string outToX = outTo.ToString(System.Globalization.CultureInfo.InvariantCulture);
            string outPointY = outY.ToString(System.Globalization.CultureInfo.InvariantCulture);
            Check(await Script(core,
                "(()=>{const n=document.querySelector('#editorContent .trim-handle-out'),h=document.elementFromPoint(" + outFromX + "," + outPointY + ");return !!n&&h===n})()") == "true",
                "trim out thumb receives a real pointer at its current position");
            await Mouse(core, "mousePressed", outFrom, outY, "left", 1);
            await Mouse(core, "mouseMoved", outTo, outY, "left", 1);
            await Mouse(core, "mouseReleased", outTo, outY, "left", 0);
            string trimDragStateRaw = await Script(core,
                "JSON.stringify((()=>{const n=document.querySelector('#editorContent .trim-handle-out'),r=n.getBoundingClientRect(),h=document.elementFromPoint(" + outToX + "," + outPointY + ");return {value:n.value,min:n.min,max:n.max,disabled:n.disabled,rect:{left:r.left,top:r.top,width:r.width,height:r.height},target:{x:" + outToX + ",y:" + outPointY + "},hit:h?{tag:h.tagName,className:String(h.className),isHandle:h===n}:null,active:document.activeElement===n}})())");
            Console.WriteLine("NATIVE_TRIM_OUT_DRAG=" + Json.Deserialize<string>(trimDragStateRaw));
            await WaitFor(core,
                "(()=>{const n=document.querySelector('#editorContent .trim-handle-out');return Number(n.value)>5.7&&Number(n.value)<6.3})()",
                "real pointer drag moves the editable trim out handle", 6);
            await ReplaceTextTrusted(core, "#editorContent .trim-timecode[data-timecode=out]", "00:00:02.700");
            await WaitFor(core, "document.querySelector('#editorContent .trim-timecode[data-timecode=out]').value==='00:00:02.700'",
                "trusted text input edits the trim out timecode", 5);
            Check(await Script(core,
                "(()=>{const t=document.querySelector('#editorContent .video-trim-timeline'),i=t.querySelector('.trim-handle-in'),o=t.querySelector('.trim-handle-out'),ti=t.querySelector('[data-timecode=in]'),to=t.querySelector('[data-timecode=out]'),preview=t.querySelector('[data-review=preview-range]');return !!t&&!t.hidden&&!i.disabled&&!o.disabled&&!ti.disabled&&!to.disabled&&!ti.readOnly&&!to.readOnly&&Number(i.value)>.3&&Number(i.value)<.7&&Number(o.value)>2.6&&Number(o.value)<2.8&&!preview.disabled})()") == "true",
                "timeline handles and editable timecodes show the selected preview range");
            await Click(core, "#editorContent [data-review=preview-range]");
            await WaitFor(core,
                "(()=>{const v=document.querySelector('#editorContent video'),b=document.querySelector('#editorContent [data-review=preview-range]'),ab=document.querySelector('#inspector [data-review=ab]');return !v.paused&&v.currentTime>=.45&&v.currentTime<2.7&&b.textContent.includes('停止范围预览')&&ab.getAttribute('aria-pressed')==='false'&&!v.loop})()",
                "selected trim range previews in the player", 8);
            await WaitFor(core,
                "(()=>{const v=document.querySelector('#editorContent video');return v.paused&&Math.abs(v.currentTime-2.7)<.15})()",
                "selected range playback pauses at its out point", 12);

            // The export command is driven through its actual inspector button.
            // The source is a generated fixture and all export bytes must stay
            // under the isolated native-verifier data root.
            Check(await Script(core,
                "(()=>{const b=document.querySelector('#inspector [data-review=export]'),j=document.querySelector('#inspector .video-job');return !!b&&!b.disabled&&!!j})()") == "true",
                "a valid selected range enables the real rough-cut export action");
            await Click(core, "#inspector [data-review=export]");
            await WaitFor(core,
                "document.querySelector('#inspector .video-job')?.textContent.includes('导出完成')&&!!document.querySelector('#inspector .video-job a, #inspector .video-job button')",
                "rough-cut export finishes and exposes its download or reveal action", 120);
            string exportId = await FindVideoExportId(core);
            var exportState = DesktopApi.Request("/api/video/exports/" + exportId);
            object exportJobState, downloadUrl, exportStart, exportEnd;
            Check(exportState.TryGetValue("state", out exportJobState) && (exportJobState as string) == "done" &&
                  exportState.TryGetValue("download_url", out downloadUrl) &&
                  String.Equals(downloadUrl as string, "/api/video/exports/" + exportId + "/file", StringComparison.Ordinal) &&
                  exportState.TryGetValue("start", out exportStart) && Convert.ToDouble(exportStart) >= .4 &&
                  exportState.TryGetValue("end", out exportEnd) && Convert.ToDouble(exportEnd) >= 2.6 && Convert.ToDouble(exportEnd) <= 2.8,
                "the real backend reports a completed short-range rough-cut with a downloadable output");
            string exportPath = Path.Combine(dataRoot, "video-review", exportId, "clip.mp4");
            Check(File.Exists(exportPath) && new FileInfo(exportPath).Length > 1024,
                "completed rough-cut bytes exist only in the isolated export fixture folder");
            Check(await Script(core,
                "(()=>{const n=document.querySelector('#inspector .video-job'),a=n?.querySelector('a,button');return !!a&&!a.hidden&&a.getClientRects().length>0&&n.textContent.includes('导出完成')})()") == "true",
                "completed export action is visibly available in the inspector");

            // Capture and measure both wide modes, then force a real 900px
            // WebView viewport to exercise the narrow tools drawer layout.
            var originalMinimumSize = window.MinimumSize;
            var originalClientSize = window.ClientSize;
            try
            {
                await Script(core, "(()=>{const v=document.querySelector('#editorContent video');v.pause();v.currentTime=3;return true})()");
                await WaitFor(core, "(()=>{const v=document.querySelector('#editorContent video');return v.paused&&Math.abs(v.currentTime-3)<.12})()",
                    "hold a stable video position before changing workspace mode", 8);
                await Click(core, "[data-video-mode=classic]");
                await WaitFor(core, "document.querySelector('[data-video-mode=classic]')?.getAttribute('aria-pressed')==='true'",
                    "classic video workspace mode is selected", 8);
                Check(await Script(core,
                    "(()=>{const w=document.querySelector('#workspace'),s=document.querySelector('.app-shell>.sidebar'),l=document.querySelector('#library');return w.classList.contains('video-workspace')&&w.classList.contains('video-library-visible')&&getComputedStyle(s).display!=='none'&&getComputedStyle(l).display!=='none'})()") == "true",
                    "classic mode shows navigation and the media library beside review tools");
                await Script(core, "window.__classicVideoEditorWidth=document.querySelector('#editor').getBoundingClientRect().width");
                await CheckNoHorizontalOverflow(core, "wide classic video workspace");
                await WaitForToastsToClear(core, "classic-mode notification clears before screenshot evidence");
                await Capture(core, "video-workspace-classic-wide.png", "wide classic workspace screenshot");

                await Script(core,
                    "(()=>{const v=document.querySelector('#editorContent video'),r=document.querySelector('#inspector .video-review'),t=document.querySelector('#editorContent .video-trim-timeline');window.__videoModeSnapshot={v,r,t,time:v.currentTime,range:r.querySelector('.review-range').textContent,in:t.querySelector('[data-timecode=in]').value,out:t.querySelector('[data-timecode=out]').value,tab:r.querySelector('[data-review-tab][aria-selected=true]').dataset.reviewTab,status:r.querySelector('.review-status').textContent};return true})()");
                await Click(core, "[data-video-mode=focus]");
                await WaitFor(core, "document.querySelector('[data-video-mode=focus]')?.getAttribute('aria-pressed')==='true'",
                    "focus video workspace mode is selected", 8);
                Check(await Script(core,
                    "(()=>{const s=window.__videoModeSnapshot,w=document.querySelector('#workspace'),shell=document.querySelector('.app-shell'),v=document.querySelector('#editorContent video'),r=document.querySelector('#inspector .video-review'),t=document.querySelector('#editorContent .video-trim-timeline');return !!s&&v===s.v&&r===s.r&&t===s.t&&Math.abs(v.currentTime-s.time)<.04&&r.querySelector('.review-range').textContent===s.range&&t.querySelector('[data-timecode=in]').value===s.in&&t.querySelector('[data-timecode=out]').value===s.out&&r.querySelector('[data-review-tab][aria-selected=true]').dataset.reviewTab===s.tab&&r.querySelector('.review-status').textContent===s.status&&shell.classList.contains('focus-workbench')&&getComputedStyle(shell.querySelector('.sidebar')).display==='none'&&!w.classList.contains('video-library-visible')&&document.querySelector('#editor').getBoundingClientRect().width>window.__classicVideoEditorWidth})()") == "true",
                    "switching modes preserves the same video, playhead, trim draft, inspector and selected tab");
                await CheckNoHorizontalOverflow(core, "wide focus video workspace");
                await WaitForToastsToClear(core, "focus-mode notification clears before screenshot evidence");
                await Capture(core, "video-workspace-focus-wide.png", "wide focus workspace screenshot");

                window.MinimumSize = Size.Empty;
                window.ClientSize = new Size(900, 680);
                Application.DoEvents();
                await WaitFor(core, "window.innerWidth<=910&&window.innerWidth>=870",
                    "native Studio WebView reaches a 900px client width", 8);
                Check(await Script(core,
                    "(()=>{const b=document.querySelector('#videoToolsToggle');return !!b&&getComputedStyle(b).display!=='none'})()") == "true",
                    "narrow workspace reveals the dedicated tools drawer button");
                await CheckNoHorizontalOverflow(core, "900px focus video workspace");
                if (await Script(core, "document.querySelector('#videoToolsToggle').getAttribute('aria-expanded')==='true'") != "true")
                    await Click(core, "#videoToolsToggle");
                await WaitFor(core, "document.querySelector('#workspace').classList.contains('video-tools-open')&&getComputedStyle(document.querySelector('#inspector')).display!=='none'",
                    "narrow tools drawer opens the inspector", 6);
                await Script(core,
                    "(()=>{const id=(window.__nativePaintTarget||0)+1;window.__nativePaintTarget=id;let n=0;const frame=()=>{if(++n===2){window.__nativePaintDone=id;return}requestAnimationFrame(frame)};requestAnimationFrame(frame);return true})()");
                await WaitFor(core, "window.__nativePaintDone===window.__nativePaintTarget",
                    "two animation frames paint the opened narrow drawer", 3);
                string narrowLayoutRaw = await Script(core,
                    "JSON.stringify((()=>{const box=n=>{if(!n)return null;const r=n.getBoundingClientRect();return {left:r.left,top:r.top,right:r.right,bottom:r.bottom,width:r.width,height:r.height}};const w=document.querySelector('#workspace'),p=document.querySelector('#inspector'),v=p?.querySelector('.video-review'),tabs=p?.querySelector('.video-review-tabs'),tab=tabs?.querySelector('[data-review-tab=file]'),tr=tab?.getBoundingClientRect(),x=tr?tr.left+tr.width/2:0,y=tr?tr.top+tr.height/2:0,hit=tr?document.elementFromPoint(x,y):null,cs=p?getComputedStyle(p):null;return {viewport:{width:innerWidth,height:innerHeight},workspace:box(w),inspector:box(p),review:box(v),tabs:box(tabs),fileTab:box(tab),computed:cs?{display:cs.display,visibility:cs.visibility,opacity:cs.opacity,position:cs.position,zIndex:cs.zIndex,width:cs.width,gridColumn:cs.gridColumn}:null,workspaceGrid:w?getComputedStyle(w).gridTemplateColumns:null,hit:hit?{tag:hit.tagName,text:(hit.textContent||'').trim(),tab:hit.closest('[data-review-tab]')?.getAttribute('data-review-tab'),insideInspector:p.contains(hit)}:null}})())");
                Console.WriteLine("NATIVE_NARROW_LAYOUT=" + Json.Deserialize<string>(narrowLayoutRaw));
                await WaitFor(core,
                    "(()=>{const p=document.querySelector('#inspector'),r=p.getBoundingClientRect(),v=p.querySelector('.video-review'),q=v?.getBoundingClientRect(),t=p.querySelector('.video-review-tabs [data-review-tab=file]'),tr=t?.getBoundingClientRect(),hit=tr&&tr.width>0&&tr.height>0?document.elementFromPoint(tr.left+tr.width/2,tr.top+tr.height/2):null,ix=Math.max(0,Math.min(r.right,innerWidth)-Math.max(r.left,0)),iy=Math.max(0,Math.min(r.bottom,innerHeight)-Math.max(r.top,0)),s=getComputedStyle(p);return s.display!=='none'&&s.visibility==='visible'&&Number(s.opacity)>.9&&r.width>=280&&r.left>=innerWidth-340&&r.right<=innerWidth+1&&ix>=280&&iy>=220&&q?.width>=260&&q.right<=r.right+1&&!!tr&&tr.width>=40&&tr.height>=20&&tr.left>=0&&tr.right<=innerWidth&&tr.top>=0&&tr.bottom<=innerHeight&&p.contains(hit)&&hit.closest('[data-review-tab]')===t})()",
                    "narrow tools drawer occupies the viewport and its rightmost file tab receives pointer hit testing", 6);
                Check(await Script(core,
                    "(()=>{const w=document.querySelector('#workspace'),p=document.querySelector('#inspector'),r=p.getBoundingClientRect(),v=p.querySelector('.video-review'),q=v?.getBoundingClientRect(),t=p.querySelector('.video-review-tabs [data-review-tab=file]'),tr=t?.getBoundingClientRect(),hit=tr&&tr.width>0&&tr.height>0?document.elementFromPoint(tr.left+tr.width/2,tr.top+tr.height/2):null,ix=Math.max(0,Math.min(r.right,innerWidth)-Math.max(r.left,0)),iy=Math.max(0,Math.min(r.bottom,innerHeight)-Math.max(r.top,0)),s=getComputedStyle(p);return w.classList.contains('video-tools-open')&&s.display!=='none'&&s.visibility==='visible'&&Number(s.opacity)>.9&&r.width>=280&&r.left>=innerWidth-340&&r.right<=innerWidth+1&&ix>=280&&iy>=220&&q?.width>=260&&q.right<=r.right+1&&!!tr&&tr.width>=40&&tr.height>=20&&tr.left>=0&&tr.right<=innerWidth&&tr.top>=0&&tr.bottom<=innerHeight&&p.contains(hit)&&hit.closest('[data-review-tab]')===t})()") == "true",
                    "narrow tools drawer has a visible right-side area and hit-testable file tab");
                await WaitForToastsToClear(core, "mode-switch notifications clear before narrow screenshot evidence");
                await Script(core,
                    "(()=>{const id=(window.__nativePaintTarget||0)+1;window.__nativePaintTarget=id;let n=0;const frame=()=>{if(++n===2){window.__nativePaintDone=id;return}requestAnimationFrame(frame)};requestAnimationFrame(frame);return true})()");
                await WaitFor(core, "window.__nativePaintDone===window.__nativePaintTarget",
                    "two animation frames complete before the narrow screenshot", 3);
                await Capture(core, "video-workspace-narrow-900.png", "900px tools drawer screenshot");
                await CheckNoHorizontalOverflow(core, "900px focus video workspace with tools drawer");
            }
            finally
            {
                window.MinimumSize = Size.Empty;
                window.ClientSize = originalClientSize;
                window.MinimumSize = originalMinimumSize;
            }

            // The strict index is a real POST to the package backend. Its
            // worker decodes the source while the browser clock keeps moving.
            await Script(core, "(()=>{const v=document.querySelector('#editorContent video');v.currentTime=1.2;v.play();return true})()");
            await WaitFor(core, "(()=>{const v=document.querySelector('#editorContent video');return !v.paused&&Math.abs(v.currentTime-1.2)<.1})()",
                "position video before background indexing", 8);
            await Click(core, "#inspector [data-review-tab=frames]");
            await WaitFor(core, "document.querySelector('#inspector [data-review-tab=frames]')?.getAttribute('aria-selected')==='true'&&!!document.querySelector('#inspector [data-review-panel=frames]:not([hidden])')",
                "frame tools occupy the selected inspector tab", 6);
            string frameTitleColorRaw = await Script(core,
                "JSON.stringify((()=>{const e=document.querySelector('.video-frame-inspector .frame-tool-heading strong');if(!e)return {found:false};const rgba=s=>{const m=s.match(/rgba?\\(([^)]+)\\)/);if(!m)return null;const p=m[1].split(',').map(Number);return {c:p.slice(0,3),a:p.length>3?p[3]:1}},fg=rgba(getComputedStyle(e).color)?.c;let n=e,layers=[],source='canvas';while(n){const c=rgba(getComputedStyle(n).backgroundColor);if(c&&c.a>0){layers.push(c);if(c.a>=.999){source=n.id||n.className||n.tagName;break}}n=n.parentElement}let bg=[255,255,255];for(let i=layers.length-1;i>=0;i--)bg=layers[i].c.map((v,j)=>v*layers[i].a+bg[j]*(1-layers[i].a));const lin=x=>{x/=255;return x<=.04045?x/12.92:Math.pow((x+.055)/1.055,2.4)},lum=c=>.2126*lin(c[0])+.7152*lin(c[1])+.0722*lin(c[2]),a=lum(fg||[0,0,0]),b=lum(bg),ratio=(Math.max(a,b)+.05)/(Math.min(a,b)+.05),r=e.getBoundingClientRect();return {found:true,text:e.textContent.trim(),foreground:getComputedStyle(e).color,background:`rgb(${bg.map(Math.round).join(',')})`,backgroundSource:source,contrast:ratio,visible:r.width>0&&r.height>0}})())");
            Console.WriteLine("NATIVE_FRAME_TITLE_COLOR=" + Json.Deserialize<string>(frameTitleColorRaw));
            Check(await Script(core,
                "(()=>{const e=document.querySelector('.video-frame-inspector .frame-tool-heading strong');if(!e)return false;const rgba=s=>{const m=s.match(/rgba?\\(([^)]+)\\)/);if(!m)return null;const p=m[1].split(',').map(Number);return {c:p.slice(0,3),a:p.length>3?p[3]:1}},fg=rgba(getComputedStyle(e).color)?.c;let n=e,layers=[];while(n){const c=rgba(getComputedStyle(n).backgroundColor);if(c&&c.a>0){layers.push(c);if(c.a>=.999)break}n=n.parentElement}let bg=[255,255,255];for(let i=layers.length-1;i>=0;i--)bg=layers[i].c.map((v,j)=>v*layers[i].a+bg[j]*(1-layers[i].a));if(!fg)return false;const lin=x=>{x/=255;return x<=.04045?x/12.92:Math.pow((x+.055)/1.055,2.4)},lum=c=>.2126*lin(c[0])+.7152*lin(c[1])+.0722*lin(c[2]),a=lum(fg),b=lum(bg);return e.getClientRects().length>0&&(Math.max(a,b)+.05)/(Math.min(a,b)+.05)>=4.5})()") == "true",
                "visible strict-frame heading has at least 4.5:1 text contrast against its rendered panel");
            Check(await Script(core,
                "(()=>{const b=document.querySelector('#inspector [data-frame=start]');return !!b&&!b.disabled&&b.getClientRects().length>0})()") == "true",
                "frame index controls are visible and enabled in the inspector");
            double before = Convert.ToDouble(await Script(core, "document.querySelector('#editorContent video').currentTime"));
            await Click(core, "#inspector [data-frame=start]");
            await Task.Delay(450);
            bool remainedPlaying = await Script(core,
                "(()=>{const v=document.querySelector('#editorContent video');return !v.paused&&v.currentTime>" +
                before.ToString(System.Globalization.CultureInfo.InvariantCulture) + "+.15})()") == "true";
            await WaitFor(core, "document.querySelector('.frame-status')?.textContent.includes('索引已就绪')",
                "real backend frame index completes", 150);
            Check(remainedPlaying && await Script(core, "!document.querySelector('#editorContent video').paused") == "true",
                "real backend indexing and completion do not pause or reset playback");
            string status = Json.Deserialize<string>(await Script(core, "document.querySelector('.frame-status').textContent"));
            Check(status.Contains("索引已就绪") && status.Contains("帧"), "frame index readiness and decoded frame count are shown in the real UI");
            string indexSession = await FindIndexSession(core);
            var indexedState = DesktopApi.Request("/api/video/frames/jobs/" + indexSession);
            object indexedJobState, indexedKind, indexedSessionValue;
            Check(indexedState.TryGetValue("state", out indexedJobState) && (indexedJobState as string) == "ready" &&
                  indexedState.TryGetValue("kind", out indexedKind) && (indexedKind as string) == "index" &&
                  indexedState.TryGetValue("session", out indexedSessionValue) && (indexedSessionValue as string) == indexSession,
                "real backend confirms the indexed session is ready before still-frame inspection");

            await Script(core,
                "(()=>{const v=document.querySelector('#editorContent video');v.pause();v.currentTime=12;const r=document.querySelector('#inspector .video-review');window.__readyIndexModeSnapshot={v,r,t:document.querySelector('#editorContent .video-trim-timeline'),time:v.currentTime,range:r.querySelector('.review-range').textContent,status:r.querySelector('.review-status').textContent,tab:r.querySelector('[data-review-tab][aria-selected=true]').dataset.reviewTab};return true})()");
            await WaitFor(core, "(()=>{const s=window.__readyIndexModeSnapshot;return s&&s.v.paused&&Math.abs(s.v.currentTime-12)<.15})()",
                "pause at a stable position with a ready frame index", 8);
            await Click(core, "[data-video-mode=classic]");
            await WaitFor(core, "document.querySelector('[data-video-mode=classic]')?.getAttribute('aria-pressed')==='true'",
                "return to classic mode with the frame index ready", 8);
            await Click(core, "[data-video-mode=focus]");
            await WaitFor(core, "document.querySelector('[data-video-mode=focus]')?.getAttribute('aria-pressed')==='true'",
                "restore focus mode with the frame index ready", 8);
            var retainedIndexState = DesktopApi.Request("/api/video/frames/jobs/" + indexSession);
            object retainedState;
            Check(retainedIndexState.TryGetValue("state", out retainedState) && (retainedState as string) == "ready" &&
                  await Script(core,
                    "(()=>{const s=window.__readyIndexModeSnapshot,v=document.querySelector('#editorContent video'),r=document.querySelector('#inspector .video-review'),t=document.querySelector('#editorContent .video-trim-timeline');return !!s&&s.v===v&&s.r===r&&s.t===t&&v.paused&&Math.abs(v.currentTime-s.time)<.04&&r.querySelector('.review-range').textContent===s.range&&r.querySelector('.review-status').textContent===s.status&&r.querySelector('[data-review-tab][aria-selected=true]').dataset.reviewTab===s.tab})()") == "true",
                "mode changes retain the same paused video and unsaved trim draft while the real frame index remains ready");

            await Click(core, "#inspector [data-frame=enter]");
            await WaitFor(core,
                "(()=>{const v=document.querySelector('#editorContent video'),o=document.querySelector('.frame-overlay');return !!o&&!o.hidden&&v.paused&&document.querySelector('.frame-overlay-label').textContent.includes('第 0 帧 ·')&&document.querySelector('.frame-overlay img').naturalWidth>0})()",
                "explicit entry pauses and paints a backend-decoded PNG", 45);
            Check(await Script(core, "document.querySelector('.frame-overlay img').naturalWidth>=160") == "true",
                "decoded still image is displayed in the package WebView");

            await Click(core, "#inspector [data-frame=next]");
            await WaitFor(core, "document.querySelector('.frame-overlay-label')?.textContent.includes('第 1 帧 ·')",
                "next-frame CDP click", 30);
            await Click(core, "#inspector [data-frame=previous]");
            await WaitFor(core, "document.querySelector('.frame-overlay-label')?.textContent.includes('第 0 帧 ·')",
                "previous-frame CDP click", 30);
            await Click(core, "#inspector [data-frame=next]");
            await WaitFor(core, "document.querySelector('.frame-overlay-label')?.textContent.includes('第 1 帧 ·')",
                "next-frame restored after previous", 30);

            await Script(core, "document.querySelector('.video-screen').focus({preventScroll:true})");
            await Key(core, ",", "Comma", 188);
            await WaitFor(core, "document.querySelector('.frame-overlay-label')?.textContent.includes('第 0 帧 ·')",
                "comma keyboard shortcut steps backward", 30);
            await Key(core, ".", "Period", 190);
            await WaitFor(core, "document.querySelector('.frame-overlay-label')?.textContent.includes('第 1 帧 ·')",
                "period keyboard shortcut steps forward", 30);
            Check(await Script(core, "document.querySelector('#editorContent video').paused") == "true",
                "keyboard frame steps keep explicit still inspection paused");

            await WaitForToastsToClear(core, "workspace-mode notifications clear before strict-frame screenshot evidence");
            string screenshot = Path.Combine(evidence, "strict-frame-webview.png");
            using (var output = new FileStream(screenshot, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                await core.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, output);
            Check(new FileInfo(screenshot).Length > 2048, "real Studio WebView strict-frame state captured at " + Path.GetFileName(screenshot));

            await Click(core, "#inspector [data-frame=resume]");
            await WaitFor(core,
                "(()=>document.querySelector('.frame-overlay').hidden&&!document.querySelector('#editorContent video').paused)()",
                "return from the decoded still to playing video", 15);
            var resumedIndexState = DesktopApi.Request("/api/video/frames/jobs/" + indexSession);
            object resumedJobState;
            Check(resumedIndexState.TryGetValue("state", out resumedJobState) && (resumedJobState as string) == "ready",
                "return-to-video preserves the real ready index for another inspection");
            await Click(core, "#inspector [data-frame=cancel]");
            await WaitFor(core, "document.querySelector('.frame-status')?.textContent.includes('索引与静帧检查已关闭')",
                "close/cancel frame inspection in the real UI", 20);
            var backendState = DesktopApi.Request("/api/video/frames/jobs/" + indexSession);
            object state;
            Check(backendState.TryGetValue("state", out state) && (state as string) == "cancelled",
                "real backend confirms the UI cancelled this ready index session and released it");
            Check(await Script(core, "!document.querySelector('#editorContent video').paused") == "true",
                "closing frame inspection leaves regular playback active");

            Console.WriteLine("NATIVE_FRAME_TESTS=" + Json.Serialize(Passed));
            result = 0;
        }

        private static void Close(StudioWindow window)
        {
            if (window == null || window.IsDisposed) return;
            var field = window.GetType().GetField("exitApproved", BindingFlags.Instance | BindingFlags.NonPublic);
            if (field != null) field.SetValue(window, true);
            window.Close();
        }

        [STAThread]
        private static int Main(string[] args)
        {
            if (args.Length != 8) throw new ArgumentException("Expected release root, runtime root, fixture root, data root, projects root, port, video path and evidence root.");
            string root = Path.GetFullPath(args[0]);
            string runtime = Path.GetFullPath(args[1]);
            string testRoot = Path.GetFullPath(args[2]);
            string data = Path.GetFullPath(args[3]);
            string projects = Path.GetFullPath(args[4]);
            int port = Int32.Parse(args[5], System.Globalization.CultureInfo.InvariantCulture);
            string video = Path.GetFullPath(args[6]);
            evidence = Path.GetFullPath(args[7]);
            if (!Directory.Exists(root) || !Directory.Exists(runtime) || !Directory.Exists(testRoot) || !Directory.Exists(evidence) ||
                !File.Exists(Path.Combine(root, "server.py")) || !File.Exists(Path.Combine(root, "launcher.pyw")) ||
                !File.Exists(Path.Combine(runtime, "webview2", "msedgewebview2.exe")) ||
                !File.Exists(Path.Combine(runtime, "ffmpeg", "bin", "ffmpeg.exe")) ||
                !video.StartsWith(testRoot.TrimEnd('\\', '/') + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase) ||
                !data.StartsWith(testRoot.TrimEnd('\\', '/') + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase) ||
                !projects.StartsWith(testRoot.TrimEnd('\\', '/') + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase) ||
                !evidence.StartsWith(testRoot.TrimEnd('\\', '/') + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase) ||
                IsolatedStartup.Overlaps(root, testRoot) || IsolatedStartup.Overlaps(data, projects) ||
                IsolatedStartup.Overlaps(data, evidence) || IsolatedStartup.Overlaps(projects, evidence))
                throw new InvalidDataException("Native frame harness paths are missing, linked, or overlapping.");
            Directory.CreateDirectory(data); Directory.CreateDirectory(projects);
            Environment.SetEnvironmentVariable("YINGXU_DATA_DIR", data);
            Environment.SetEnvironmentVariable("YINGXU_PROJECTS_DIR", projects);
            Environment.SetEnvironmentVariable("YINGXU_WEBVIEW2_MODE", "bundled");
            Environment.SetEnvironmentVariable("YINGXU_PYTHON", null);
            Hub.Root = root; Hub.Data = data; Hub.Cache = Path.Combine(data, "desktop");
            Hub.Port = port; Hub.Url = "http://127.0.0.1:" + port + "/";
            Program.Isolated = null; Program.IncrementalInstalling = false;
            Program.InitialFiles = new string[0]; Program.InstanceKey = Guid.NewGuid().ToString("N");
            Program.ActivateEvent = new EventWaitHandle(false, EventResetMode.AutoReset);
            try
            {
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                typeof(Program).GetMethod("PrepareLibraries", BindingFlags.Static | BindingFlags.NonPublic).Invoke(null, null);
                CoreWebView2Environment.SetLoaderDllFolderPath(Program.LoaderFolder);
                using (var window = new StudioWindow(initialize: true, routed: true))
                using (var startup = new System.Windows.Forms.Timer { Interval = 100 })
                using (var deadline = new System.Windows.Forms.Timer { Interval = 240000 })
                {
                    bool started = false;
                    startup.Tick += async delegate
                    {
                        if (started || window.IsDisposed || !(bool)Field(window, "pageReady")) return;
                        started = true; startup.Stop(); deadline.Stop();
                        try { await RunChecks(window, video, data); }
                        catch (Exception error)
                        {
                            Console.WriteLine("NATIVE_FRAME_ERROR " + error.GetType().Name + ": " + error.Message);
                            result = 1;
                        }
                        finally { Close(window); }
                    };
                    deadline.Tick += delegate
                    {
                        deadline.Stop(); startup.Stop();
                        Console.WriteLine("NATIVE_FRAME_ERROR TimeoutException: StudioWindow did not become ready.");
                        result = 1; Close(window);
                    };
                    window.Shown += delegate { startup.Start(); deadline.Start(); };
                    Application.Run(window);
                }
            }
            finally { if (Program.ActivateEvent != null) Program.ActivateEvent.Dispose(); }
            return result;
        }
    }
}
