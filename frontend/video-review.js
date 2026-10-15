(() => {
  'use strict';

  const emptyReview = () => ({markers:[],in:null,out:null,a:null,b:null});
  const copy = value => JSON.parse(JSON.stringify(value));
  const RANGE_STEP = .001;
  const TERMINAL_EXPORT_STATES = new Set(['done','error','cancelled']);
  const isTerminalExportState = state => TERMINAL_EXPORT_STATES.has(state);
  const clamp = (value,min,max) => Math.max(min,Math.min(max,value));
  const validDuration = value => Number.isFinite(value) && value > 0;
  const validRange = (review,duration) => Number.isFinite(review?.in) && Number.isFinite(review?.out) && review.in >= 0 && review.out > review.in && validDuration(duration) && review.out <= duration + .001;

  function parseTimecode(raw) {
    const match = String(raw).trim().match(/^(\d{2,}):([0-5]\d):([0-5]\d)(?:\.(\d{1,3}))?$/);
    if (!match) return null;
    const fraction = Number((match[4] || '').padEnd(3,'0')) / 1000;
    const value = Number(match[1]) * 3600 + Number(match[2]) * 60 + Number(match[3]) + fraction;
    return Number.isFinite(value) ? value : null;
  }

  function mount(player,{tab,api,onDirty=()=>{},inspector=null}={}) {
    if (!player?.shell || !player?.video || !tab || typeof api !== 'function') throw new TypeError('视频审片工作区缺少播放器、资源或请求接口。');
    const controller = new AbortController(), signal = controller.signal;
    const timeline = document.createElement('section');
    timeline.className = 'video-trim-timeline';
    timeline.setAttribute('aria-label','粗剪时间线');
    timeline.innerHTML = `
      <div class="video-trim-heading"><strong>粗剪范围</strong><span class="trim-duration">时长 —</span></div>
      <div class="video-trim-track" role="group" aria-label="拖动入点和出点边界">
        <div class="video-trim-selection" aria-hidden="true"></div><div class="video-trim-playhead" aria-hidden="true"></div>
        <input class="video-trim-handle trim-handle-in" type="range" min="0" max="1" step="0.001" value="0" aria-label="粗剪入点">
        <input class="video-trim-handle trim-handle-out" type="range" min="0" max="1" step="0.001" value="0" aria-label="粗剪出点">
      </div>
      <div class="video-trim-fields">
        <label>入点 <input class="trim-timecode" data-timecode="in" inputmode="numeric" autocomplete="off" aria-label="入点时间码" placeholder="00:00:00.000"></label>
        <label>出点 <input class="trim-timecode" data-timecode="out" inputmode="numeric" autocomplete="off" aria-label="出点时间码" placeholder="00:00:00.000"></label>
        <button type="button" data-review="in" title="把当前播放时间设为入点">设入点</button>
        <button type="button" data-review="out" title="把当前播放时间设为出点">设出点</button>
        <button type="button" data-review="preview-range" class="trim-preview" disabled>预览选中范围</button>
        <button type="button" data-review="clear-range" class="trim-clear">清除范围</button>
      </div>
      <p class="trim-guidance" role="status">载入视频后可拖动边界或编辑时间码。设置入点和出点后可预览选中范围。</p>
    `;
    player.shell.append(timeline);

    const workspace = document.createElement('section');
    workspace.className = 'video-review';
    workspace.setAttribute('aria-label','视频工具');
    workspace.innerHTML = `
      <header class="video-review-heading">
        <div class="video-review-title"><strong>视频工具</strong><p class="review-status" role="status" aria-live="polite">正在载入审片记录…</p></div>
        <div class="video-review-header-actions"><button type="button" class="review-load-retry" data-review="retry-load" hidden>重试</button><button type="button" class="video-review-close" data-action="toggle-video-tools" aria-label="收起视频工具" title="收起视频工具">收起</button></div>
      </header>
      <nav class="video-review-tabs" role="tablist" aria-label="视频工具">
        <button type="button" role="tab" id="video-review-tab-trim" aria-controls="video-review-panel-trim" data-review-tab="trim">剪辑</button>
        <button type="button" role="tab" id="video-review-tab-markers" aria-controls="video-review-panel-markers" data-review-tab="markers">标记</button>
        <button type="button" role="tab" id="video-review-tab-frames" aria-controls="video-review-panel-frames" data-review-tab="frames">逐帧</button>
        <button type="button" role="tab" id="video-review-tab-file" aria-controls="video-review-panel-file" data-review-tab="file">文件</button>
      </nav>
      <div class="video-review-panels">
        <section id="video-review-panel-trim" class="video-review-content" role="tabpanel" aria-labelledby="video-review-tab-trim" data-review-panel="trim">
          <div class="review-clock-row"><span>当前时间</span><output class="review-clock">—</output><button type="button" data-review="step-back" title="按时间后退 40 毫秒；不是逐帧定位">−40 ms</button><button type="button" data-review="step-forward" title="按时间前进 40 毫秒；不是逐帧定位">+40 ms</button></div>
          <div class="review-range-card"><strong>已选范围</strong><output class="review-range">尚未设置</output><p>时间码定位是近似时间 seek；逐帧检查会单独解码并核对源帧。</p></div>
          <div class="review-export-card">
            <label class="review-export-label" for="reviewExportMode">导出方式</label>
            <select id="reviewExportMode" class="review-export-mode" aria-label="粗剪导出方式"><option value="encode">精确时间裁切 · MP4</option><option value="copy">快速复制 · MKV</option></select>
            <button type="button" data-review="export">导出选中范围</button>
            <button type="button" data-review="cancel" hidden>取消导出</button>
          </div>
          <details class="review-compatibility"><summary>格式与兼容性</summary><p>两种方式都导出第一条视频和第一条音轨，不含其他音轨、字幕或附件。精确裁切会重编码为 H.264 / AAC（8-bit SDR），不用于保留 HDR 或高位深母版；快速复制保留编码，但入点可能向前对齐关键帧，部分播放器兼容性有限。</p></details>
          <div class="video-job" role="status" aria-live="polite"></div>
        </section>
        <section id="video-review-panel-markers" class="video-review-content" role="tabpanel" aria-labelledby="video-review-tab-markers" data-review-panel="markers" hidden>
          <div class="review-ab-card"><strong>比较点</strong><div class="review-ab-actions"><button type="button" data-review="a">设 A</button><button type="button" data-review="b">设 B</button><button type="button" data-review="ab" aria-pressed="false">循环 A–B</button></div><output class="review-ab-range">A — · B —</output></div>
          <form class="video-marker-form">
            <label for="videoMarkerNote">标记备注</label><div><input id="videoMarkerNote" class="video-marker-note" maxlength="500" placeholder="记录此处要检查什么"><button type="button" data-review="mark">添加当前时间标记</button></div>
            <p class="marker-count">0 / 500 个标记</p>
          </form>
          <div class="video-markers" aria-label="审片标记"></div>
          <div class="review-save-row"><p>标记和范围保存在审片记录中，不会修改原视频。</p><button type="button" class="button-primary" data-review="save">保存审片记录</button></div>
        </section>
        <section id="video-review-panel-frames" class="video-review-content video-review-frame-host" role="tabpanel" aria-labelledby="video-review-tab-frames" data-review-panel="frames" hidden></section>
        <section id="video-review-panel-file" class="video-review-content video-review-file-content" role="tabpanel" aria-labelledby="video-review-tab-file" data-review-panel="file" hidden></section>
      </div>`;

    const $ = selector => workspace.querySelector(selector);
    const timeline$ = selector => timeline.querySelector(selector);
    const source = tab.source === 'external' ? 'external' : 'file';
    const url = `/api/video/review/${source}/${encodeURIComponent(tab.id)}`;
    let loaded = false, loading = false, destroyed = false, pollTimer = 0, historyLoaded = false;
    let abEnabled = false, rangePreview = false, previewEndPause = false;
    let exportSubmitting = !!tab.videoExportSubmitting;
    let exportStatusUnknown = !!tab.videoExportStatusUnknown || !!tab.videoExportReconcileRequired || (!!tab.videoExportId && !tab.videoExportState) || !tab.videoExportId;
    let exportBusy = exportSubmitting || exportStatusUnknown || !!tab.videoExportReconcileRequired || (!!tab.videoExportId && !isTerminalExportState(tab.videoExportState));
    let exportCheckGeneration=0;
    let revision = tab.videoReviewRevision || '';
    let review = tab.videoReviewDraft || emptyReview();
    if (!Array.isArray(review.markers)) review.markers = [];
    let savedReview = tab.videoReviewSaved || emptyReview();
    let frames = null, frameState = {inspecting:false,busy:false,exporting:false};
    const filePanel = $('[data-review-panel="file"]');
    const currentDuration = () => player.video.duration;
    const isFrameLocked = () => !!(frameState.inspecting || frames?.isInspecting?.());
    const setStatus = text => { $('.review-status').textContent = text; };
    function showExportCheckError(message) {
      const node=$('.video-job');node.replaceChildren(document.createTextNode(message));
      const retry=document.createElement('button');retry.type='button';retry.dataset.review='retry-export-status';retry.textContent='重试核对';
      node.append(document.createTextNode(' '),retry);
    }
    const callDirty = () => { try { onDirty(); } catch (_) {} };
    const setTimeError = text => { timeline$('.trim-guidance').textContent = text;if(text)timeline$('.trim-guidance').setAttribute('data-error','true');else timeline$('.trim-guidance').removeAttribute('data-error'); };
    const setGuidance = text => { timeline$('.trim-guidance').textContent=text;timeline$('.trim-guidance').removeAttribute('data-error'); };
    const setRangeCSS = () => {
      const duration = currentDuration(), validDurationNow = validDuration(duration);
      const start = Number.isFinite(review.in) ? clamp(review.in,0,validDurationNow?duration:0) : 0;
      const end = Number.isFinite(review.out) ? clamp(review.out,0,validDurationNow?duration:0) : 0;
      const at = validDurationNow && Number.isFinite(player.video.currentTime) ? clamp(player.video.currentTime,0,duration) : 0;
      timeline.style.setProperty('--trim-start',validDurationNow ? `${start/duration*100}%` : '0%');
      timeline.style.setProperty('--trim-end',validDurationNow ? `${end/duration*100}%` : '0%');
      timeline.style.setProperty('--trim-playhead',validDurationNow ? `${at/duration*100}%` : '0%');
    };
    function renderTimeline({preserveText=false}={}) {
      const duration = currentDuration(), durationReady = validDuration(duration), locked = isFrameLocked() || exportBusy;
      timeline$('.trim-duration').textContent = durationReady ? `总长 ${player.time(duration)}` : '等待视频时长';
      for (const side of ['in','out']) {
        const range = timeline$(`.trim-handle-${side}`), timecode = timeline$(`[data-timecode="${side}"]`);
        range.min = '0'; range.max = String(durationReady?duration:1);
        range.disabled = !loaded || !durationReady || locked;
        if (!preserveText) range.value = String(Number.isFinite(review[side])?clamp(review[side],0,durationReady?duration:0):side==='in'?0:durationReady?duration:0);
        if (!preserveText) timecode.value = Number.isFinite(review[side]) ? player.time(review[side]) : '';
        timecode.disabled = !loaded || !durationReady || locked;
        if (!preserveText) { timecode.removeAttribute('aria-invalid'); timecode.setCustomValidity?.(''); }
      }
      const valid = validRange(review,duration);
      timeline$('[data-review="in"]').disabled = !loaded || !durationReady || locked;
      timeline$('[data-review="out"]').disabled = !loaded || !durationReady || locked;
      timeline$('[data-review="preview-range"]').disabled = !loaded || !valid || locked;
      timeline$('[data-review="clear-range"]').disabled = !loaded || (!Number.isFinite(review.in)&&!Number.isFinite(review.out)) || locked;
      if (!valid && !timeline$('.trim-guidance').dataset.error) {
        timeline$('.trim-guidance').textContent = !loaded ? '审片记录载入后可编辑范围。' : !durationReady ? '正在读取视频时长…' : '尚未选定有效范围；先设置入点，再设置更靠后的出点。';
      } else if (valid && !rangePreview && !timeline$('.trim-guidance').dataset.error) setGuidance('拖动时间线边界、编辑时间码，或从播放器当前位置设置入点和出点。预览只播放所选范围。');
      timeline$('.trim-preview').textContent = rangePreview ? '停止范围预览' : '预览选中范围';
      setRangeCSS();
    }
    function renderControls() {
      const locked = isFrameLocked() || exportBusy;
      const durationReady = validDuration(currentDuration());
      const canEdit = loaded && !locked;
      for (const node of workspace.querySelectorAll('button[data-review]')) {
        const action = node.dataset.review;
        if (action === 'retry-load') { node.hidden = loaded || loading; continue; }
        if (action === 'retry-export-status') { node.disabled = false; continue; }
        if (action === 'cancel') { node.hidden = !exportBusy || exportSubmitting || !tab.videoExportId || !!tab.videoExportReconcileRequired; node.disabled = !exportBusy || exportSubmitting || !tab.videoExportId || !!tab.videoExportReconcileRequired; continue; }
        if (action === 'export') { node.disabled = !canEdit || !validRange(review,currentDuration()); continue; }
        if (action === 'save') { node.disabled = !loaded || !!tab.videoReviewSaving; continue; }
        node.disabled = !canEdit;
      }
      for (const node of workspace.querySelectorAll('.video-marker-note,.review-export-mode')) node.disabled = !loaded || locked;
      const markerCount = Array.isArray(review.markers) ? review.markers.length : 0;
      $('.marker-count').textContent = `${markerCount} / 500 个标记`;
      const validAB = Number.isFinite(review.a)&&Number.isFinite(review.b)&&review.b>review.a;
      $('[data-review="ab"]').disabled = !canEdit || !validAB;
      $('[data-review="ab"]').setAttribute('aria-pressed',String(abEnabled));
      $('[data-review="ab"]').textContent = abEnabled?'停止 A–B 循环':'循环 A–B';
      $('[data-review="save"]').textContent = tab.videoReviewSaving ? '保存中…' : '保存审片记录';
      for (const node of workspace.querySelectorAll('.video-marker-jump,.video-marker-remove')) node.disabled = !canEdit;
      renderTimeline();
    }
    tab.videoReviewRenderControls=renderControls;
    function renderMarkers() {
      const marks = $('.video-markers'); marks.replaceChildren();
      const markerRows = Array.isArray(review.markers) ? review.markers : [];
      markerRows.forEach((mark,index) => {
        const row = document.createElement('div'); row.className = 'video-marker-row';
        const jump = document.createElement('button'); jump.type='button'; jump.className='video-marker-jump'; jump.dataset.review='seek-marker'; jump.dataset.markerIndex=String(index);
        const note = typeof mark?.note === 'string' ? mark.note : '';
        jump.textContent = `${player.time(Number(mark?.time))} · ${note || '未填写备注'}`;
        const remove = document.createElement('button'); remove.type='button'; remove.className='video-marker-remove'; remove.dataset.review='remove-marker'; remove.dataset.markerIndex=String(index); remove.textContent='删除'; remove.setAttribute('aria-label',`删除 ${player.time(Number(mark?.time))} 标记`);
        row.append(jump,remove); marks.append(row);
      });
      if (!markerRows.length) { const empty=document.createElement('p');empty.className='video-markers-empty';empty.textContent=loaded?'还没有标记。播放到需要复查的位置，写下备注后添加。':'审片记录载入后会显示保存的标记。';marks.append(empty); }
    }
    function render() {
      $('.review-range').textContent = validRange(review,currentDuration()) ? `${player.time(review.in)} → ${player.time(review.out)} · ${player.time(review.out-review.in)}` : '尚未设置有效范围';
      $('.review-ab-range').textContent = `A ${player.time(review.a)} · B ${player.time(review.b)}`;
      renderMarkers(); renderControls();
    }
    function switchTab(key) {
      if (!['trim','markers','frames','file'].includes(key)) key='trim';
      tab.videoReviewWorkspaceTab = key;
      workspace.querySelectorAll('[data-review-tab]').forEach(node=>{const active=node.dataset.reviewTab===key;node.setAttribute('aria-selected',String(active));node.tabIndex=active?0:-1;});
      workspace.querySelectorAll('[data-review-panel]').forEach(node=>{node.hidden=node.dataset.reviewPanel!==key;});
      if(key==='markers')$('.video-marker-note').focus({preventScroll:true});
    }
    function setABFromReview() {
      const valid = Number.isFinite(review.a)&&Number.isFinite(review.b)&&review.b>review.a;
      if (!valid && abEnabled) { abEnabled=false; player.setAB(null,null); }
      else if (abEnabled) player.setAB(review.a,review.b);
    }
    function stopRangePreview({pause=false,seekOut=false}={}) {
      if (!rangePreview) return;
      rangePreview=false;
      if (seekOut && Number.isFinite(review.out)) player.seek(review.out);
      if (pause) { previewEndPause=true; player.pause(); previewEndPause=false; }
      renderTimeline();
    }
    function disableLoopsForRangePreview() {
      // A–B and the player's whole-video loop both override the trim endpoint.
      // Range preview takes ownership of playback, so leave loop mode visibly off.
      abEnabled=false;
      player.setAB(null,null);
      player.video.loop=false;
      const loopButton=player.shell.querySelector('[data-command="loop"]');
      if(loopButton)loopButton.setAttribute('aria-pressed','false');
      renderControls();
    }
    function changed() {
      if (rangePreview && !validRange(review,currentDuration())) stopRangePreview({pause:true});
      setABFromReview();
      tab.videoReviewDraft=review; tab.videoReviewDirty=true; callDirty();
      setStatus('审片记录尚未保存；原视频保持不变。'); render();
    }
    async function save() {
      if (!loaded || tab.videoReviewSaving) return false;
      const saveGeneration=(Number(tab.videoReviewSaveGeneration)||0)+1;
      tab.videoReviewSaveGeneration=saveGeneration;
      tab.videoReviewSaving=true;renderControls(); const raw=JSON.stringify(copy(tab.videoReviewDraft||review));
      try {
        const saveRevision=tab.videoReviewRevision||revision;
        const result=await api(url,{method:'POST',body:{revision:saveRevision,review:JSON.parse(raw)}});
        revision=tab.videoReviewRevision=result.revision;
        savedReview=tab.videoReviewSaved=JSON.parse(raw);
        tab.videoReviewDirty=JSON.stringify(tab.videoReviewDraft||review)!==raw;
        if(!destroyed)setStatus(tab.videoReviewDirty?'另有新修改尚未保存。':'审片记录已保存；原视频保持不变。');
        return !tab.videoReviewDirty;
      } catch(error) { if(!destroyed)setStatus(error.message);return false; }
      finally {
        if(tab.videoReviewSaveGeneration===saveGeneration)tab.videoReviewSaving=false;
        if(tab.videoReviewRenderControls)tab.videoReviewRenderControls();else if(!destroyed)renderControls();
        callDirty();
      }
    }
    tab.saveVideoReview=save;
    tab.discardVideoReview=()=>{
      savedReview=copy(tab.videoReviewSaved||savedReview);review=tab.videoReviewDraft=copy(savedReview);if(!Array.isArray(review.markers))review.markers=[];tab.videoReviewDirty=false;callDirty();
      if(!destroyed){abEnabled=false;player.setAB(null,null);render();setStatus('已恢复保存的审片记录。');}
    };
    async function loadHistory() {
      if(historyLoaded||destroyed)return;
      if(tab.videoExportSubmitting){exportSubmitting=true;exportBusy=true;renderControls();$('.video-job').textContent='正在提交所选范围的导出任务…';poll();return;}
      historyLoaded=true;
      const knownExportId=tab.videoExportId;
      const reconcileRequired=!!tab.videoExportReconcileRequired;
      const reconcileBaseline=tab.videoExportReconcileBaseline||null;
      try {
        if(!tab.videoExportId||reconcileRequired){
          $('.video-job').textContent='正在核对已有导出任务…';
          const history=await api(`/api/video/exports?source=${source}&id=${encodeURIComponent(tab.id)}`);
          if(destroyed)return;
          if(tab.videoExportSubmitting||tab.videoExportId!==knownExportId)return;
          const jobs=Array.isArray(history.jobs)?history.jobs:[];
          const baselineIndex=reconcileRequired&&reconcileBaseline?jobs.findIndex(job=>job?.id===reconcileBaseline):-1;
          // The service orders recent jobs newest first. Only entries before
          // the prior job can have been created by the ambiguous submission.
          const latest=reconcileRequired&&reconcileBaseline?(baselineIndex>0?jobs[0]:null):jobs[0];
          tab.videoExportHistoryChecked=true;
          if(latest?.id){
            tab.videoExportId=latest.id;tab.videoExportState=latest.state||'unknown';
            tab.videoExportStatusUnknown=!latest.state;
            exportStatusUnknown=tab.videoExportStatusUnknown;
            exportBusy=exportStatusUnknown||!isTerminalExportState(tab.videoExportState);
            if(reconcileRequired){tab.videoExportReconcileRequired=false;tab.videoExportReconcileBaseline=null;tab.videoExportReconcileBaselineState=null;tab.videoExportReconcileError=null;}
          } else if(reconcileRequired) {
            const previous=jobs.find(job=>job?.id===reconcileBaseline);
            if(previous?.id){tab.videoExportId=previous.id;tab.videoExportState=previous.state||'unknown';}
            else if(reconcileBaseline){tab.videoExportId=reconcileBaseline;tab.videoExportState=tab.videoExportReconcileBaselineState||tab.videoExportState||'unknown';}
            else {tab.videoExportId=null;tab.videoExportState=null;}
            tab.videoExportStatusUnknown=false;tab.videoExportReconcileRequired=false;tab.videoExportReconcileBaseline=null;tab.videoExportReconcileBaselineState=null;tab.videoExportReconcileError=null;
            exportStatusUnknown=false;exportBusy=!!tab.videoExportId&&!isTerminalExportState(tab.videoExportState);
            $('.video-job').textContent='已核对导出历史；没有发现新的导出任务。';
          } else {
            tab.videoExportId=null;tab.videoExportState=null;tab.videoExportStatusUnknown=false;
            exportStatusUnknown=false;exportBusy=false;
            $('.video-job').textContent='暂无导出任务。';
          }
          renderControls();
        }
        if(tab.videoExportId)poll();
      }
      catch(error){
        if(destroyed)return;
        historyLoaded=false;tab.videoExportHistoryChecked=false;tab.videoExportStatusUnknown=true;
        exportStatusUnknown=true;exportBusy=true;renderControls();
        showExportCheckError(`导出历史暂不可用：${error.message}`);
      }
    }
    async function load() {
      if(loading||loaded||destroyed)return;
      const loadSaveGeneration=Number(tab.videoReviewSaveGeneration)||0;
      const loadRevision=tab.videoReviewRevision||revision;
      loading=true;setStatus('正在载入审片记录…');renderControls();
      try {
        const result=await api(url); if(destroyed)return;
        const versionMoved=(Number(tab.videoReviewSaveGeneration||0)!==loadSaveGeneration)||!!(tab.videoReviewRevision&&tab.videoReviewRevision!==loadRevision);
        if(!tab.videoReviewDirty&&!versionMoved){revision=tab.videoReviewRevision=result.revision;review=result.review||emptyReview();if(!Array.isArray(review.markers))review.markers=[];tab.videoReviewDraft=review;savedReview=tab.videoReviewSaved=copy(review);}
        else {
          review=tab.videoReviewDraft||review;
          savedReview=tab.videoReviewSaved||savedReview;
          revision=tab.videoReviewRevision||revision;
        }
        loaded=true;tab.videoReviewOpen=true;render();
        setStatus(result.source_changed?'源视频已更改；已有标记可能偏移，请核对后保存。':tab.videoReviewDirty?'审片记录尚未保存。':'审片记录已载入；原视频保持不变。');
        loadHistory();
      } catch(error) { if(!destroyed){setStatus(`审片记录载入失败：${error.message}`);renderControls();} }
      finally { loading=false;if(!destroyed)renderControls(); }
    }
    async function poll() {
      clearTimeout(pollTimer); if(destroyed)return;
      if(tab.videoExportReconcileRequired){exportSubmitting=false;exportStatusUnknown=true;exportBusy=true;renderControls();showExportCheckError(`无法确认导出请求是否已接受：${tab.videoExportReconcileError||'请核对导出历史后再继续。'}`);return;}
      if(tab.videoExportSubmitting){exportSubmitting=true;exportBusy=true;renderControls();pollTimer=setTimeout(poll,250);return;}
      if(exportSubmitting){exportSubmitting=false;exportStatusUnknown=!!tab.videoExportStatusUnknown||!!tab.videoExportId&&!tab.videoExportState;exportBusy=exportStatusUnknown||!!tab.videoExportId&&!isTerminalExportState(tab.videoExportState);renderControls();}
      if(!tab.videoExportId){if(!historyLoaded)loadHistory();return;}
      if(exportStatusUnknown||!tab.videoExportState)$('.video-job').textContent='正在核对导出任务…';
      else if(!isTerminalExportState(tab.videoExportState))$('.video-job').textContent=({queued:'导出任务已排队',running:'正在导出所选范围',cancelling:'正在取消并清理'})[tab.videoExportState]||'导出任务进行中…';
      const polledId=tab.videoExportId;
      const checkGeneration=++exportCheckGeneration;
      try {
        const job=await api(`/api/video/exports/${polledId}`);if(destroyed)return;
        if(checkGeneration!==exportCheckGeneration)return;
        if(tab.videoExportReconcileRequired)return;
        if(tab.videoExportSubmitting||tab.videoExportId!==polledId){pollTimer=setTimeout(poll,100);return;}
        const node=$('.video-job');node.replaceChildren();
        const label=({queued:'导出任务已排队',running:'正在导出所选范围',cancelling:'正在取消并清理',cancelled:'已取消；原视频不变',done:'导出完成；原视频不变',error:'导出失败'})[job.state]||job.state;
        node.textContent=label+(job.error?` · ${job.error}`:'');
        tab.videoExportState=typeof job.state==='string'?job.state:'unknown';
        tab.videoExportStatusUnknown=false;tab.videoExportHistoryChecked=true;
        exportStatusUnknown=false;exportBusy=!isTerminalExportState(tab.videoExportState);renderControls();
        if(job.download_url&&(window.yingxuNativeStartup||window.yingxuMac)){
          const reveal=document.createElement('button');reveal.type='button';reveal.textContent='在文件夹中查看片段';reveal.addEventListener('click',()=>api(`/api/video/exports/${job.id}/reveal`,{method:'POST',body:{}}).catch(error=>setStatus(error.message)),{signal});node.append(reveal);
        } else if(job.download_url){const link=document.createElement('a');link.href=job.download_url;link.download=job.mode==='frames'?`clip.${job.format}`:job.mode==='copy'?'clip.mkv':'clip.mp4';link.textContent='下载片段';node.append(link);}
        if(exportBusy)pollTimer=setTimeout(poll,700);
      } catch(error) {
        if(destroyed)return;
        if(checkGeneration!==exportCheckGeneration)return;
        if(tab.videoExportReconcileRequired)return;
        if(tab.videoExportSubmitting||tab.videoExportId!==polledId){pollTimer=setTimeout(poll,100);return;}
        tab.videoExportStatusUnknown=true;exportStatusUnknown=true;exportBusy=true;renderControls();
        showExportCheckError(`暂时无法读取任务状态；导出可能仍在运行。${error.message}`);
        pollTimer=setTimeout(poll,2000);
      }
    }
    function startRangePreview() {
      if (isFrameLocked()) { setStatus('请先返回视频，再预览时间范围。');return; }
      if (rangePreview) { stopRangePreview({pause:true,seekOut:true});return; }
      if (!validRange(review,currentDuration())) { setTimeError('请设置视频时长内有效的入点和出点。');return; }
      disableLoopsForRangePreview();
      rangePreview=true;setGuidance('正在播放选中范围；到达出点后会自动暂停。');
      player.seek(review.in);player.play();renderTimeline();
    }
    async function exportRange() {
      if (isFrameLocked() || exportBusy || exportSubmitting || exportStatusUnknown || tab.videoExportSubmitting) { setStatus('请先返回视频并核对或等待当前导出任务。');return; }
      if (!validRange(review,currentDuration())) { setTimeError('请设置视频范围内有效的入点和出点。');return; }
      const previousExportId=tab.videoExportId||null;
      const previousExportState=tab.videoExportState||null;
      exportSubmitting=tab.videoExportSubmitting=true;exportBusy=true;exportStatusUnknown=true;tab.videoExportStatusUnknown=true;
      $('.video-job').textContent='正在提交所选范围的导出任务…';renderControls();
      try {
        const job=await api('/api/video/exports',{method:'POST',body:{source,id:tab.id,start:review.in,end:review.out,mode:$('.review-export-mode').value}});
        tab.videoExportId=job.id;tab.videoExportState=typeof job.state==='string'?job.state:'unknown';
        tab.videoExportStatusUnknown=!job.state;tab.videoExportHistoryChecked=true;
        tab.videoExportReconcileRequired=false;tab.videoExportReconcileBaseline=null;tab.videoExportReconcileBaselineState=null;tab.videoExportReconcileError=null;
        tab.videoExportSubmitting=false;exportSubmitting=false;
        exportStatusUnknown=tab.videoExportStatusUnknown||!isTerminalExportState(tab.videoExportState);
        exportBusy=exportStatusUnknown||!isTerminalExportState(tab.videoExportState);
        if(!destroyed){renderControls();poll();}
      } catch(error){
        tab.videoExportSubmitting=false;tab.videoExportStatusUnknown=true;tab.videoExportHistoryChecked=false;
        tab.videoExportReconcileRequired=true;tab.videoExportReconcileBaseline=previousExportId;tab.videoExportReconcileBaselineState=previousExportState;tab.videoExportReconcileError=error.message;
        exportSubmitting=false;exportStatusUnknown=true;exportBusy=true;historyLoaded=false;
        if(!destroyed){setStatus(error.message);renderControls();showExportCheckError(`无法确认导出请求是否已接受：${error.message}`);}
      }
    }
    function setRangePoint(side,value) {
      const duration=currentDuration(); if(isFrameLocked()||exportBusy||!validDuration(duration)||!Number.isFinite(value)){setTimeError('当前无法修改时间范围。');return false;}
      value=clamp(value,0,duration);
      if(side==='in') {review.in=value;if(Number.isFinite(review.out)&&review.out<=value)review.out=null;}
      else {if(!Number.isFinite(review.in)||value<=review.in){setTimeError('出点必须晚于入点。');return false;}review.out=value;}
      setGuidance('');changed();return true;
    }
    function parseTimecodeField(field) {
      const side=field.dataset.timecode, value=parseTimecode(field.value), duration=currentDuration();
      if(value===null||!validDuration(duration)||value>duration+.001){field.setAttribute('aria-invalid','true');field.setCustomValidity?.('请输入视频时长内的 HH:MM:SS.mmm 时间码。');setTimeError('时间码格式为 时:分:秒.毫秒，且不能超过视频时长。');return;}
      if(setRangePoint(side,value)){field.removeAttribute('aria-invalid');field.setCustomValidity?.('');field.value=player.time(review[side]);}
      else {field.setAttribute('aria-invalid','true');field.setCustomValidity?.('出点必须晚于入点。');}
    }
    function rangeInput(event) {
      const field=event.target.closest('.video-trim-handle');if(!field)return;
      const side=field.classList.contains('trim-handle-in')?'in':'out', duration=currentDuration(), value=clamp(Number(field.value),0,duration);
      if(side==='in') {
        review.in=value;if(!Number.isFinite(review.out))review.out=duration;
        if(review.out<=review.in){if(review.in+RANGE_STEP<=duration)review.out=review.in+RANGE_STEP;else{review.in=Math.max(0,duration-RANGE_STEP);review.out=duration;}}
      } else {
        if(!Number.isFinite(review.in))review.in=0;
        review.out=value;
        if(review.out<=review.in){if(review.in+RANGE_STEP<=duration)review.out=review.in+RANGE_STEP;else{review.in=Math.max(0,duration-RANGE_STEP);review.out=duration;}}
      }
      setGuidance('');changed();renderTimeline({preserveText:true});
    }
    function abortExport() { if(!tab.videoExportId||!exportBusy||tab.videoExportReconcileRequired)return;api(`/api/video/exports/${tab.videoExportId}/cancel`,{method:'POST',body:{}}).then(poll).catch(error=>setStatus(error.message)); }
    function reviewAction(action,event) {
      if(action==='retry-load'){load();return;}
      if(action==='retry-export-status'){
        exportCheckGeneration++;
        exportStatusUnknown=true;tab.videoExportStatusUnknown=true;exportBusy=true;renderControls();
        $('.video-job').textContent='正在重新核对导出任务…';
        if(tab.videoExportReconcileRequired||!tab.videoExportId){historyLoaded=false;loadHistory();}else poll();
        return;
      }
      if(action==='save'){save();return;}
      if(action==='cancel'){abortExport();return;}
      if(action==='export'){exportRange();return;}
      if(action==='preview-range'){startRangePreview();return;}
      if(action==='clear-range'){if(isFrameLocked()||exportBusy){setStatus('请先返回视频再修改剪辑范围。');return;}review.in=null;review.out=null;setTimeError('');changed();return;}
      if(action==='step-back'||action==='step-forward'){
        if(isFrameLocked()){setStatus('请先返回视频，再按时间步进。');return;}
        player.pause();player.seek(player.video.currentTime+(action==='step-back'?-.04:.04));return;
      }
      if(action==='ab'){
        if(isFrameLocked()){setStatus('请先返回视频再操作 A–B 循环。');return;}
        if(!(Number.isFinite(review.a)&&Number.isFinite(review.b)&&review.b>review.a)){setStatus('先设置有效的 A、B 点。');return;}
        abEnabled=!abEnabled;player.setAB(abEnabled?review.a:null,abEnabled?review.b:null);if(abEnabled)player.seek(review.a);renderControls();return;
      }
      if(['a','b'].includes(action)){
        if(isFrameLocked()||exportBusy){setStatus('请先返回视频，再按当前播放时间设置 A–B 点。');return;}
        const point=player.video.currentTime;
        if(action==='b'&&Number.isFinite(review.a)&&point<=review.a){setStatus('B 点必须晚于 A 点。');return;}
        review[action]=point;if(action==='a'&&Number.isFinite(review.b)&&review.b<=review.a)review.b=null;changed();return;
      }
      if(action==='in'||action==='out'){
        if(!setRangePoint(action,player.video.currentTime))return;
        timeline$(action==='in'?'.trim-handle-in':'.trim-handle-out').value=String(review[action]);timeline$(`[data-timecode="${action}"]`).value=player.time(review[action]);return;
      }
      if(action==='mark'){
        if(isFrameLocked()||exportBusy){setStatus('请先返回视频，再添加基于播放时间的标记。');return;}
        const markers=Array.isArray(review.markers)?review.markers:(review.markers=[]);
        if(markers.length>=500){setStatus('最多保存 500 个标记。');return;}
        markers.push({time:player.video.currentTime,note:$('.video-marker-note').value});$('.video-marker-note').value='';changed();return;
      }
      if(action==='seek-marker'||action==='remove-marker'){
        if(isFrameLocked()||exportBusy){setStatus('请先返回视频，再操作时间标记。');return;}
        const index=Number(event.target.closest('[data-marker-index]')?.dataset.markerIndex);
        if(!Number.isInteger(index)||index<0||index>=review.markers.length)return;
        if(action==='seek-marker'){player.seek(review.markers[index].time);return;}
        review.markers.splice(index,1);changed();return;
      }
    }

    workspace.addEventListener('click',event=>{
      const tabButton=event.target.closest('[data-review-tab]');if(tabButton){switchTab(tabButton.dataset.reviewTab);return;}
      const action=event.target.closest('[data-review]')?.dataset.review;if(action)reviewAction(action,event);
    },{signal});
    timeline.addEventListener('click',event=>{const action=event.target.closest('[data-review]')?.dataset.review;if(action)reviewAction(action,event);},{signal});
    timeline.addEventListener('input',rangeInput,{signal});
    timeline.addEventListener('change',event=>{const field=event.target.closest('[data-timecode]');if(field)parseTimecodeField(field);},{signal});
    timeline.addEventListener('keydown',event=>{
      const field=event.target.closest('.trim-timecode');if(!field||event.isComposing||event.ctrlKey||event.metaKey||event.altKey)return;
      if(event.key==='Enter'){event.preventDefault();parseTimecodeField(field);}
    },{signal});
    workspace.addEventListener('keydown',event=>{
      if(event.target.matches?.('.video-marker-note')&&event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();$('[data-review="mark"]').click();}
      if(!event.target.matches?.('[role="tab"]')||!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
      const tabs=Array.from(workspace.querySelectorAll('[data-review-tab]')),index=tabs.indexOf(event.target);
      const next=event.key==='Home'?0:event.key==='End'?tabs.length-1:(index+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;
      event.preventDefault();tabs[next].focus();switchTab(tabs[next].dataset.reviewTab);
    },{signal});
    player.video.addEventListener('timeupdate',()=>{
      $('.review-clock').textContent=player.time(player.video.currentTime);setRangeCSS();
      if(rangePreview){if(player.video.currentTime>=review.out){stopRangePreview({pause:true,seekOut:true});}else if(player.video.currentTime<review.in){player.seek(review.in);}}
    },{signal});
    player.video.addEventListener('durationchange',()=>render(),{signal});
    player.video.addEventListener('loadedmetadata',()=>render(),{signal});
    player.video.addEventListener('pause',()=>{if(rangePreview&&!previewEndPause)stopRangePreview();},{signal});
    player.video.addEventListener('seeking',()=>{if(rangePreview&&(player.video.currentTime<review.in||player.video.currentTime>review.out))stopRangePreview();},{signal});
    player.screen.addEventListener('keydown',event=>{
      if(!loaded||isFrameLocked()||event.defaultPrevented||event.isComposing||event.ctrlKey||event.metaKey||event.altKey||event.target.closest('input,textarea,select,button,[contenteditable]'))return;
      const action={i:'in',o:'out'}[event.key.toLowerCase()];if(action){event.preventDefault();event.stopPropagation();reviewAction(action,event);}
    },{signal});

    function attachInspector(root) {
      if(!root||destroyed)return workspace;
      if(root===player.shell){workspace.classList.remove('video-review-docked');if(workspace.parentElement!==root)root.append(workspace);return workspace;}
      workspace.classList.add('video-review-docked');
      const current = Array.from(root.childNodes||[]).filter(node=>node!==workspace);
      if(current.length){filePanel.replaceChildren(...current);}
      if(workspace.parentElement!==root)root.replaceChildren(workspace);
      else if(root.childNodes.length!==1||root.firstChild!==workspace)root.replaceChildren(workspace);
      return workspace;
    }

    const framesHost=$('[data-review-panel="frames"]');
    frames=window.YingXuVideoFrames?.mount(player,{
      tab,api,container:framesHost,
      onEnter:()=>{abEnabled=false;player.setAB(null,null);switchTab('frames');renderControls();},
      onStateChange:value=>{frameState=value||frameState;if(frameState.inspecting)switchTab('frames');renderControls();}
    })||null;
    render();switchTab(tab.videoReviewWorkspaceTab||'trim');
    const inspectorRoot=inspector||player.shell;attachInspector(inspectorRoot);
    tab.videoReviewOpen=true;
    load();

    return {
      tabKey:tab.key,
      isInspecting:()=>isFrameLocked(),
      attachInspector,
      destroy(){
        if(destroyed)return;destroyed=true;clearTimeout(pollTimer);stopRangePreview();frames?.destroy();controller.abort();timeline.remove();workspace.remove();
        if(tab.videoReviewRenderControls===renderControls)tab.videoReviewRenderControls=null;
      }
    };
  }

  window.YingXuVideoReview={mount,parseTimecode};
})();
