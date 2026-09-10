const $=s=>document.querySelector(s);
const queue=$('#queue'),mainInput=$('#mainInput'),analyzeBtn=$('#analyzeBtn'),renderBtn=$('#renderBtn'),activity=$('#activity'),errorBanner=$('#errorBanner'),drawer=$('#drawer'),drawerBackdrop=$('#drawerBackdrop'),drawerBody=$('#drawerBody'),drawerTitle=$('#drawerTitle'),modalBackdrop=$('#modalBackdrop'),modalTitle=$('#modalTitle'),modalEyebrow=$('#modalEyebrow'),modalBody=$('#modalBody');
let state=null,filter='all',selectedItemId=null,sync=null;

async function api(path,options={}){const r=await fetch(path,{headers:{'Content-Type':'application/json',...(options.headers||{})},...options});if(!r.ok){let m=`HTTP ${r.status}`;try{const b=await r.json();m=b.detail||m}catch(_){}throw new Error(m)}return r.json()}
const labels={ready:'READY',needs_sync:'싱크 확인',needs_translation_review:'의미 확인',processing:'처리 중',done:'완료',failed:'실패',excluded:'제외'};
const modes={synced:'Synced LRC',plain:'Plain · Auto Sync',lyricless:'가사 없음'};
const languages={korean:'한국어',mixed:'한영 혼용',english:'영어-only',lyricless:'가사 없음',other:'기타',unknown:'자동 판별'};
const esc=v=>String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const review=i=>['needs_sync','needs_translation_review'].includes(i.status);
const visible=i=>filter==='all'||(filter==='review'?review(i):i.status===filter);
function action(i){if(i.status==='needs_sync')return'싱크 보정';if(i.status==='needs_translation_review')return`의미 확인 ${i.translation_issue_count||''}`;if(i.status==='failed')return'다시 준비';if(i.status==='processing')return`${i.progress}%`;if(i.lyrics_mode==='lyricless'&&i.status==='ready')return'가사 없이 렌더';return'상세'}

function renderState(){
  if(!state)return;
  const items=state.items||[],c=state.counts||{};
  $('#countAll').textContent=items.length;$('#countReady').textContent=c.ready||0;$('#countReview').textContent=(c.needs_sync||0)+(c.needs_translation_review||0);$('#countDone').textContent=c.done||0;$('#countFailed').textContent=c.failed||0;
  activity.textContent=state.activity||'대기 중';analyzeBtn.disabled=state.analysis_busy||state.render_busy;analyzeBtn.textContent=state.analysis_busy?'분석 중…':'분석 시작';renderBtn.disabled=state.analysis_busy||state.render_busy||!(c.ready>0);renderBtn.textContent=state.render_busy?'처리 중…':`READY ${c.ready||0}곡 처리`;
  errorBanner.classList.toggle('hidden',!state.last_error);errorBanner.textContent=state.last_error||'';
  const shown=items.filter(visible);
  if(!shown.length){queue.className='queue empty-state';queue.textContent=items.length?'이 필터에 해당하는 곡이 없습니다.':'입력 후 분석을 시작하세요.';return}
  queue.className='queue';
  queue.innerHTML=shown.map(i=>`<div class="track" data-id="${i.id}"><div class="track-index">${String(items.indexOf(i)+1).padStart(2,'0')}</div><div><div class="track-title">${esc(i.label)}</div><div class="track-sub"><span class="pill ${i.status}">${labels[i.status]||i.status}</span><span>${modes[i.lyrics_mode]||i.lyrics_mode}</span><span>· ${languages[i.language_mode]||i.language_mode}</span>${i.reason?`<span>· ${esc(i.reason)}</span>`:''}</div>${i.status==='processing'?`<div class="progressline"><i style="width:${i.progress}%"></i></div>`:''}</div><button class="ghost context-action" data-action="context" data-id="${i.id}">${action(i)}</button></div>`).join('');
  if(selectedItemId)refreshDrawer();
}
async function refresh(){try{state=await api('/api/state');renderState()}catch(e){console.error(e)}}
function showError(m){errorBanner.textContent=m;errorBanner.classList.remove('hidden')}
async function startAnalyze(){const input=mainInput.value.trim();if(!input)return mainInput.focus();try{await api('/api/analyze',{method:'POST',body:JSON.stringify({input,output_mode:'video'})});await refresh()}catch(e){showError(e.message)}}
async function startRender(){try{await api('/api/render',{method:'POST'});await refresh()}catch(e){showError(e.message)}}

