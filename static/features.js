'use strict';
/* Follow-ups, stages, board, insights, resumes, sources, alerts, data. Loaded after app.js. */
const STAGE_LABEL = {new:'Not applied', sent:'Applied / Sent', followed_up:'Followed up', replied:'Got reply',
  interview:'Interview', offer:'Offer', rejected:'Rejected', no_answer:'No answer'};
const lines = id => $(id).value.split('\n').map(s=>s.trim()).filter(Boolean);
const swap = j => { JOBS = JOBS.map(x=>x.id===j.id?j:x); renderJobs(); };
const fail = e => { if(e.detail?.needs_gmail){ toast('Connect Gmail first','bad'); show('settings'); } else toast(e.message,'bad'); };

/* ---------- authorised downloads ---------- */
async function download(path, name){
  const r = await fetch(path, {headers:{Authorization:'Bearer '+await auth.currentUser.getIdToken()}});
  if(!r.ok) throw new Error((await r.json().catch(()=>({}))).detail || 'Download failed');
  const a = document.createElement('a'); a.href = URL.createObjectURL(await r.blob()); a.download = name;
  document.body.append(a); a.click(); a.remove(); setTimeout(()=>URL.revokeObjectURL(a.href), 30000);
}
$$('[data-export]').forEach(b => b.onclick = () => busy(b, async()=>{ const f=b.dataset.export;
  try{ await download('/api/export/'+f, f==='all.zip'?'job-mailer-data.zip':f); }catch(e){ fail(e); } }));

/* ---------- hooks called by app.js ---------- */
function onJobs(){
  const due = JOBS.filter(j=>j.follow_up_due);
  $('#fu-panel').hidden = !due.length;
  $('#fu-rows').innerHTML = due.slice(0,20).map(j=>`<div class="runrow"><span><b>${esc(j.company||'—')}</b> · ${esc(j.job_title||'')}<br><span class="sub">${esc(j.recruiter_email)} · sent ${esc(j.sent_date||'')}</span></span>
    <span class="row"><button class="btn sm" data-fu-replied="${esc(j.id)}">Got a reply</button><button class="btn sm primary" data-fu="${esc(j.id)}">Check &amp; send</button></span></div>`).join('');
  if(view==='board') renderBoard();
}
$('#fu-rows').onclick = e => {
  const f = e.target.closest('[data-fu]'), r = e.target.closest('[data-fu-replied]');
  if(f) openFollowUp(f.dataset.fu);
  if(r) setStage(r.dataset.fuReplied, 'replied');
};

async function setStage(id, stage){
  const j = JOBS.find(x=>x.id===id); if(!j || j.stage===stage) return j;
  if(stage==='new' && j.email_status==='Sent' && !confirm('Move back to “Not applied”? This clears the sent date.')) { renderBoard(); return j; }
  try{ const n = await api(`/api/jobs/${id}/stage`,{method:'POST',body:{stage}}); swap(n); toast('Moved to '+STAGE_LABEL[stage],'good'); return n; }
  catch(e){ fail(e); return j; }
}

/* ---------- board ---------- */
function renderBoard(){
  const opts = s => Object.entries(STAGE_LABEL).map(([v,l])=>`<option value="${v}" ${v===s?'selected':''}>${l}</option>`).join('');
  $('#board').innerHTML = Object.entries(STAGE_LABEL).map(([stage,label])=>{
    const rows = JOBS.filter(j=>j.stage===stage);
    return `<div class="col" data-stage="${stage}"><h3><span>${label}</span><span class="sub">${rows.length}</span></h3>${
      rows.slice(0,40).map(j=>`<div class="kc" draggable="true" data-id="${esc(j.id)}"><b>${esc(j.company||'—')}</b><span class="sub">${esc(j.job_title||'')}</span><select aria-label="Stage">${opts(stage)}</select></div>`).join('')
    }${rows.length>40?`<div class="sub">+${rows.length-40} more on the Dashboard</div>`:''}</div>`;
  }).join('');
}
const board = $('#board');
board.onclick = e => { if(e.target.closest('select')) return; const c=e.target.closest('.kc'); if(c) openJob(c.dataset.id); };
board.onchange = e => { const c=e.target.closest('.kc'); if(c) setStage(c.dataset.id, e.target.value); };
board.addEventListener('dragstart', e => { const c=e.target.closest('.kc'); if(c) e.dataTransfer.setData('text/plain', c.dataset.id); });
board.addEventListener('dragover', e => { const c=e.target.closest('.col'); if(c){ e.preventDefault(); c.classList.add('over'); } });
board.addEventListener('dragleave', e => e.target.closest('.col')?.classList.remove('over'));
board.addEventListener('drop', e => { const c=e.target.closest('.col'); if(!c) return; e.preventDefault(); c.classList.remove('over'); setStage(e.dataTransfer.getData('text/plain'), c.dataset.stage); });

