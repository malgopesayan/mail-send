'use strict';
const $ = s => document.querySelector(s), $$ = s => [...document.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let ME = null, JOBS = [], SHOTS = [], filt = {s:'all', p:'', q:'', page:1}, view = 'dash';
let CHAT = [], CHAT_BUSY = false;
let lastSeq = 0, RUNNING = false, RUNMODE = '', STOPPING = false, QUEUED = 0, pollT = null, curJob = null;
const PAGE = 25;

/* ---------- theme + logo ---------- */
const SUN = '<svg class="i-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4 12H2M22 12h-2M5 5l1.5 1.5M17.500 17.500L19 19M5 19l1.5-1.5M17.500 6.500L19 5"/></svg>';
const MOON = '<svg class="i-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 13A9 9 0 1111 3a7 7 0 0010 10z"/></svg>';
$$('[data-theme-toggle]').forEach(b => { b.innerHTML = SUN + MOON; b.onclick = () => {
  const t = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = t; try { localStorage.setItem('jm-theme', t); } catch(e){} }; });
$$('[data-logo]').forEach(l => l.append($('#logo-t').content.cloneNode(true)));
(() => { const img = new Image();            // your own static/logo.png|svg replaces the drawn mark
  img.onload = () => $$('[data-logo] .face').forEach(f => { const i = new Image(); i.src = '/logo'; i.alt = ''; f.replaceChildren(i); });
  img.src = '/logo'; })();

/* ---------- toast / busy ---------- */
function toast(msg, kind=''){ const d=document.createElement('div'); d.className='toast '+kind; d.textContent=msg; $('#toasts').append(d); setTimeout(()=>d.remove(), kind==='bad'?7000:3800); }
async function busy(btn, fn){ if(btn.disabled) return; const h=btn.innerHTML; btn.disabled=true; btn.innerHTML='<span class="spin"></span>'; try{ return await fn(); } finally{ btn.disabled=false; btn.innerHTML=h; } }

/* ---------- firebase ---------- */
let auth = null;
if (!window.FIREBASE_CONFIG || !window.FIREBASE_CONFIG.apiKey) {
  $('#login').hidden = false; $('#login-err').textContent = 'Server is missing FIREBASE_WEB_CONFIG_JSON — see README step 3.';
} else {
  firebase.initializeApp(window.FIREBASE_CONFIG); auth = firebase.auth();
  auth.onAuthStateChanged(async u => { if (u) await boot(u); else { ME=null; stopPoll(); resetChat(); $('#chat').hidden=true; $('#app').hidden=true; $('#login').hidden=false; } });
}
$('#btn-google').onclick = async () => {
  $('#login-err').textContent = '';
  const p = new firebase.auth.GoogleAuthProvider(); p.setCustomParameters({prompt:'select_account'});
  try { await auth.signInWithPopup(p); }
  catch(e){ if (['auth/popup-blocked','auth/operation-not-supported-in-this-environment'].includes(e.code)) return auth.signInWithRedirect(p);
            if (e.code!=='auth/popup-closed-by-user' && e.code!=='auth/cancelled-popup-request') $('#login-err').textContent = e.message; }
};
$('#btn-out').onclick = () => auth.signOut();

async function api(path, opt={}) {
  const u = auth.currentUser; if(!u) throw new Error('Not signed in');
  const h = {'Authorization':'Bearer '+await u.getIdToken(), ...(opt.headers||{})};
  let body = opt.body;
  if (body && !(body instanceof FormData) && typeof body !== 'string') { body = JSON.stringify(body); h['Content-Type']='application/json'; }
  const r = await fetch(path, {...opt, headers:h, body});
  if (r.status === 401 && !(await r.clone().json().catch(()=>({}))).detail?.needs_gmail) { auth.signOut(); throw new Error('Session expired'); }
  const data = await r.json().catch(()=>({}));
  if (!r.ok) { const d = data.detail; const e = new Error(typeof d==='string' ? d : (d?.message || JSON.stringify(d) || r.statusText)); e.status=r.status; e.detail=d; throw e; }
  return data;
}

async function boot(user){
  try { ME = await api('/api/me'); }
  catch(e){ $('#login').hidden=false; $('#app').hidden=true; $('#login-err').textContent = e.message; if(auth) auth.signOut(); return; }
  $('#login').hidden = true; $('#app').hidden = false;
  const av=$('#avatar'); av.onerror=()=>{av.style.visibility='hidden'}; av.style.visibility='visible'; if(user.photoURL) av.src=user.photoURL; else av.style.visibility='hidden'; $('#m-name').textContent = ME.name||''; $('#m-email').textContent = ME.email||'';
  fillSettings(); renderSetup();
  const q = new URLSearchParams(location.search);
  if (q.get('gmail')) { show('settings'); q.get('gmail')==='connected' ? toast('Gmail connected 🎉','good') : toast('Gmail connection failed: '+(q.get('msg')||'unknown'),'bad'); history.replaceState({},'','/'); }
  await Promise.all([loadJobs(), loadShots()]); loadRuns(); loadSched(); loadQueue(); startPoll(true);
}

/* ---------- navigation ---------- */
function show(v){ view=v; $$('.view').forEach(x=>x.classList.toggle('on', x.id==='v-'+v)); $$('#tabs button').forEach(b=>b.classList.toggle('on', b.dataset.v===v));
  if(v==='console') loadRuns(); if(v==='shots') loadShots(); if(v==='sched') loadSched(); if(v==='insights') loadInsights(); if(v==='board') renderBoard(); window.scrollTo(0,0); }
$('#tabs').onclick = e => { const b=e.target.closest('button'); if(b) show(b.dataset.v); };
$('#avatar').onclick = e => { e.stopPropagation(); $('#menu').hidden = !$('#menu').hidden; };
document.addEventListener('click', e => { if(!e.target.closest('#menu')) $('#menu').hidden = true; });

/* ---------- setup checklist ---------- */
function renderSetup(){
  const k=ME.keys, g=ME.gmail;
  const items=[
    {ok:k.groq.length>0||!!k.nvidia||k.nvidia_server_key, t:'Add a Groq or NVIDIA API key', s:'Reads screenshots & writes emails', go:'settings'},
    {ok:g.connected, t:'Connect Gmail', s: g.connected?g.email:'Mails are sent from your account', go:'settings'},
    {ok:!!ME.resume, t:'Upload your resume', s:'Attached to every mail', go:'settings'},
    {ok:ME.profile_text.trim().length>100, t:'Build your experience profile', s:'So emails sound like you', go:'settings'},
    {ok:!!k.harvest, t:'Add HarvestAPI key (optional)', s:'Only needed for LinkedIn search', go:'settings'},
  ];
  const left = items.filter(i=>!i.ok).length;
  $('#setup').hidden = left===0;
  $('#setup-n').textContent = `${items.length-left}/${items.length} done`;
  $('#steps').innerHTML = items.map((i,n)=>`<div class="step ${i.ok?'ok':''}" data-go="${i.go}"><i>${i.ok?'✓':n+1}</i><div>${esc(i.t)}<small>${esc(i.s)}</small></div></div>`).join('');
}
$('#steps').onclick = e => { const s=e.target.closest('.step'); if(s) show(s.dataset.go); };

/* ---------- jobs ---------- */
async function loadJobs(){ try{ JOBS = await api('/api/jobs'); }catch(e){ toast(e.message,'bad'); } renderJobs(); }
function renderJobs(){
  const unsent = JOBS.filter(j=>j.email_status!=='Sent' && j.recruiter_email && !j.hold).length;
  const sent = JOBS.filter(j=>j.email_status==='Sent').length;
  const hi = JOBS.filter(j=>j.priority==='High').length;
  const avg = JOBS.length ? Math.round(JOBS.reduce((a,j)=>a+(+j.job_match_score||0),0)/JOBS.length) : 0;
  const held = JOBS.filter(j=>j.hold && j.recruiter_email).length;
  renderStats([['Total jobs',JOBS.length,'','--a1'],['Ready to send',unsent,'','--a2'],['Held for review',held,'','--warn'],['Sent',sent,'','--green'],['High priority',hi,'','--hot'],['Avg match',avg,'%','--a1']]);
  $('#a-send').dataset.n = unsent;
  renderRunButtons();

  const q = filt.q.toLowerCase();
  let rows = JOBS.filter(j => (filt.s==='all' || (filt.s==='held' ? !!j.hold : (filt.s==='Sent') === (j.email_status==='Sent'))) &&
    (!filt.p || j.priority===filt.p) &&
    (!q || [j.company,j.job_title,j.recruiter_name,j.recruiter_email,j.location].join(' ').toLowerCase().includes(q)));
  const pages = Math.max(1, Math.ceil(rows.length/PAGE)); if(filt.page>pages) filt.page=pages;
  const slice = rows.slice((filt.page-1)*PAGE, filt.page*PAGE);
  $('#jobs').innerHTML = slice.length ? slice.map(j=>{
    const pr = (j.priority||'').toLowerCase(); const pc = pr.startsWith('review')?'review':pr||'low';
    const sc = Math.max(0,Math.min(100,+j.job_match_score||0));
    return `<tr data-id="${esc(j.id)}">
      <td><div class="co">${esc(j.company||'—')}</div><div class="sub">${esc(j.job_title||'—')}${j.location?' · '+esc(j.location):''}${j.source==='linkedin'?' · <span class="mono">in</span>':''}</div></td>
      <td class="hide-m"><div>${esc(j.recruiter_name||'—')}</div><div class="sub">${j.recruiter_email?esc(j.recruiter_email):j.apply_url?'<span class="mono">apply link</span>':'<span class="err">no email</span>'}</div></td>
      <td><div class="score"><span>${sc}</span><div class="bar"><i style="width:${sc}%"></i></div></div></td>
      <td class="hide-m"><span class="badge b-${esc(pc)}">${esc(j.priority||'—')}</span></td>
      <td><span class="badge ${j.email_status==='Sent'?'b-sent':j.hold?'b-held':'b-not'}" title="${esc(j.hold||'')}">${j.email_status==='Sent'?(j.stage&&j.stage!=='sent'?esc(STAGE_LABEL[j.stage]):'Sent'+(j.sent_date?' · '+esc(j.sent_date.slice(5)):'')):j.hold?'Held':j.recruiter_email?'Ready':j.apply_url?'To apply':'No email'}</span>${j.send_error&&j.email_status!=='Sent'?'<div class="err" title="'+esc(j.send_error)+'">send failed</div>':''}</td>
      <td><div class="rowact">${j.email_status==='Sent'||!j.recruiter_email?'':'<button class="btn sm" data-send="'+esc(j.id)+'">Send</button>'}</div></td></tr>`;
  }).join('') : `<tr><td colspan="6"><div class="empty"><b>${JOBS.length?'No jobs match your filters':'No jobs yet'}</b>${JOBS.length?'':'Upload screenshots or run a LinkedIn search to get started.'}</div></td></tr>`;
  onJobs();
  $('#pager').innerHTML = `<span>${rows.length} job(s)</span><span class="row"><button class="btn sm ghost" data-pg="-1" ${filt.page<=1?'disabled':''}>‹ Prev</button>${filt.page}/${pages}<button class="btn sm ghost" data-pg="1" ${filt.page>=pages?'disabled':''}>Next ›</button></span>`;
}
$('#f-status').onclick = e => { const b=e.target.closest('button'); if(!b) return; filt.s=b.dataset.s; filt.page=1; $$('#f-status button').forEach(x=>x.classList.toggle('on',x===b)); renderJobs(); };
$('#f-prio').onchange = e => { filt.p=e.target.value; filt.page=1; renderJobs(); };
$('#f-q').oninput = e => { filt.q=e.target.value; filt.page=1; renderJobs(); };
$('#pager').onclick = e => { const b=e.target.closest('[data-pg]'); if(b){ filt.page+=+b.dataset.pg; renderJobs(); } };
$('#jobs').onclick = async e => {
  const sb = e.target.closest('[data-send]');
  if (sb) { e.stopPropagation(); return sendOne(sb.dataset.send, sb); }
  const tr = e.target.closest('tr[data-id]'); if(tr) openJob(tr.dataset.id);
};
async function sendOne(id, btn){
  const job = JOBS.find(x=>x.id===id);
  if (job?.hold && !confirm(`This job is held: ${job.hold}\n\nSend it to ${job.recruiter_email} anyway?`)) return false;
  return busy(btn, async()=>{
    try { const j = await api(`/api/jobs/${id}/send`, {method:'POST'}); JOBS = JOBS.map(x=>x.id===id?j:x); renderJobs(); toast('Sent to '+j.recruiter_email,'good'); return true; }
    catch(e){ if(e.detail?.needs_gmail){ toast('Connect Gmail first','bad'); show('settings'); } else toast(e.message,'bad'); loadJobs(); return false; }
  });
}

/* job modal */
function openJob(id){ const j=JOBS.find(x=>x.id===id); if(!j) return; curJob=j;
  $('#jm-title').textContent = (j.company||'Job')+' — '+(j.job_title||'');
  $('#j-company').value=j.company||''; $('#j-title').value=j.job_title||''; $('#j-rname').value=j.recruiter_name||''; $('#j-remail').value=j.recruiter_email||'';
  $('#j-subject').value=j.email_subject||''; $('#j-body').value=j.email_body||'';
  $('#j-meta').textContent = `Score ${j.job_match_score||0} · ${j.source||''}${j.linkedin_url?' · '+j.linkedin_url:''}${j.sent_date?' · sent '+j.sent_date:''}`;
  $('#j-mark').textContent = j.email_status==='Sent' ? 'Mark as not sent' : 'Mark as sent';
  $('#j-send').textContent = j.email_status==='Sent' ? 'Send again' : 'Save & send';
  const held = j.email_status!=='Sent' && j.hold;
  $('#j-hold').hidden = !held; $('#j-hold').textContent = held ? 'Held — will not be sent automatically: '+j.hold : '';
  $('#j-release').hidden = !held;
  onOpenJob(j);
  $('#jm').hidden=false; }
const closeJob = () => { $('#jm').hidden=true; curJob=null; };
$('#jm-x').onclick = closeJob; $('#jm').onclick = e => { if(e.target.id==='jm') closeJob(); };
const jobBody = () => ({company:$('#j-company').value, job_title:$('#j-title').value, recruiter_name:$('#j-rname').value, recruiter_email:$('#j-remail').value.trim(), email_subject:$('#j-subject').value, email_body:$('#j-body').value, resume_id:$('#j-resume').value, notes:$('#j-notes').value});
async function saveJob(extra={}){ const j = await api('/api/jobs/'+curJob.id,{method:'PATCH',body:{...jobBody(),...extra}}); JOBS=JOBS.map(x=>x.id===j.id?j:x); curJob=j; renderJobs(); return j; }
$('#j-release').onclick = e => busy(e.currentTarget, async()=>{ try{ await saveJob({reviewed:true}); toast('Allowed — it will go with the next send','good'); closeJob(); }catch(x){ toast(x.message,'bad'); } });
$('#j-save').onclick = e => busy(e.currentTarget, async()=>{ try{ await saveJob(); toast('Saved','good'); closeJob(); }catch(x){ toast(x.message,'bad'); } });
$('#j-mark').onclick = e => busy(e.currentTarget, async()=>{ try{ await saveJob({email_status: curJob.email_status==='Sent'?'Not Sent':'Sent'}); closeJob(); }catch(x){ toast(x.message,'bad'); } });
$('#j-send').onclick = e => busy(e.currentTarget, async()=>{ try{ await saveJob(); const id=curJob.id; closeJob(); await sendOne(id, document.createElement('button')); }catch(x){ toast(x.message,'bad'); } });
$('#j-del').onclick = async () => { if(!confirm('Delete this job?')) return; await api('/api/jobs/'+curJob.id,{method:'DELETE'}); JOBS=JOBS.filter(x=>x.id!==curJob.id); closeJob(); renderJobs(); };

/* ---------- runs: the same button starts and stops ---------- */
const RUN_LABEL = {search:'Start search', resume:'Continue checking', send_only:'Send all unsent', process:'Read screenshots', both:'Run everything', boards:'Check company boards'};
async function startRun(mode){
  try{ await api('/api/run',{method:'POST',body:{mode}}); lastSeq=0; $('#console').innerHTML=''; toast('Started — watch the Console tab'); setRunning(true, mode, false); startPoll(true); }
  catch(e){ toast(e.message,'bad'); }
}
async function stopRun(){
  try{ STOPPING = true; renderRunButtons(); await api('/api/run/stop',{method:'POST'}); toast('Stopping after the current item…'); startPoll(true); }
  catch(e){ STOPPING = false; renderRunButtons(); toast(e.message,'bad'); }
}
function confirmRun(mode){
  if (mode==='both') return confirm('Search LinkedIn, read screenshots AND send every ready mail?');
  if (mode!=='send_only') return true;
  const n = +$('#a-send').dataset.n;
  if (!ME.gmail.connected) { toast('Connect Gmail first','bad'); show('settings'); return false; }
  if (!n) { toast('Nothing is ready to send.'); return false; }
  return confirm(`Send ${n} email(s) from ${ME.gmail.email||'your Gmail'}${ME.resume?' with your resume attached':' (no resume uploaded!)'}?\n\nUp to ${ME.settings.max_send_per_run} are sent per run. Held jobs are skipped.`);
}
$$('[data-run]').forEach(btn => btn.onclick = () => {
  if (RUNNING) return stopRun();
  if (confirmRun(btn.dataset.run)) startRun(btn.dataset.run);
});
$('#c-stop').onclick = stopRun; $('#a-stop-other').onclick = stopRun;

function renderRunButtons(){
  let owned = false;
  $$('[data-run]').forEach(b => {
    const mode = b.dataset.run, mine = RUNNING && RUNMODE===mode;
    let label = RUN_LABEL[mode];
    if (mode==='send_only' && +b.dataset.n) label += ` (${b.dataset.n})`;
    if (mode==='process') label = b.id==='shot-run' ? 'Read screenshots now' : label + (SHOTS.length ? ` (${SHOTS.length})` : '');
    if (mine) { owned = true; label = STOPPING ? 'Stopping…' : (mode==='search' ? '■ Stop search' : '■ Stop'); }
    b.textContent = label;
    b.classList.toggle('stop', mine);
    b.disabled = RUNNING && (!mine || STOPPING);
  });
  const searching = RUNNING && (RUNMODE==='search' || RUNMODE==='resume');
  $('#hero').classList.toggle('live', searching);
  $('#hero-title').textContent = searching ? (RUNMODE==='resume' ? 'Checking saved posts…' : 'Searching LinkedIn…') : 'LinkedIn job search';
  // runs started by a schedule or the assistant have no button of their own
  $('#a-stop-other').hidden = !(RUNNING && !owned); $('#a-stop-other').disabled = STOPPING;
  $('#c-stop').hidden = !RUNNING; $('#c-stop').disabled = STOPPING;
}
function setRunning(r, mode='', stopping=false){
  RUNNING=r; RUNMODE=r?mode:''; STOPPING=r&&stopping;
  $('#runpill .dot').classList.toggle('run', r); $('#runpill span:last-child').textContent = !r?'IDLE':STOPPING?'STOPPING':'RUNNING';
  $('#progress').hidden = !r;
  renderRunButtons(); renderQueue();
}
async function loadQueue(){ try{ QUEUED = (await api('/api/queue')).pending; renderQueue(); }catch(e){} }
function renderQueue(){
  $('#queue').hidden = !QUEUED && RUNMODE!=='resume';
  $('#queue-text').textContent = RUNNING ? `${QUEUED} post(s) were waiting` : `${QUEUED} fetched post(s) not checked yet. They are checked first on the next run.`;
  $('#q-discard').disabled = RUNNING;
}
$('#q-discard').onclick = async () => { if(!confirm(`Throw away ${QUEUED} unchecked post(s)?`)) return;
  try{ await api('/api/queue',{method:'DELETE'}); loadQueue(); }catch(e){ toast(e.message,'bad'); } };

const shown = new Map();
function renderStats(items){
  $('#stats').innerHTML = items.map(([l,v,suf,tone])=>`<div class="stat glass" style="--tone:var(${tone})"><div class="l">${l}</div><div class="v">${shown.get(l)??0}${suf}</div></div>`).join('');
  const still = matchMedia('(prefers-reduced-motion: reduce)').matches;
  $$('#stats .v').forEach((el,i) => {
    const [l,v,suf] = items[i], from = shown.get(l)??0, t0 = performance.now(); shown.set(l,v);
    if (still || from===v) return el.textContent = v+suf;
    const step = now => { const k = Math.min(1,(now-t0)/650); el.textContent = Math.round(from+(v-from)*(1-Math.pow(1-k,3)))+suf; if(k<1) requestAnimationFrame(step); };
    requestAnimationFrame(step);
  });
}
function addLines(lines){ const c=$('#console'); const stick = c.scrollTop+c.clientHeight >= c.scrollHeight-40;
  for(const l of lines){ const d=document.createElement('div'); d.className='l '+l.kind; d.innerHTML=`<span class="t">${esc(l.t)}</span>${esc(l.msg)}`; c.append(d); }
  if(stick) c.scrollTop=c.scrollHeight; }
async function poll(){
  try{
    const s = await api('/api/run/status?after='+lastSeq);
    if(s.lines.length){ addLines(s.lines); lastSeq=s.seq; }
    const was = RUNNING; setRunning(s.running, s.mode, s.stopping);
    if(was && !s.running){ await Promise.all([loadJobs(), loadShots()]); loadRuns(); loadQueue(); toast('Run finished','good'); }
    pollT = setTimeout(poll, s.running ? 1500 : 15000);
  }catch(e){ pollT = setTimeout(poll, 20000); }
}
function startPoll(now){ clearTimeout(pollT); pollT = setTimeout(poll, now?200:0); }
function stopPoll(){ clearTimeout(pollT); }
$('#c-clear').onclick = () => $('#console').innerHTML='';
async function loadRuns(){ try{ const r = await api('/api/runs');
  $('#runs').innerHTML = r.length ? r.map(x=>`<div class="runrow"><span><b>${esc(x.trigger)}</b> · ${esc((x.steps||[]).join(' → '))}</span><span class="${x.status==='done'?'ok':'bad'}">${esc(x.status)}</span><span class="sub" style="width:100%">${new Date(x.started_at).toLocaleString()} — ${esc(x.summary||'')}</span></div>`).join('') : '<div class="empty">No runs yet.</div>'; }catch(e){} }

/* ---------- screenshots ---------- */
async function loadShots(){ try{ const d = await api('/api/screenshots'); SHOTS=d.files; $('#maxmb').textContent=10;
  $('#shot-count').textContent = `${SHOTS.length} waiting (limit ${d.limit})`;
  $('#thumbs').innerHTML = SHOTS.map(s=>`<div class="thumb">${s.url?`<img src="${esc(s.url)}" loading="lazy" alt="">`:'<div style="height:120px"></div>'}<span title="${esc(s.label)}">${esc(s.label)}</span><button data-del="${esc(s.name)}" aria-label="Delete">×</button></div>`).join('');
  renderRunButtons(); }catch(e){} }
$('#thumbs').onclick = async e => { const b=e.target.closest('[data-del]'); if(!b) return; await api('/api/screenshots/'+encodeURIComponent(b.dataset.del),{method:'DELETE'}); loadShots(); };
const drop = $('#drop'); drop.onclick = () => $('#file').click();
['dragenter','dragover'].forEach(t=>drop.addEventListener(t,e=>{e.preventDefault();drop.classList.add('over')}));
['dragleave','drop'].forEach(t=>drop.addEventListener(t,e=>{e.preventDefault();drop.classList.remove('over')}));
drop.addEventListener('drop', e => upload([...e.dataTransfer.files]));
$('#file').onchange = e => { upload([...e.target.files]); e.target.value=''; };
async function upload(files){ files = files.filter(f=>f.type.startsWith('image/')); if(!files.length) return toast('Only image files','bad');
  toast(`Uploading ${files.length} image(s)…`); let ok=0, skipped=[];
  for(let i=0;i<files.length;i+=4){ const fd=new FormData(); files.slice(i,i+4).forEach(f=>fd.append('files',f));
    try{ const r=await api('/api/screenshots',{method:'POST',body:fd}); ok+=r.uploaded.length; skipped.push(...r.skipped);}catch(e){ toast(e.message,'bad'); } }
  if(ok) toast(`${ok} uploaded`,'good'); if(skipped.length) toast('Skipped: '+skipped.join('; '),'bad'); loadShots(); }

/* ---------- schedule ---------- */
const MODES = [['search','Find jobs'],['send','Read screenshots + send'],['both','Everything'],['boards','Company boards']];
async function loadSched(){ try{ const d = await api('/api/schedules'); $('#s-tz').textContent=d.timezone;
  $('#s-rows').innerHTML = d.schedules.length ? d.schedules.map(s=>`<div class="srow" data-id="${esc(s.id)}">
    <input type="text" class="s-l" value="${esc(s.label)}"><input type="time" class="s-t" value="${esc(s.run_at)}">
    <select class="s-m">${MODES.map(([v,l])=>`<option value="${v}" ${s.mode===v?'selected':''}>${l}</option>`).join('')}</select>
    <label class="check"><input type="checkbox" class="s-o" ${s.enabled?'checked':''}> on</label>
    <span class="row"><button class="btn sm" data-ss>Save</button><button class="btn sm danger" data-sd>Delete</button></span>
    <div class="last">last run: ${s.last_run_at?new Date(s.last_run_at).toLocaleString():'never'}</div></div>`).join('') : '<div class="empty"><b>No schedules</b>Add a time to run automatically every day.</div>'; }catch(e){} }
$('#s-add').onclick = async () => { try{ await api('/api/schedules',{method:'POST',body:{label:'Daily run',run_at:'10:00',mode:'both',enabled:true}}); loadSched(); }catch(e){ toast(e.message,'bad'); } };
$('#s-rows').onclick = async e => { const r=e.target.closest('.srow'); if(!r) return; const id=r.dataset.id;
  if(e.target.closest('[data-ss]')){ try{ await api('/api/schedules/'+id,{method:'PATCH',body:{label:r.querySelector('.s-l').value||'Run',run_at:r.querySelector('.s-t').value,mode:r.querySelector('.s-m').value,enabled:r.querySelector('.s-o').checked}}); toast('Saved','good'); loadSched(); }catch(x){ toast(x.message,'bad'); } }
  if(e.target.closest('[data-sd]') && confirm('Delete this schedule?')){ await api('/api/schedules/'+id,{method:'DELETE'}); loadSched(); } };

/* ---------- settings ---------- */
function fillSettings(){
  const m=ME, c=m.candidate, s=m.settings;
  $('#c-name').value=c.name||''; $('#c-email').value=c.email||''; $('#c-phone').value=c.phone||''; $('#c-linkedin').value=c.linkedin||''; $('#c-github').value=c.github||'';
  $('#profile').value=m.profile_text||''; $('#kw').value=(m.keywords||[]).join('\n');
  $('#p-min').value=s.min_score; $('#p-age').value=s.max_age_days; $('#p-cap').value=s.max_send_per_run; $('#p-tz').value=s.timezone; $('#p-vm').value=s.vision_model; $('#p-cm').value=s.chat_model; $('#p-llm').value=s.llm_provider||'auto'; $('#p-nvm').value=s.nvidia_vision_model||''; $('#p-ncm').value=s.nvidia_chat_model||''; $('#p-nthink').checked=!!s.nvidia_thinking; $('#p-strict').checked=s.strict_match!==false; $('#p-excl').value=(s.exclude_words||[]).join(', '); $('#p-am').value=s.assistant_model||''; $('#p-req').checked=!!s.require_contact;
  $('#p-loc').value=(s.preferred_locations||[]).join(', '); $('#p-locstrict').checked=!!s.strict_location; $('#p-locremote').checked=s.include_remote!==false;
  $('#groq-saved').innerHTML = m.keys.groq.length ? m.keys.groq.map(k=>`<span class="chip">${esc(k)}</span>`).join('') : '<span class="sub">No Groq key saved yet.</span>';
  $('#nv-state').innerHTML = m.keys.nvidia ? `<span class="ok">● Saved</span> <span class="chip">${esc(m.keys.nvidia)}</span>` : (m.keys.nvidia_server_key ? '<span class="ok">● Using the server’s NVIDIA key</span>' : '<span class="sub">○ Not set</span>');
  $('#hv-state').innerHTML = m.keys.harvest ? `<span class="ok">● Saved</span> <span class="chip">${esc(m.keys.harvest)}</span>` : '<span class="sub">○ Not set</span>';
  const g=m.gmail;
  $('#gm-state').innerHTML = g.connected ? `<span class="ok">● Connected</span> — sending as <b>${esc(g.email||'your account')}</b>` : '<span class="bad">○ Not connected</span>';
  $('#gm-connect').textContent = g.connected ? 'Switch Gmail account' : 'Connect Gmail'; $('#gm-connect').disabled = !g.client_available;
  $('#gm-off').hidden = !g.connected; $('#gm-uri').textContent = g.redirect_uri;
  if(!g.client_available){ $('#gm-adv').open = true; $('#gm-state').innerHTML += '<div class="hint">This server has no shared Google client — add your own under Advanced first.</div>'; }
  const r=m.resume;
  $('#rs-state').innerHTML = r ? `<span class="ok">● ${esc(r.name)}</span> <span class="sub">${Math.round(r.size/1024)} KB · will be attached as this name</span>` : '<span class="sub">○ No resume uploaded</span>';
  $('#rs-del').hidden = !r;
  fillExtra();
}
const setME = s => { ME = s; fillSettings(); renderSetup(); };
$('#groq-save').onclick = e => busy(e.currentTarget, async()=>{ try{ const keys=$('#groq-in').value.split(/\s+/).filter(Boolean); if(!keys.length) return toast('Paste at least one key','bad'); setME(await api('/api/secrets',{method:'PUT',body:{groq_keys:keys}})); $('#groq-in').value=''; toast('Groq keys saved','good'); }catch(x){ toast(x.message,'bad'); } });
$('#groq-clear').onclick = async () => { if(!confirm('Remove all saved Groq keys?')) return; setME(await api('/api/secrets',{method:'PUT',body:{groq_keys:[]}})); };
$('#groq-test').onclick = e => busy(e.currentTarget, async()=>{ try{ const r=await api('/api/secrets/test-groq',{method:'POST'}); $('#groq-res').innerHTML = r.results.map(x=>x.ok?`<span class="ok">key ${x.key} ✓</span>`:`<span class="bad">key ${x.key} ✗ ${esc(x.error)}</span>`).join(' · '); }catch(x){ toast(x.message,'bad'); } });
$('#nv-save').onclick = e => busy(e.currentTarget, async()=>{ try{ const v=$('#nv-in').value.trim(); if(!v) return toast('Paste a key first','bad'); setME(await api('/api/secrets',{method:'PUT',body:{nvidia_key:v}})); $('#nv-in').value=''; toast('NVIDIA key saved','good'); }catch(x){ toast(x.message,'bad'); } });
$('#nv-clear').onclick = async () => { setME(await api('/api/secrets',{method:'PUT',body:{nvidia_key:''}})); };
$('#nv-test').onclick = e => busy(e.currentTarget, async()=>{ try{ const r=await api('/api/secrets/test-nvidia',{method:'POST'}); $('#nv-res').innerHTML = r.ok ? `<span class="ok">works ✓ (${esc(r.model)})</span>` : `<span class="bad">✗ ${esc(r.error)}</span>`; }catch(x){ toast(x.message,'bad'); } });
$('#hv-save').onclick = e => busy(e.currentTarget, async()=>{ try{ const v=$('#hv-in').value.trim(); if(!v) return toast('Paste a key first','bad'); setME(await api('/api/secrets',{method:'PUT',body:{harvest_key:v}})); $('#hv-in').value=''; toast('HarvestAPI key saved','good'); }catch(x){ toast(x.message,'bad'); } });
$('#hv-clear').onclick = async () => { setME(await api('/api/secrets',{method:'PUT',body:{harvest_key:''}})); };
$('#gm-connect').onclick = e => busy(e.currentTarget, async()=>{ try{ const r=await api('/api/gmail/auth-url'); location.href=r.url; }catch(x){ toast(x.message,'bad'); } });
$('#gm-off').onclick = async () => { if(confirm('Disconnect Gmail? Scheduled sends will stop.')) setME(await api('/api/gmail/disconnect',{method:'POST'})); };
$('#gm-file').onclick = () => $('#gm-file-in').click();
$('#gm-file-in').onchange = async e => { const f=e.target.files[0]; if(f) $('#gm-json').value = await f.text(); e.target.value=''; };
$('#gm-json-save').onclick = e => busy(e.currentTarget, async()=>{ try{ setME(await api('/api/secrets',{method:'PUT',body:{gmail_client_json:$('#gm-json').value}})); $('#gm-json').value=''; toast('Client saved — now press Connect Gmail','good'); }catch(x){ toast(x.message,'bad'); } });
$('#gm-json-clear').onclick = async () => { setME(await api('/api/secrets',{method:'PUT',body:{gmail_client_json:''}})); toast('Using the shared client'); };

$('#rs-pick').onclick = () => $('#rs-in').click();
$('#rs-in').onchange = async e => { const f=e.target.files[0]; e.target.value=''; if(!f) return; const fd=new FormData(); fd.append('file',f); toast('Uploading resume…');
  try{ const r=await api('/api/resume',{method:'POST',body:fd}); setME(r.state); toast('Resume saved'+(r.notes.length?' — '+r.notes.join(', '):''),'good'); }catch(x){ toast(x.message,'bad'); } };
$('#rs-del').onclick = async () => { if(confirm('Remove your resume?')) setME(await api('/api/resume',{method:'DELETE'})); };
$('#rs-build').onclick = e => busy(e.currentTarget, async()=>{ try{ setME(await api('/api/profile/build',{method:'POST'})); toast('Profile & keywords rebuilt','good'); }catch(x){ toast(x.message,'bad'); } });
$('#tex-pick').onclick = () => $('#tex-in').click();
$('#tex-in').onchange = async e => { const f=e.target.files[0]; e.target.value=''; if(!f) return; const fd=new FormData(); fd.append('file',f); toast('Reading LaTeX…');
  try{ setME(await api('/api/profile/tex',{method:'POST',body:fd})); toast('Profile built from LaTeX','good'); }catch(x){ toast(x.message,'bad'); } };

$('#save-all').onclick = e => busy(e.currentTarget, async()=>{ try{
  setME(await api('/api/settings',{method:'PUT',body:{
    ...extraSettings(),
    candidate:{name:$('#c-name').value,email:$('#c-email').value,phone:$('#c-phone').value,linkedin:$('#c-linkedin').value,github:$('#c-github').value},
    profile_text:$('#profile').value, keywords:$('#kw').value.split('\n'),
    min_score:+$('#p-min').value, max_age_days:+$('#p-age').value, max_send_per_run:+$('#p-cap').value, timezone:$('#p-tz').value.trim(),
    vision_model:$('#p-vm').value.trim(), chat_model:$('#p-cm').value.trim(), llm_provider:$('#p-llm').value, nvidia_vision_model:$('#p-nvm').value.trim(), nvidia_chat_model:$('#p-ncm').value.trim(), nvidia_thinking:$('#p-nthink').checked, require_contact:$('#p-req').checked, strict_match:$('#p-strict').checked, exclude_words:$('#p-excl').value.split(','), assistant_model:$('#p-am').value.trim(),
    preferred_locations:$('#p-loc').value.split(','), strict_location:$('#p-locstrict').checked, include_remote:$('#p-locremote').checked}}));
  toast('Settings saved','good'); loadJobs(); }catch(x){ toast(x.message,'bad'); } });

/* ---------- assistant ---------- */
/* Everything from the model is inserted with textContent: no HTML, links or images. */
function el(tag, cls, text){ const n=document.createElement(tag); if(cls) n.className=cls; if(text!==undefined) n.textContent=text; return n; }
function say(role, text){ const m=$('#msgs'); m.append(el('div','msg '+role, text)); m.scrollTop=m.scrollHeight; }
function resetChat(){
  CHAT=[]; const m=$('#msgs'); m.replaceChildren();
  say('assistant', 'Hi! I can look through your jobs, edit drafts, run searches, send mail and change settings. Anything that changes something waits for your approval.');
  const s=el('div','suggest');
  ['Give me an overview','Which jobs are held and why?','Show my 5 best unsent jobs','Start a LinkedIn search'].forEach(t=>{ const b=el('button','',t); b.type='button'; b.onclick=()=>ask(t); s.append(b); });
  m.append(s);
}
$('#fab').onclick = () => { $('#chat').hidden=false; $('#fab').hidden=true; $('#chat-in').focus(); };
$('#chat-x').onclick = () => { $('#chat').hidden=true; $('#fab').hidden=false; };
$('#chat-new').onclick = resetChat;
$('#composer').onsubmit = e => { e.preventDefault(); ask($('#chat-in').value); };
$('#chat-in').onkeydown = e => { if(e.key==='Enter' && !e.shiftKey && !e.isComposing){ e.preventDefault(); ask(e.target.value); } };

async function ask(text){
  text = text.trim(); if(!text || CHAT_BUSY) return;
  CHAT_BUSY = true; $('#chat-send').disabled = true; $('#chat-in').value=''; $('.suggest')?.remove();
  say('user', text);
  const dots=el('div','typing'); dots.append(el('i'),el('i'),el('i')); $('#msgs').append(dots); $('#msgs').scrollTop=1e9;
  try{
    const r = await api('/api/assistant/chat',{method:'POST',body:{message:text, history:CHAT.slice(-12)}});
    dots.remove();
    if (r.warning) say('note','🛡 '+r.warning);
    say('assistant', r.reply);
    r.actions.forEach(actionCard);
    CHAT.push({role:'user',content:text},{role:'assistant',content:r.reply.slice(0,4000)});
  }catch(e){ dots.remove(); say('note', e.message); $('#chat-in').value=text; }
  CHAT_BUSY = false; $('#chat-send').disabled = false;
}

function actionCard(a){
  const card=el('div','card '+a.risk);
  if (a.risk==='high') card.append(el('div','risk','High risk — needs two approvals'));
  card.append(el('h4','',a.title));
  if (a.note) card.append(el('div','hint',a.note));
  const dl=el('dl'); a.details.forEach(([k,v])=>dl.append(el('dt','',k), el('dd','',v))); card.append(dl);
  const second=el('div'), buttons=el('div','row');
  const yes=el('button','btn sm primary', a.risk==='high'?'Approve (1 of 2)':'Approve'), no=el('button','btn sm','Cancel');
  let step=1, input=null;
  const finish = text => { second.replaceChildren(); buttons.replaceChildren(el('span','sub',text)); $('#msgs').scrollTop=1e9; };
  yes.onclick = async () => {
    yes.disabled=true;
    try{
      const r = await api(`/api/assistant/actions/${a.id}/approve`,{method:'POST',body:{step, code: input?input.value:''}});
      if (!r.done) {
        step=2; input=el('input'); input.type='text'; input.inputMode='numeric'; input.maxLength=4; input.setAttribute('aria-label','Confirmation code');
        input.onkeydown = e => { if(e.key==='Enter') yes.click(); };
        second.replaceChildren(el('div','hint','Second approval: type this code'), el('div','code',r.code), input);
        yes.textContent='Confirm (2 of 2)'; setTimeout(()=>{ yes.disabled=false; }, 1600); input.focus(); $('#msgs').scrollTop=1e9; return;
      }
      finish((r.ok?'✓ ':'✗ ')+r.message);
      CHAT.push({role:'assistant',content:`[${r.ok?'Approved and done':'Approved but failed'}: ${a.title}]`});
      await refreshAll();
    }catch(e){ toast(e.message,'bad'); if(/expired|cancelled/i.test(e.message)) return finish(e.message); yes.disabled=false; }
  };
  no.onclick = async () => { try{ await api(`/api/assistant/actions/${a.id}/reject`,{method:'POST'}); }catch(e){} finish('Cancelled — nothing changed.'); };
  buttons.append(yes,no); card.append(second,buttons); $('#msgs').append(card); $('#msgs').scrollTop=1e9;
}
async function refreshAll(){
  try{ setME(await api('/api/me')); }catch(e){}
  await Promise.all([loadJobs(), loadShots()]); loadSched(); loadQueue(); startPoll(true);
}
resetChat();