function openDrawer(id){selectedItemId=id;drawer.classList.add('open');drawerBackdrop.classList.remove('hidden');refreshDrawer()}
function closeDrawer(){selectedItemId=null;drawer.classList.remove('open');drawerBackdrop.classList.add('hidden')}
function refreshDrawer(){
  if(!state||!selectedItemId)return;const i=state.items.find(x=>x.id===selectedItemId);if(!i)return closeDrawer();drawerTitle.textContent=i.label;
  const optionalLyrics=i.lyrics_mode==='lyricless'?'<button class="ghost" data-drawer="lyrics">가사 직접 추가</button>':'';
  drawerBody.innerHTML=`<div class="detail-block"><div class="detail-label">상태</div><div class="detail-value"><span class="pill ${i.status}">${labels[i.status]||i.status}</span> · ${modes[i.lyrics_mode]||i.lyrics_mode} · ${languages[i.language_mode]||i.language_mode}</div></div>${i.reason?`<div class="detail-block"><div class="detail-label">처리 메모</div><div class="detail-value">${esc(i.reason)}</div></div>`:''}${i.progress_text?`<div class="detail-block"><div class="detail-label">현재 작업</div><div class="detail-value">${esc(i.progress_text)} ${i.progress||0}%</div></div>`:''}${i.result_path?`<div class="detail-block"><div class="detail-label">결과</div><div class="detail-value">${esc(i.result_path)}</div></div>`:''}<div class="drawer-actions">${i.status==='needs_sync'?'<button class="primary" data-drawer="sync">싱크 보정</button>':''}${i.status==='needs_translation_review'?'<button class="primary" data-drawer="translation">애매한 의미 확인</button>':''}${i.status==='failed'?'<button class="primary" data-drawer="include">다시 READY로</button>':''}${optionalLyrics}${i.status==='excluded'?'<button class="primary" data-drawer="include">다시 포함</button>':'<button class="ghost" data-drawer="exclude">이 곡 제외</button>'}</div>`;
}
function openModal(t,e,h){modalTitle.textContent=t;modalEyebrow.textContent=e;modalBody.innerHTML=h;modalBackdrop.classList.remove('hidden')}
function closeModal(){modalBackdrop.classList.add('hidden');sync=null}

async function openLyricsEditor(i){
  try{const d=await api(`/api/items/${i.id}/lyrics`);openModal(i.label,'LYRICS',`<p class="microcopy" style="margin:14px 0">가사 없음도 정상적으로 렌더됩니다. 정말 가사를 넣고 싶을 때만 여기에 plain lyric 또는 LRC를 붙여넣으세요.</p><textarea id="lyricsText" placeholder="가사를 줄 단위로 붙여넣기">${esc(d.text||'')}</textarea><div class="modal-actions"><button class="ghost" id="cancelLyrics">취소</button><button class="primary" id="saveLyrics">저장 · 자동 처리</button></div>`);$('#cancelLyrics').onclick=closeModal;$('#saveLyrics').onclick=async()=>{const text=$('#lyricsText').value.trim();if(!text)return;try{await api(`/api/items/${i.id}/lyrics`,{method:'POST',body:JSON.stringify({text})});closeModal();closeDrawer();await refresh()}catch(e){showError(e.message)}}}catch(e){showError(e.message)}
}