/* ---------- insights ---------- */
async function loadInsights(){
  try{
    const a = await api('/api/analytics');
    const cards = [['Sent',a.sent],['Replies',a.replied],['Reply rate',a.reply_rate+'%'],['Interviews',a.interviews],['Interview rate',a.interview_rate+'%'],['Offers',a.offers],['Held',a.held],['Sent today',`${a.usage.sent_today}/${a.usage.limit}`]];
    $('#in-stats').innerHTML = cards.map(([l,v])=>`<div class="stat glass"><div class="l">${l}</div><div class="v">${esc(v)}</div></div>`).join('');
    const max = Math.max(1, ...a.weeks.map(w=>w.sent));
    $('#in-weeks').innerHTML = a.weeks.map(w=>`<div><span>${w.sent}</span><i style="height:${Math.round(100*w.sent/max)}px"></i><span>${esc(w.week.slice(5))}</span></div>`).join('');
    const table = (id, rows, head) => $(id).innerHTML = `<thead><tr><th>${head}</th><th>Sent</th><th>Replies</th><th>Reply rate</th></tr></thead><tbody>`+
      (rows.length ? rows.map(r=>`<tr><td>${esc(r.name)}</td><td>${r.sent}</td><td>${r.replies}</td><td>${r.reply_rate}%</td></tr>`).join('') : '<tr><td colspan="4" class="sub">Nothing sent yet.</td></tr>')+'</tbody>';
    table('#in-roles', a.roles, 'Role'); table('#in-locs', a.locations, 'Location'); table('#in-srcs', a.sources, 'Source');
  }catch(e){ fail(e); }
}

/* ---------- job window ---------- */
function onOpenJob(j){
  $('#j-stage').innerHTML = Object.entries(STAGE_LABEL).map(([v,l])=>`<option value="${v}" ${v===j.stage?'selected':''}>${l}</option>`).join('');
  $('#j-resume').innerHTML = '<option value="">Pick the best match automatically</option>' + (ME.resumes||[]).map(r=>`<option value="${esc(r.id)}">${esc(r.label)}</option>`).join('');
  $('#j-resume').value = (ME.resumes||[]).some(r=>r.id===j.resume_id) ? j.resume_id : '';
  $('#j-resume').disabled = !!j.tailored;
  $('#j-notes').value = j.notes||'';
  $('#j-replied').hidden = !(j.email_status==='Sent' && ['sent','followed_up','no_answer'].includes(j.stage));
  $('#j-followup').hidden = !(j.email_status==='Sent' && j.stage==='sent' && !j.follow_up_sent && j.recruiter_email);
  const a = $('#j-apply'), url = /^https?:\/\//i.test(j.apply_url||'') ? j.apply_url : '';
  a.hidden = !url; if(url) a.href = url; else a.removeAttribute('href');
  $('#j-send').hidden = !j.recruiter_email && !!j.apply_url;
  $('#j-jd').value = ''; $('#j-tbox').open = false;
  showTailored(j);
}
function madeHTML(d){
  const p = d.preview, list = a => a.map(x=>'• '+esc(x)).join('\n');
  const part = (title, body) => body ? `<h4>${title}</h4>${body}` : '';
  return part('Based on', esc(d.based_on)+' · '+esc(d.model))
    + part('Headline', esc(p.headline)) + part('Summary', esc(p.summary)) + part('Skills', esc(p.skills.join(', ')))
    + part('Experience', p.experience.map(e=>`<b>${esc([e.role,e.company,e.dates].filter(Boolean).join(' · '))}</b>\n${list(e.bullets)}`).join('\n'))
    + part('Projects', p.projects.map(e=>`<b>${esc(e.name)}</b>\n${list(e.bullets)}`).join('\n'))
    + part('Education', list(p.education)) + part('Certifications', list(p.certifications))
    + part('The job asks for these, your resume does not show them', esc(d.missing_keywords.join(', ')))
    + part('Suggestions', list(d.suggestions))
    + part('Removed because it was not in your resume', d.removed.length ? `<span class="warnline">${list(d.removed)}</span>` : '');
}
function showTailored(j){
  const d = j.tailored_draft;
  const using = j.tailored ? `<span class="ok">● This job’s mail attaches ${esc(j.tailored.name)}</span>\n` : '';
  $('#j-made').innerHTML = using + (d ? madeHTML(d) : '');
  $('#j-tdl').hidden = !d && !j.tailored;
  $('#j-tuse').hidden = !d || j.tailored?.path===d.path;
  $('#j-tremove').hidden = !j.tailored;
  if(d || j.tailored) $('#j-tbox').open = !!j.tailored;
}
const refreshCur = j => { curJob = j; swap(j); onOpenJob(j); $('#j-tbox').open = true; };
$('#j-stage').onchange = async e => { const n = await setStage(curJob.id, e.target.value); if(n){ curJob=n; openJob(n.id); } };
$('#j-replied').onclick = async () => { const n = await setStage(curJob.id,'replied'); if(n){ curJob=n; openJob(n.id); } };
$('#j-followup').onclick = () => { const id=curJob.id; closeJob(); openFollowUp(id); };
$('#j-tailor').onclick = e => busy(e.currentTarget, async()=>{
  const id = curJob.id; $('#j-tbox').open = true; $('#j-made').textContent = 'Writing your resume… this can take a minute or two.';
  try{
    const draft = await api(`/api/jobs/${id}/tailor`,{method:'POST',body:{resume_id:$('#j-resume').value, job_text:$('#j-jd').value}});
    const j = {...JOBS.find(x=>x.id===id), tailored_draft:draft}; swap(j);
    if(curJob?.id===id){ curJob=j; showTailored(j); $('#j-tbox').open = true; }
  }catch(x){ if(curJob?.id===id) showTailored(curJob); fail(x); }
});
$('#j-tdl').onclick = e => busy(e.currentTarget, async()=>{ try{ await download(`/api/jobs/${curJob.id}/tailor/download`, (curJob.tailored_draft||curJob.tailored).name); }catch(x){ fail(x); } });
$('#j-tuse').onclick = e => busy(e.currentTarget, async()=>{ try{ refreshCur(await api(`/api/jobs/${curJob.id}/tailor/use`,{method:'POST'})); toast('Attached to this job','good'); }catch(x){ fail(x); } });
$('#j-tremove').onclick = e => busy(e.currentTarget, async()=>{ try{ refreshCur(await api(`/api/jobs/${curJob.id}/tailor/remove`,{method:'POST'})); }catch(x){ fail(x); } });