async function openSyncEditor(id){
  const i=state.items.find(x=>x.id===id);if(!i)return;openModal(i.label,'TAP SYNC','<div class="empty-state" style="min-height:180px">싱크용 음원을 준비하는 중…</div>');
  try{const d=await api(`/api/items/${i.id}/sync/prepare`,{method:'POST'});sync={itemId:id,points:d.points,selected:0};modalBody.innerHTML=`<div class="sync-layout"><div class="player-panel"><audio id="syncAudio" controls preload="auto" src="${d.audio_url}?t=${Date.now()}"></audio><div class="nudge"><button class="ghost" id="minusTime">−0.10s</button><button class="primary" id="tapNow">현재 줄 찍기 · Space</button><button class="ghost" id="plusTime">+0.10s</button></div><div class="sync-help">AI 자동 싱크가 먼저 시도된 결과입니다. 어긋나는 지점만 고치면 됩니다.<br>Space: 현재 재생 위치 기록+다음 줄 · ↑/↓: 줄 이동 · ←/→: ±0.10초</div><div class="modal-actions"><button class="primary" id="saveSync">저장하고 READY</button></div></div><div class="line-list" id="syncLines"></div></div>`;drawSync();$('#tapNow').onclick=tap;$('#minusTime').onclick=()=>nudge(-.1);$('#plusTime').onclick=()=>nudge(.1);$('#saveSync').onclick=saveSync}catch(e){closeModal();showError(e.message)}
}
function fmt(sec){const m=Math.floor(sec/60),s=Math.max(0,sec-m*60);return`${String(m).padStart(2,'0')}:${s.toFixed(2).padStart(5,'0')}`}
function drawSync(){if(!sync)return;const l=$('#syncLines');l.innerHTML=sync.points.map((p,n)=>`<div class="sync-line ${n===sync.selected?'active':''}" data-sync-index="${n}"><div class="sync-time">${fmt(p.time)}</div><div class="sync-text">${esc(p.text)}</div></div>`).join('');l.querySelectorAll('.sync-line').forEach(el=>el.onclick=()=>{sync.selected=Number(el.dataset.syncIndex);drawSync()});l.querySelector('.active')?.scrollIntoView({block:'nearest'})}
function tap(){if(!sync)return;sync.points[sync.selected].time=$('#syncAudio').currentTime;if(sync.selected<sync.points.length-1)sync.selected++;drawSync()}
function nudge(d){if(!sync)return;sync.points[sync.selected].time=Math.max(0,sync.points[sync.selected].time+d);drawSync()}
async function saveSync(){if(!sync)return;try{await api(`/api/items/${sync.itemId}/sync/save`,{method:'POST',body:JSON.stringify({points:sync.points})});closeModal();closeDrawer();await refresh()}catch(e){showError(e.message)}}

async function openTranslationReview(i){
  try{const d=await api(`/api/items/${i.id}/translation-review`);if(!d.issues?.length){showError('확인할 구절이 없습니다.');return}
    openModal(i.label,'MEANING REVIEW',`<p class="microcopy" style="margin:14px 0">Luna → Terra → Sol까지 자동 검토했는데도 의미가 갈리는 구절만 남았습니다. 최종 영어를 쓸 필요 없이, 이 구절이 무슨 뜻인지 한국어로 짧게 힌트만 적어주세요.</p><div class="meaning-list">${d.issues.map(x=>`<div class="meaning-item"><div class="detail-label">원문 ${Number(x.index)+1}</div><div class="meaning-source">${esc(x.source)}</div><div class="detail-label">현재 번역</div><div class="meaning-current">${esc(x.translated)}</div><div class="detail-label">AI 질문</div><div class="meaning-question">${esc(x.question)}</div><textarea class="meaning-hint" data-index="${x.index}" placeholder="예: 여기서 ‘세종/사임당’은 지폐, 즉 돈을 뜻함"></textarea></div>`).join('')}</div><div class="modal-actions"><button class="primary" id="applyMeaningHints">힌트 반영해 재번역</button></div>`);
    $('#applyMeaningHints').onclick=async()=>{const hints={};document.querySelectorAll('.meaning-hint').forEach(el=>{if(el.value.trim())hints[el.dataset.index]=el.value.trim()});if(!Object.keys(hints).length)return;try{await api(`/api/items/${i.id}/translation-review`,{method:'POST',body:JSON.stringify({hints})});closeModal();closeDrawer();await refresh()}catch(e){showError(e.message)}};
  }catch(e){showError(e.message)}
}