/* ---------- follow-up window ---------- */
let fuId = null;
const closeFollowUp = () => { $('#fm').hidden = true; fuId = null; };
async function openFollowUp(id){
  try{
    const d = await api(`/api/jobs/${id}/followup`);
    if(d.blocked) return toast('No follow-up: '+d.blocked,'bad');
    fuId = id; $('#fm-subject').value = d.subject; $('#fm-body').value = d.body;
    $('#fm-subject').readOnly = d.same_thread;
    $('#fm-info').textContent = `To ${d.to} · ${d.same_thread ? 'sent as a reply in the same thread (subject stays the same)' : 'sent as a new email (the first one has no saved thread)'}. Check your inbox first: the app cannot see replies.`;
    $('#fm').hidden = false;
  }catch(e){ fail(e); }
}
$('#fm-x').onclick = closeFollowUp; $('#fm').onclick = e => { if(e.target.id==='fm') closeFollowUp(); };
$('#fm-send').onclick = e => busy(e.currentTarget, async()=>{ try{ swap(await api(`/api/jobs/${fuId}/followup`,{method:'POST',body:{subject:$('#fm-subject').value, body:$('#fm-body').value}})); toast('Follow-up sent','good'); closeFollowUp(); }catch(x){ fail(x); } });
$('#fm-skip').onclick = e => busy(e.currentTarget, async()=>{ try{ swap(await api(`/api/jobs/${fuId}/followup/skip`,{method:'POST'})); closeFollowUp(); }catch(x){ fail(x); } });
$('#fm-replied').onclick = async () => { const id=fuId; closeFollowUp(); await setStage(id,'replied'); };

/* ---------- settings ---------- */
function fillExtra(){
  const s = ME.settings, k = ME.keys;
  $('#p-rm').value = s.resume_model||''; $('#p-daily').value = s.daily_cap; $('#p-keep').value = s.retention_days;
  $('#p-warm').checked = s.warmup!==false; $('#p-optout').checked = !!s.opt_out_line; $('#p-block').value = (s.blocklist||[]).join('\n');
  $('#p-gh').value = (s.greenhouse_boards||[]).join('\n'); $('#p-lv').value = (s.lever_companies||[]).join('\n'); $('#p-azc').value = s.adzuna_country||'in';
  $('#p-tgchat').value = s.telegram_chat_id||''; $('#p-esum').checked = !!s.email_summary;
  $('#az-state').innerHTML = k.adzuna ? `<span class="ok">● Saved</span> <span class="chip">${esc(k.adzuna)}</span>` : '<span class="sub">○ Not set</span>';
  $('#tg-state').innerHTML = k.telegram ? `<span class="ok">● Saved</span> <span class="chip">${esc(k.telegram)}</span>` : '<span class="sub">○ Not set</span>';
  renderResumes();
}
function extraSettings(){
  return {resume_model:$('#p-rm').value.trim(), daily_cap:+$('#p-daily').value||40, retention_days:+$('#p-keep').value||0,
    warmup:$('#p-warm').checked, opt_out_line:$('#p-optout').checked, blocklist:lines('#p-block'),
    greenhouse_boards:lines('#p-gh'), lever_companies:lines('#p-lv'), adzuna_country:$('#p-azc').value.trim().toLowerCase()||'in',
    telegram_chat_id:$('#p-tgchat').value.trim(), email_summary:$('#p-esum').checked};
}
function renderResumes(){
  const r = ME.resumes||[];
  $('#rv-rows').innerHTML = r.length ? r.map(x=>`<div class="runrow"><span><b>${esc(x.label)}</b> <span class="sub">${esc(x.name)} · ${Math.round((x.size||0)/1024)} KB</span></span>${x.id==='default'?'<span class="sub">main</span>':`<button class="btn sm danger" data-rv="${esc(x.id)}">Remove</button>`}</div>`).join('') : '<span class="sub">Upload your main resume above first, or add one here.</span>';
}
async function reloadMe(){ ME = await api('/api/me'); renderResumes(); renderSetup(); fillResumeState(); }
function fillResumeState(){ const r=ME.resume; $('#rs-del').hidden=!r; if(r) $('#rs-state').innerHTML=`<span class="ok">● ${esc(r.name)}</span>`; }
$('#rv-pick').onclick = () => { if(!$('#rv-label').value.trim()) return toast('Type a short name first','bad'); $('#rv-in').click(); };
$('#rv-in').onchange = async e => { const f=e.target.files[0]; e.target.value=''; if(!f) return; const fd=new FormData(); fd.append('file',f); fd.append('label',$('#rv-label').value);
  try{ await api('/api/resumes',{method:'POST',body:fd}); $('#rv-label').value=''; await reloadMe(); toast('Resume added','good'); }catch(x){ fail(x); } };
$('#rv-rows').onclick = async e => { const b=e.target.closest('[data-rv]'); if(!b || !confirm('Remove this resume?')) return;
  try{ await api('/api/resumes/'+b.dataset.rv,{method:'DELETE'}); await reloadMe(); }catch(x){ fail(x); } };
$('#rm-make').onclick = e => busy(e.currentTarget, async()=>{ $('#rm-out').textContent='Writing your resume… this can take a minute or two.';
  try{ const d = await api('/api/resume/make',{method:'POST'}); $('#rm-out').innerHTML = madeHTML(d); $('#rm-dl').hidden=false; }catch(x){ $('#rm-out').textContent=''; fail(x); } });
$('#rm-dl').onclick = e => busy(e.currentTarget, async()=>{ try{ await download('/api/resume/make/download','Resume.docx'); }catch(x){ fail(x); } });

const saveSecret = async (body, msg) => { try{ setME(await api('/api/secrets',{method:'PUT',body})); toast(msg,'good'); return true; }catch(x){ fail(x); } };
$('#az-save').onclick = async () => { const i=$('#az-id').value.trim(), k=$('#az-key').value.trim(); if(!i||!k) return toast('Enter both ID and key','bad'); if(await saveSecret({adzuna_app_id:i, adzuna_app_key:k},'Adzuna keys saved')){ $('#az-id').value=''; $('#az-key').value=''; } };
$('#az-clear').onclick = () => saveSecret({adzuna_app_id:'', adzuna_app_key:''},'Removed');
$('#tg-save').onclick = async () => { const t=$('#tg-token').value.trim(); if(!t) return toast('Paste the bot token first','bad'); if(await saveSecret({telegram_token:t},'Token saved')) $('#tg-token').value=''; };
$('#tg-clear').onclick = () => saveSecret({telegram_token:''},'Removed');
$('#tg-test').onclick = e => busy(e.currentTarget, async()=>{ try{ await api('/api/telegram/test',{method:'POST'}); toast('Test message sent','good'); }catch(x){ fail(x); } });
$('#dt-purge').onclick = async () => { if(!confirm('Delete jobs older than your saved limit?')) return; try{ const r=await api('/api/privacy/purge',{method:'POST'}); toast(`${r.removed} job(s) deleted`,'good'); loadJobs(); }catch(x){ fail(x); } };
$('#dt-del').onclick = async () => {
  const word = prompt('This removes ALL your jobs, files, keys and schedules. It cannot be undone.\nType DELETE to continue.');
  if(word===null) return;
  try{ await api('/api/account/delete',{method:'POST',body:{confirm:word}}); toast('All your data was deleted','good'); await auth.signOut(); }catch(x){ fail(x); }
};