function openSettings(){
  const routing=state?.model_routing||{};
  openModal('설정','AUTOMATIC MODEL ROUTING',`<div class="form-row"><label>OpenAI API Key</label><input id="apiKeyInput" type="password" placeholder="${state?.api_key_configured?'설정됨 · 변경할 때만 입력':'sk-…'}" /></div><div class="routing-card"><div><strong>일반 번역</strong><span>${esc(routing.translation||'GPT-5.6 Luna')}</span></div><div><strong>어려운 구절</strong><span>${esc(routing.difficult_translation||'GPT-5.6 Terra')}</span></div><div><strong>최종 애매성</strong><span>${esc(routing.final_ambiguity||'GPT-5.6 Sol')}</span></div><div><strong>자동 싱크</strong><span>${esc(routing.auto_sync_transcription||'GPT-Transcribe')} + ${esc(routing.auto_sync_alignment||'GPT-5.6 Luna')}</span></div></div><p class="microcopy" style="margin-top:14px">모델은 자동으로 선택됩니다. 영어-only/가사 없음은 번역 API를 호출하지 않고, 비싼 모델은 애매한 일부 구절에만 사용합니다.</p><div class="modal-actions"><button class="primary" id="saveSettings">저장</button></div>`);
  $('#saveSettings').onclick=async()=>{const api_key=$('#apiKeyInput').value.trim()||null;try{await api('/api/settings',{method:'POST',body:JSON.stringify({api_key})});closeModal();await refresh()}catch(e){showError(e.message)}};
}

queue.addEventListener('click',async e=>{const t=e.target.closest('[data-id]');if(!t)return;const i=state.items.find(x=>x.id===t.dataset.id);if(!i)return;if(e.target.dataset.action==='context'){if(i.status==='needs_sync')return openSyncEditor(i.id);if(i.status==='needs_translation_review')return openTranslationReview(i);if(i.status==='failed'){await api(`/api/items/${i.id}/include`,{method:'POST'});return refresh()}}openDrawer(i.id)});
drawerBody.addEventListener('click',async e=>{const a=e.target.dataset.drawer;if(!a||!selectedItemId)return;const i=state.items.find(x=>x.id===selectedItemId);if(!i)return;if(a==='lyrics')return openLyricsEditor(i);if(a==='sync')return openSyncEditor(i.id);if(a==='translation')return openTranslationReview(i);try{if(a==='exclude')await api(`/api/items/${i.id}/exclude`,{method:'POST'});if(a==='include')await api(`/api/items/${i.id}/include`,{method:'POST'});await refresh()}catch(err){showError(err.message)}});
document.querySelectorAll('.stat').forEach(b=>b.onclick=()=>{document.querySelectorAll('.stat').forEach(x=>x.classList.remove('active'));b.classList.add('active');filter=b.dataset.filter;renderState()});
analyzeBtn.onclick=startAnalyze;mainInput.addEventListener('keydown',e=>{if(e.key==='Enter')startAnalyze()});renderBtn.onclick=startRender;$('#settingsBtn').onclick=openSettings;$('#openOutput').onclick=()=>api('/api/open-output',{method:'POST'}).catch(e=>showError(e.message));$('#shutdownBtn').onclick=async()=>{try{await api('/api/shutdown',{method:'POST'});document.body.innerHTML='<div class="empty-state" style="height:100vh">Lyric Video Maker가 종료되었습니다. 이 탭을 닫아도 됩니다.</div>'}catch(_){}};$('#closeDrawer').onclick=closeDrawer;drawerBackdrop.onclick=closeDrawer;$('#modalClose').onclick=closeModal;modalBackdrop.addEventListener('click',e=>{if(e.target===modalBackdrop)closeModal()});
document.addEventListener('keydown',e=>{if(!sync||modalBackdrop.classList.contains('hidden')||['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName))return;if(e.code==='Space'){e.preventDefault();tap()}else if(e.key==='ArrowUp'){e.preventDefault();sync.selected=Math.max(0,sync.selected-1);drawSync()}else if(e.key==='ArrowDown'){e.preventDefault();sync.selected=Math.min(sync.points.length-1,sync.selected+1);drawSync()}else if(e.key==='ArrowLeft'){e.preventDefault();nudge(-.1)}else if(e.key==='ArrowRight'){e.preventDefault();nudge(.1)}});
refresh();setInterval(refresh,1100);
