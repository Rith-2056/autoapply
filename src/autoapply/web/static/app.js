/* AutoApplier SPA — vanilla JS, hash router, SSE live updates, dictation-style voice input. */
(() => {
  const $ = (sel, el = document) => el.querySelector(sel);
  const main = $('#main');
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const api = async (path, opts = {}) => {
    const r = await fetch('/api' + path, { headers: { 'Content-Type': 'application/json' }, ...opts, body: opts.body && typeof opts.body !== 'string' && !(opts.body instanceof FormData) ? JSON.stringify(opts.body) : opts.body });
    if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail || m; } catch (_) {} throw new Error(m); }
    return r.json();
  };
  const fmt = (iso) => { if (!iso) return ''; const d = new Date(iso); return isNaN(d) ? iso : d.toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }); };
  const fmtDate = (iso) => { if (!iso) return ''; const d = new Date(iso); return isNaN(d) ? iso : d.toLocaleDateString([], { month: 'short', day: 'numeric' }); };
  const due = (iso) => { if (!iso) return ''; const h = (new Date(iso) - Date.now()) / 36e5; if (h < 0) return 'overdue'; if (h < 1) return 'due within the hour'; if (h < 36) return `due in ${Math.round(h)} hours`; return `due in ${Math.round(h / 24)} days`; };
  const toast = (msg, kind = '') => { const t = document.createElement('div'); t.className = 'toast ' + kind; t.textContent = msg; $('#toasts').appendChild(t); setTimeout(() => t.remove(), 6000); };
  const pill = (s, label) => `<span class="pill ${esc(s)}">${esc(label || s)}</span>`;
  const STATUS_LABELS = { DISCOVERED: 'Discovered', READY_TO_APPLY: 'Ready to apply', APPLYING: 'Applying', NEEDS_INPUT: 'Needs input', SUBMITTED: 'Submitted', CONFIRMATION_RECEIVED: 'Confirmed', ASSESSMENT: 'Assessment', INTERVIEW: 'Interview', FINAL_INTERVIEW: 'Final interview', OFFER: 'Offer', REJECTED: 'Rejected', WITHDRAWN: 'Withdrawn', FAILED: 'Failed', UNKNOWN: 'Unknown' };

  // ---------------------------------------------------------------- live events
  let route = '/';
  const state = { session: null, notifications: [] };
  const refreshers = new Set();
  function connectSSE() {
    const es = new EventSource('/api/events/stream');
    es.onopen = () => $('#conn').classList.add('on');
    es.onerror = () => $('#conn').classList.remove('on');
    const handle = (ev) => {
      let d; try { d = JSON.parse(ev.data); } catch (_) { return; }
      if (d.kind === 'notification') { toast(d.title || d.message, 'ok'); notifyBrowser(d.title, d.message); loadNotifications(); }
      if (d.kind === 'notice') toast(d.message, 'warn');
      if (d.kind === 'answer_result') { if (d.ok) toast('Answer entered ✓', 'ok'); else { toast('Could not enter the answer: ' + d.message + '. Fix it in the browser window, then press "I edited in the browser".', 'danger'); document.querySelectorAll('.q-card').forEach((c) => c.style.opacity = 1); } }
      if (d.kind === 'application_event' && ['ApplicationSubmitted'].includes(d.type)) toast('Application submitted ✓', 'ok');
      if (d.kind === 'application_event' && d.type === 'InputRequested') toast('An application needs your input', 'warn');
      if (d.kind === 'email' || d.kind === 'email_sync') loadNotifications();
      if (d.kind === 'session') { state.session = d; $('#nav-session-dot').classList.toggle('hidden', !['Running', 'WaitingForUser', 'Paused'].includes(d.status)); }
      refreshers.forEach((fn) => { try { fn(d); } catch (e) { console.error(e); } });
    };
    ['message', 'session', 'application_event', 'notification', 'notice', 'answer_result', 'email', 'email_sync', 'email_status', 'application_updated', 'action_updated'].forEach((k) => es.addEventListener(k, handle));
  }
  function notifyBrowser(title, body) {
    if (!('Notification' in window) || Notification.permission !== 'granted') return;
    try { new Notification(title || 'AutoApplier', { body: body || '' }); } catch (_) {}
  }
  async function loadNotifications() {
    try {
      state.notifications = await api('/notifications');
      const unread = state.notifications.filter((n) => !n.read).length;
      const b = $('#notif-count'); b.textContent = unread; b.classList.toggle('hidden', !unread);
      const actions = await api('/actions');
      const ab = $('#nav-actions-badge'); ab.textContent = actions.length; ab.classList.toggle('hidden', !actions.length);
    } catch (_) {}
  }
  $('#btn-notifications').onclick = async () => {
    const p = $('#notif-panel'); p.classList.toggle('hidden');
    if (p.classList.contains('hidden')) return;
    p.innerHTML = `<div class="row between"><h3>Notifications</h3><button class="ghost" id="read-all">Mark all read</button></div>` +
      (state.notifications.length ? state.notifications.map((n) => `<div class="notif ${n.read ? '' : 'unread'}">${esc(n.title)}<div class="sub" style="margin:0">${esc(n.message)} · ${fmt(n.created_at)}${n.application_id ? ` · <a href="#/applications/${n.application_id}">open</a>` : ''}</div></div>`).join('') : '<div class="empty">Nothing yet.</div>');
    $('#read-all', p).onclick = async () => { await api('/notifications/read_all', { method: 'POST' }); loadNotifications(); p.classList.add('hidden'); };
  };

  // ---------------------------------------------------------------- pages
  const pages = {};

  pages['/'] = async () => {
    const d = await api('/dashboard');
    const m = d.metrics; const s = d.session;
    const hour = new Date().getHours(); const greet = hour < 12 ? 'Good morning' : hour < 18 ? 'Good afternoon' : 'Good evening';
    main.innerHTML = `
      <div class="row between wrap"><div><h1>${greet}</h1><p class="sub">${m.needs_action ? `<b>${m.needs_action}</b> thing${m.needs_action === 1 ? '' : 's'} need your attention.` : 'Nothing needs your attention right now.'}${d.migrated ? ` Imported ${d.migrated} application(s) from the old database.` : ''}</p></div>
        <a class="btn big" href="#/autoapply">▶ Start AutoApplying</a></div>
      <div class="grid cols-4">
        ${metric(m.total_applications, 'Applications')}${metric(m.applications_today, 'Today')}${metric(m.applications_this_week, 'This week')}${metric(m.applications_this_month, 'This month')}
        ${metric(m.needs_action, 'Need action', 'warn')}${metric(m.assessments, 'Assessments')}${metric(m.interviews, 'Interviews')}${metric(m.offers, 'Offers')}
      </div>
      <h2>Action required</h2>
      <div class="card" id="dash-actions">${renderActions(d.actions, true)}</div>
      <h2>AutoApply</h2>
      <div class="card" id="dash-session">${renderSessionSummary(s)}</div>
      <h2>Recent applications</h2>
      <div class="card">${renderAppTable(d.recent_applications)}<div class="row" style="margin-top:10px"><a href="#/applications">View all →</a>${d.email.connected ? '' : '<span class="right sub" style="margin:0">Email not connected · <a href="#/settings">connect Gmail</a> to track assessments and interviews automatically</span>'}</div></div>`;
    bindAppRows();
    refreshers.add(pages.__dashRefresh = async (ev) => { if (['session', 'application_event', 'action_updated', 'email'].includes(ev.kind)) { const dd = await api('/dashboard'); $('#dash-session').innerHTML = renderSessionSummary(dd.session); $('#dash-actions').innerHTML = renderActions(dd.actions, true); } });
  };
  const metric = (n, l, cls = '') => `<div class="card metric"><div class="n" style="${cls === 'warn' && n ? 'color:var(--warn)' : ''}">${n}</div><div class="l">${l}</div></div>`;

  function renderActions(actions, compact = false) {
    if (!actions.length) return '<div class="empty">No open actions. 🎉</div>';
    return actions.map((a) => `<div class="action"><span class="prio ${esc(a.priority)}"></span><div style="flex:1">
      <div><b>${esc(a.company || '')}</b>${a.job_title ? ' — ' + esc(a.job_title) : ''}</div>
      <div>${esc(a.title)}${a.deadline ? ` · <span class="deadline ${(new Date(a.deadline) - Date.now()) < 48 * 36e5 ? 'soon' : ''}">${due(a.deadline)} (${fmt(a.deadline)})</span>` : ''}</div>
      ${!compact && a.description ? `<div class="sub" style="margin:4px 0 0">${esc(a.description)}</div>` : ''}</div>
      <div class="row">${a.url ? `<a class="btn sm" href="${esc(a.url)}" target="_blank" rel="noopener">Open</a>` : ''}${a.application_id ? `<a class="btn sm secondary" href="#/applications/${a.application_id}">View</a>` : ''}
      <button class="btn sm ok" data-done="${a.id}">Done</button><button class="ghost" data-dismiss="${a.id}">✕</button></div></div>`).join('');
  }
  document.addEventListener('click', async (e) => {
    const done = e.target.closest('[data-done]'); const dis = e.target.closest('[data-dismiss]');
    if (done || dis) { e.preventDefault(); await api(`/actions/${(done || dis).dataset.done || (done || dis).dataset.dismiss}`, { method: 'PATCH', body: { status: done ? 'done' : 'dismissed' } }); loadNotifications(); render(); }
  });

  function renderAppTable(apps, full = false) {
    if (!apps.length) return '<div class="empty">No applications yet.</div>';
    return `<table><thead><tr><th>Company</th><th>Position</th><th>Applied</th><th>Status</th><th>Next action</th>${full ? '<th>Location</th>' : ''}</tr></thead><tbody>` +
      apps.map((a) => `<tr class="click" data-app="${a.id}"><td><b>${esc(a.company)}</b></td><td>${esc(a.title)}</td><td>${fmtDate(a.date_applied || a.created_at)}</td><td>${pill(a.status, a.status_label || STATUS_LABELS[a.status])}</td>
        <td>${a.next_action ? esc(a.next_action) + (a.next_action_deadline ? ` <span class="deadline ${(new Date(a.next_action_deadline) - Date.now()) < 48 * 36e5 ? 'soon' : ''}">· ${due(a.next_action_deadline)}</span>` : '') : (['SUBMITTED', 'CONFIRMATION_RECEIVED'].includes(a.status) ? '<span class="sub" style="margin:0">Waiting</span>' : '')}</td>${full ? `<td>${esc(a.location || '')}</td>` : ''}</tr>`).join('') + '</tbody></table>';
  }
  const bindAppRows = () => main.querySelectorAll('tr[data-app]').forEach((tr) => tr.onclick = () => location.hash = '#/applications/' + tr.dataset.app);

  pages['/applications'] = async () => {
    main.innerHTML = `<h1>Applications</h1><p class="sub">Every application the AutoApplier has made or tracked. Type "needs action" or "assessment" to filter by state.</p>
      <div class="filters"><input id="f-search" placeholder="Search company, position, location… or 'needs action'"><select id="f-status"><option value="">Any status</option>${Object.entries(STATUS_LABELS).map(([k, v]) => `<option value="${k}">${v}</option>`).join('')}</select>
      <input id="f-since" type="date" title="Applied since"><input id="f-until" type="date" title="Applied until"><label style="margin:0;display:flex;align-items:center;gap:6px;text-transform:none"><input type="checkbox" id="f-needs" style="width:auto"> Needs action</label></div>
      <div class="card" id="app-list"></div>`;
    const load = async () => {
      const p = new URLSearchParams();
      const s = $('#f-search').value.trim(); if (s) p.set('search', s);
      if ($('#f-status').value) p.set('status', $('#f-status').value);
      if ($('#f-since').value) p.set('since', $('#f-since').value);
      if ($('#f-until').value) p.set('until', $('#f-until').value);
      if ($('#f-needs').checked) p.set('needs_action', 'true');
      const apps = await api('/applications?' + p.toString());
      $('#app-list').innerHTML = renderAppTable(apps, true); bindAppRows();
    };
    ['#f-search', '#f-status', '#f-since', '#f-until', '#f-needs'].forEach((id) => $(id).addEventListener('input', load));
    await load();
    refreshers.add(async (ev) => { if (['application_event', 'application_updated', 'email'].includes(ev.kind)) load(); });
  };

  pages['/applications/:id'] = async (id) => {
    const d = await api('/applications/' + id); const a = d.application;
    const stages = ['SUBMITTED', 'CONFIRMATION_RECEIVED', 'ASSESSMENT', 'INTERVIEW', 'FINAL_INTERVIEW', 'OFFER'];
    const idx = stages.indexOf(a.status);
    main.innerHTML = `<div class="row between wrap"><div><h1>${esc(a.company)}</h1><p class="sub">${esc(a.title)} · ${esc(a.location || '')}</p></div>
      <div class="row">${['NEEDS_INPUT', 'FAILED'].includes(a.status) ? `<button class="btn" id="btn-retry">↻ Retry / resume</button>` : ''}<a class="btn secondary" href="${esc(a.application_url || a.job_url)}" target="_blank" rel="noopener">Open application ↗</a></div></div>
      <div class="grid cols-3">
        <div class="card"><div class="l sub" style="margin:0">Status</div><div style="font-size:20px;font-weight:700">${pill(a.status, a.status_label)}</div>${a.error ? `<div class="sub" style="margin:6px 0 0;color:var(--danger)">${esc(a.error)}</div>` : ''}
          <label>Set status manually</label><select id="set-status"><option value="">—</option>${Object.entries(STATUS_LABELS).map(([k, v]) => `<option value="${k}">${v}</option>`).join('')}</select></div>
        <div class="card"><div class="l sub" style="margin:0">Next action</div><div style="font-size:16px;font-weight:600">${esc(a.next_action || (idx >= 0 && idx < 2 ? 'Waiting for response' : '—'))}</div>${a.next_action_deadline ? `<div class="deadline ${(new Date(a.next_action_deadline) - Date.now()) < 48 * 36e5 ? 'soon' : ''}">${due(a.next_action_deadline)} · ${fmt(a.next_action_deadline)}</div>` : ''}
          ${d.actions.filter((x) => x.status === 'open').map((x) => `<div class="row" style="margin-top:8px">${x.url ? `<a class="btn sm" href="${esc(x.url)}" target="_blank" rel="noopener">Open</a>` : ''}<button class="btn sm ok" data-done="${x.id}">Done</button><span>${esc(x.title)}</span></div>`).join('')}</div>
        <div class="card"><div class="l sub" style="margin:0">Application</div><dl class="kv"><dt>Applied</dt><dd>${fmt(a.date_applied) || '—'}</dd><dt>Source</dt><dd>${esc(a.source || '')} (${esc(a.ats || '')})</dd><dt>Resume</dt><dd>${esc((a.resume_path || '').split('/').pop())}</dd><dt>Job URL</dt><dd><a href="${esc(a.job_url)}" target="_blank" rel="noopener">${esc(a.job_url)}</a></dd>${a.screenshot_path ? `<dt>Screenshot</dt><dd>${esc(a.screenshot_path.split('/').pop())}</dd>` : ''}</dl></div>
      </div>
      <div class="tabs"><button class="active" data-tab="timeline">Timeline</button><button data-tab="qa">Questions & answers (${d.questions.length})</button><button data-tab="emails">Emails (${d.emails.length})</button><button data-tab="jd">Job description</button></div>
      <div class="card" id="tab-timeline"><ul class="timeline">${d.events.map((e) => `<li><span class="t">${fmt(e.timestamp)}</span><span><b>${esc(e.type.replace(/([A-Z])/g, ' $1').trim())}</b>${e.message ? ' — ' + esc(e.message) : ''}</span></li>`).join('') || '<div class="empty">No events.</div>'}</ul></div>
      <div class="card hidden" id="tab-qa">${d.questions.length ? `<table><thead><tr><th>Question</th><th>Answer</th><th>Source</th><th>Status</th></tr></thead><tbody>${d.questions.map((q) => { const last = q.answers[q.answers.length - 1] || {}; return `<tr><td>${esc(q.question_text)}<div class="sub" style="margin:0;font-size:12px">${esc(q.category)}${q.required ? ' · required' : ''}${q.reason ? ' · ' + esc(q.reason) : ''}</div></td><td>${esc(last.final_answer || '')}${last.raw_transcription && last.raw_transcription !== last.final_answer ? `<div class="sub" style="margin:0;font-size:12px">raw: ${esc(last.raw_transcription)}</div>` : ''}</td><td>${esc(last.source || '')}${last.ai_modified ? ' <span class="pill">AI-cleaned</span>' : ''}${last.user_approved ? ' <span class="pill">approved</span>' : ''}</td><td>${esc(q.status)}</td></tr>`; }).join('')}</tbody></table>` : '<div class="empty">No questions recorded.</div>'}</div>
      <div class="card hidden" id="tab-emails">${d.emails.length ? d.emails.map((e) => `<div class="action"><div style="flex:1"><b>${esc(e.subject)}</b> ${pill(e.category)}<div class="sub" style="margin:0">${esc(e.sender)} · ${fmt(e.received_at)}${e.extracted && e.extracted.summary ? ' · ' + esc(e.extracted.summary) : ''}</div></div></div>`).join('') : '<div class="empty">No emails linked yet.</div>'}</div>
      <div class="card hidden" id="tab-jd"><pre style="white-space:pre-wrap;max-height:600px">${esc(a.description || 'Not captured.')}</pre></div>`;
    main.querySelectorAll('.tabs button').forEach((b) => b.onclick = () => { main.querySelectorAll('.tabs button').forEach((x) => x.classList.remove('active')); b.classList.add('active'); ['timeline', 'qa', 'emails', 'jd'].forEach((t) => $('#tab-' + t).classList.toggle('hidden', t !== b.dataset.tab)); });
    $('#set-status').onchange = async (e) => { if (!e.target.value) return; try { await api('/applications/' + id, { method: 'PATCH', body: { status: e.target.value } }); toast('Status updated', 'ok'); render(); } catch (err) { toast(err.message, 'danger'); } };
    const rb = $('#btn-retry'); if (rb) rb.onclick = async () => { try { await api(`/applications/${id}/retry`, { method: 'POST' }); location.hash = '#/autoapply'; } catch (err) { toast(err.message, 'danger'); } };
    refreshers.add(async (ev) => { if ((ev.kind === 'application_event' && ev.application_id == id) || ev.kind === 'application_updated') render(); });
  };

  pages['/actions'] = async () => {
    const [open, done, waiting] = await Promise.all([api('/actions'), api('/actions?status=done'), api('/applications?status=SUBMITTED')]);
    const confirmed = await api('/applications?status=CONFIRMATION_RECEIVED');
    main.innerHTML = `<h1>Action Center</h1><p class="sub">What do I need to do right now?</p>
      <h2>Action required (${open.length})</h2><div class="card">${renderActions(open)}</div>
      <h2>Waiting</h2><div class="card">${waiting.length + confirmed.length ? `<b>${waiting.length + confirmed.length}</b> application(s) waiting for a response. <a href="#/applications">View</a>` : '<div class="empty">Nothing waiting.</div>'}</div>
      <h2>Recently completed</h2><div class="card">${done.slice(0, 10).map((a) => `<div class="action"><div style="flex:1"><b>${esc(a.company || '')}</b> — ${esc(a.title)} <span class="sub" style="margin:0">· ${fmt(a.completed_at)}</span></div></div>`).join('') || '<div class="empty">None yet.</div>'}</div>`;
    refreshers.add(async (ev) => { if (['action_updated', 'email', 'application_event'].includes(ev.kind)) render(); });
  };

  // ---------------------------------------------------------------- AutoApply
  pages['/autoapply'] = async () => {
    const s = await api('/autoapply/current');
    main.innerHTML = `<div class="row between wrap"><div><h1>AutoApply</h1><p class="sub">Configure a run, then follow it live. The browser stays visible; you review every application before it is submitted.</p></div></div>
      <div id="aa-session"></div><div id="aa-config"></div>`;
    renderSession(s);
    if (!s.running) renderConfig();
    refreshers.add(async (ev) => { if (['session', 'application_event', 'answer_result'].includes(ev.kind)) { const ss = await api('/autoapply/current'); renderSession(ss); if (!ss.running && !$('#aa-config').innerHTML) renderConfig(); if (ss.running) $('#aa-config').innerHTML = ''; } });
  };

  function renderConfig() {
    const el = $('#aa-config'); if (!el) return;
    el.innerHTML = `<div class="card"><h3>Start AutoApplying</h3><div class="form-grid">
      <div><label>Target roles (comma separated, matched against titles; empty = any)</label><input id="c-roles" placeholder="Software Engineer Intern, Backend, ML"></div>
      <div><label>Locations (comma separated; empty = anywhere)</label><input id="c-locs" placeholder="United States, Remote, New York"></div>
      <div><label>Maximum applications this session</label><input id="c-max" type="number" value="10" min="1" max="100"></div>
      <div><label>Categories</label><input id="c-cats" value="Software, AI/ML/Data, Quant"></div>
      <div><label>Excluded companies</label><input id="c-excl" placeholder="Acme, Foo Corp"></div>
      <div><label>Excluded locations</label><input id="c-exloc" placeholder="Canada, London"></div>
      <div><label>Options</label><div class="row wrap"><label style="text-transform:none;margin:0;display:flex;gap:6px;align-items:center"><input type="checkbox" id="c-dry" style="width:auto"> Dry run (never submit)</label><label style="text-transform:none;margin:0;display:flex;gap:6px;align-items:center"><input type="checkbox" id="c-retry" style="width:auto"> Include failed / needs-input applications</label></div></div>
      </div><div class="row" style="margin-top:14px"><button class="btn secondary" id="c-preview">Preview matching jobs</button><button class="btn big" id="c-start">▶ Start AutoApply</button></div><div id="c-preview-out" style="margin-top:12px"></div></div>`;
    const cfg = () => ({ target_roles: split($('#c-roles').value), locations: split($('#c-locs').value), max_applications: +$('#c-max').value || 10, categories: split($('#c-cats').value), excluded_companies: split($('#c-excl').value), excluded_locations: split($('#c-exloc').value), dry_run: $('#c-dry').checked, retry: $('#c-retry').checked });
    $('#c-preview').onclick = async () => { const c = cfg(); const p = new URLSearchParams({ target_roles: c.target_roles.join(','), locations: c.locations.join(','), categories: c.categories.join(','), excluded_companies: c.excluded_companies.join(',') }); const r = await api('/listings/preview?' + p); $('#c-preview-out').innerHTML = r.stats.error ? `<div class="sub">${esc(r.stats.error)}</div>` : `<div class="sub">${r.stats.candidates} candidate(s) (of ${r.stats.matched_filters} matching, ${r.stats.already_in_db} already tracked)</div><table><tbody>${r.listings.map((l) => `<tr><td><b>${esc(l.company)}</b></td><td>${esc(l.title)}</td><td>${esc((l.locations || []).join('; '))}</td></tr>`).join('')}</tbody></table>`; };
    $('#c-start').onclick = async () => { try { await api('/autoapply/start', { method: 'POST', body: cfg() }); toast('AutoApply session started', 'ok'); if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission(); render(); } catch (e) { toast(e.message, 'danger'); } };
  }
  const split = (s) => s.split(',').map((x) => x.trim()).filter(Boolean);

  function renderSessionSummary(s) {
    if (!s || !s.session) return '<div class="empty">No AutoApply session yet. <a href="#/autoapply">Start one</a>.</div>';
    const sess = s.session; const cur = s.current;
    return `<div class="row between wrap"><div><b>${esc(sess.status)}</b> · ${sess.submitted} submitted / ${sess.attempted} attempted / ${sess.total || '?'} planned${cur ? `<div class="sub" style="margin:4px 0 0">Current: <b>${esc(cur.company)}</b> — ${esc(cur.title)} (${esc(cur.step || '')})</div>` : ''}${s.waiting ? `<div style="color:var(--warn);font-weight:600;margin-top:4px">⚠ Waiting for you: ${esc(s.waiting.type)}</div>` : ''}</div><a class="btn ${s.waiting ? '' : 'secondary'}" href="#/autoapply">${s.waiting ? 'Respond' : 'View session'}</a></div>`;
  }

  function renderSession(s) {
    const el = $('#aa-session'); if (!el) return;
    if (!s.session) { el.innerHTML = ''; return; }
    const sess = s.session; const apps = s.applications || []; const cur = s.current;
    const pct = sess.total ? Math.min(100, Math.round(100 * sess.attempted / sess.total)) : 0;
    const stepOrder = ['opening', 'filling', 'questions', 'review', 'submitting', 'done'];
    const stepLabels = { opening: 'Application opened', filling: 'Personal information & resume filled', questions: 'Application questions', review: 'Review', submitting: 'Submit', done: 'Submitted' };
    const ci = cur ? stepOrder.indexOf(cur.step) : -1;
    el.innerHTML = `<div class="card"><div class="row between wrap"><div><h3>AutoApply session #${sess.id} · ${esc(sess.status)}</h3><div class="sub" style="margin:0">${sess.submitted} submitted · ${sess.attempted} / ${sess.total || '?'} applications${sess.error ? ` · <span style="color:var(--danger)">${esc(sess.error)}</span>` : ''}</div></div>
      <div class="row">${s.running ? (s.paused ? `<button class="btn" id="s-resume">Resume</button>` : `<button class="btn secondary" id="s-pause">Pause</button>`) + `<button class="btn danger" id="s-cancel">Cancel</button>` : `<button class="btn" id="s-new">New session</button>`}</div></div>
      <div class="progress" style="margin:12px 0"><i style="width:${pct}%"></i></div>
      <div class="grid cols-2"><div><ul class="steps">${apps.map((a) => `<li class="${['SUBMITTED', 'CONFIRMATION_RECEIVED'].includes(a.status) ? 'done' : (cur && a.id === cur.id ? 'now' : '')}">${['SUBMITTED', 'CONFIRMATION_RECEIVED'].includes(a.status) ? '✓' : (cur && a.id === cur.id ? '→' : (['FAILED', 'NEEDS_INPUT', 'WITHDRAWN'].includes(a.status) ? '✕' : '○'))} <a href="#/applications/${a.id}">${esc(a.company)}</a> — ${esc(a.title)} ${pill(a.status, STATUS_LABELS[a.status])}</li>`).join('') || '<li>Collecting listings…</li>'}</ul></div>
      <div>${cur ? `<b>${esc(cur.company)}</b><div class="sub" style="margin:0 0 6px">${esc(cur.title)}</div><ul class="steps">${stepOrder.slice(0, 5).map((st, i) => `<li class="${i < ci ? 'done' : i === ci ? 'now' : ''}">${i < ci ? '✓' : i === ci ? '→' : '○'} ${stepLabels[st]}</li>`).join('')}</ul>` : ''}</div></div>
      <div id="aa-waiting"></div><h3 style="margin-top:14px">Live events</h3><div id="aa-events" class="sub" style="max-height:220px;overflow:auto;font-size:13px"></div></div>`;
    const sp = $('#s-pause'); if (sp) sp.onclick = () => api(`/autoapply/${sess.id}/pause`, { method: 'POST' });
    const sr = $('#s-resume'); if (sr) sr.onclick = () => api(`/autoapply/${sess.id}/resume`, { method: 'POST' });
    const sc = $('#s-cancel'); if (sc) sc.onclick = () => { if (confirm('Cancel this session?')) api(`/autoapply/${sess.id}/cancel`, { method: 'POST' }); };
    const sn = $('#s-new'); if (sn) sn.onclick = () => { $('#aa-session').innerHTML = ''; renderConfig(); };
    renderWaiting(s);
    api('/events/recent?session_id=' + sess.id).then((evs) => { const box = $('#aa-events'); if (box) box.innerHTML = evs.map((e) => `<div><span style="color:var(--muted)">${fmt(e.timestamp)}</span> · <b>${esc(e.company)}</b> · ${esc(e.type.replace(/([A-Z])/g, ' $1').trim())}${e.message ? ' — ' + esc(e.message) : ''}</div>`).join(''); });
  }

  // ---------------------------------------------------------------- waiting-for-user panel (questions / review / captcha)
  const answered = {};
  function renderWaiting(s) {
    const el = $('#aa-waiting'); if (!el) return;
    const w = s.waiting; if (!w) { el.innerHTML = ''; return; }
    const sid = s.session.id;
    const cmd = (c) => api(`/autoapply/${sid}/command`, { method: 'POST', body: c }).catch((e) => toast(e.message, 'danger'));
    if (w.type === 'captcha') {
      el.innerHTML = `<div class="status-banner warn" style="margin-top:12px"><div style="flex:1"><b>⚠ CAPTCHA at ${esc(w.company)}</b> (${esc(w.stage)}). Solve it in the browser window, then continue. It is never bypassed automatically.</div><button class="btn" id="cap-ok">I solved it</button><button class="btn secondary" id="cap-skip">Skip this application</button></div>`;
      $('#cap-ok').onclick = () => cmd({ type: 'captcha_solved' }); $('#cap-skip').onclick = () => cmd({ type: 'skip' }); return;
    }
    if (w.type === 'review') {
      const rows = (w.answers || []).filter((a) => a.label);
      el.innerHTML = `<div class="card" style="margin-top:12px;border-color:var(--warn)"><h3>Review before submitting — ${esc(w.company)}</h3>
        ${w.blocking && w.blocking.length ? `<div class="status-banner warn" style="margin-bottom:10px">Still blank / unapproved: ${w.blocking.map(esc).join('; ')}. Finish them in the browser and press "I edited it in the browser".</div>` : ''}
        <table><thead><tr><th>Field</th><th>Answer</th><th>Source</th></tr></thead><tbody>${rows.map((a) => `<tr><td>${esc(a.label)}</td><td>${esc(a.value)}</td><td>${esc(a.source)}${a.status === 'draft' ? ' <span class="pill">needs approval</span>' : a.status === 'needs_user' ? ' <span class="pill NEEDS_INPUT">blank</span>' : ''}</td></tr>`).join('')}</tbody></table>
        <div class="row" style="margin-top:12px"><button class="btn ok big" id="rv-submit" ${w.blocking && w.blocking.length ? 'disabled' : ''}>Submit application</button><button class="btn secondary" id="rv-edit">I edited it in the browser</button><button class="btn secondary" id="rv-skip">Skip</button><button class="ghost" id="rv-manual">Leave for later</button></div></div>`;
      $('#rv-submit').onclick = () => cmd({ type: 'submit' }); $('#rv-edit').onclick = () => cmd({ type: 'edit_done' }); $('#rv-skip').onclick = () => cmd({ type: 'skip' }); $('#rv-manual').onclick = () => cmd({ type: 'manual' });
      return;
    }
    if (w.type === 'questions') {
      const qs = w.questions || [];
      el.innerHTML = `<div class="card" style="margin-top:12px;border-color:var(--warn)"><div class="row between"><h3>⚠ ${qs.length} question${qs.length === 1 ? '' : 's'} need you — ${esc(w.company)}</h3><div class="row"><button class="btn secondary sm" id="q-edit-done">I edited in the browser</button><button class="btn sm" id="q-continue">Continue to review</button></div></div>
        <div id="q-list"></div></div>`;
      const list = $('#q-list');
      qs.forEach((q) => list.appendChild(questionCard(q, w.application_id, sid)));
      $('#q-continue').onclick = () => cmd({ type: 'continue' }); $('#q-edit-done').onclick = () => cmd({ type: 'edit_done' });
    }
  }

  function questionCard(q, appId, sid) {
    const card = document.createElement('div'); card.className = 'q-card';
    const key = appId + ':' + q.field_id; const saved = answered[key] || {};
    card.innerHTML = `<div class="q">${esc(q.label)}</div><div class="meta">${esc(q.category)} · ${q.required ? 'required' : 'optional'} · ${esc(q.reason)}</div>
      ${q.options && q.options.length ? `<select class="q-select"><option value="">— choose —</option>${q.options.map((o) => `<option ${saved.value === o ? 'selected' : ''}>${esc(o)}</option>`).join('')}</select>` : `<textarea class="q-text" placeholder="Type here, use your own dictation tool, or press the mic">${esc(saved.value || q.draft || '')}</textarea>`}
      ${q.draft ? `<div class="sub" style="margin:6px 0">✨ Draft written from your resume and the job — review and edit before accepting.</div>` : ''}
      <div class="interim"></div><div class="clean-out"></div>
      <div class="row wrap" style="margin-top:8px">${!(q.options && q.options.length) ? `<button class="mic">🎙 Speak</button><button class="ghost btn-clean">✨ Clean up</button>` : ''}<button class="ghost btn-reuse">Reuse answer</button><span class="right"></span><button class="ghost btn-skip">Skip</button><button class="btn btn-confirm">Confirm answer</button></div>`;
    const ta = $('.q-text', card); const sel = $('.q-select', card); const out = $('.clean-out', card); const interim = $('.interim', card);
    let voice = { raw: '', cleaned: '', confidence: '', ai_modified: false, source: 'typed' };
    const getVal = () => (sel ? sel.value : ta.value.trim());
    const cmd = (c) => api(`/autoapply/${sid}/command`, { method: 'POST', body: c }).catch((e) => toast(e.message, 'danger'));
    $('.btn-confirm', card).onclick = () => { const v = getVal(); if (!v) return toast('Nothing to confirm', 'warn'); answered[key] = { value: v }; cmd({ type: 'answer', field_id: q.field_id, value: v, source: voice.raw ? 'voice' : (q.draft && v === q.draft ? 'draft' : 'typed'), raw: voice.raw, cleaned: voice.cleaned, confidence: voice.confidence, ai_modified: voice.ai_modified, save_reusable: v.length > 60 }); card.style.opacity = .5; };
    $('.btn-skip', card).onclick = () => cmd({ type: 'skip_question', field_id: q.field_id });
    $('.btn-reuse', card).onclick = async () => { const r = await api('/approved-answers?question=' + encodeURIComponent(q.label)); if (!r.length) return toast('No previously approved answer for a similar question', 'warn'); out.innerHTML = `<div class="sub">Previously approved answer (${r[0].company || 'earlier'}):</div><div class="q-card">${esc(r[0].answer)}</div><div class="chips"><button class="chip use-prev">Use answer</button></div>`; $('.use-prev', out).onclick = () => { if (ta) ta.value = r[0].answer; else if (sel) sel.value = r[0].answer; out.innerHTML = ''; }; };
    const cleanBtn = $('.btn-clean', card); if (cleanBtn) cleanBtn.onclick = () => cleanup(ta.value, false);
    const mic = $('.mic', card);
    async function cleanup(raw, fromVoice) {
      if (!raw.trim()) return;
      out.innerHTML = '<span class="sub">Cleaning up…</span>';
      try {
        const r = await api('/voice/clean', { method: 'POST', body: { raw, application_id: appId, question: q.label } });
        voice = { raw: fromVoice ? raw : voice.raw, cleaned: r.cleaned, confidence: r.confidence, ai_modified: r.ai_modified, source: fromVoice ? 'voice' : 'typed' };
        ta.value = r.cleaned;
        let html = `<span class="conf ${r.confidence}">${r.confidence} confidence</span>`;
        if (r.guard_rejected) html += ' <span class="sub">(AI clean-up added content and was rejected; showing your words)</span>';
        const spans = (r.spans || []).filter((s) => s.suggestion && s.suggestion.toLowerCase() !== s.text.toLowerCase());
        if (spans.length) html += `<div style="margin-top:6px">I may have misheard: ${spans.map((s) => `“<span class="span-uncertain">${esc(s.text)}</span>”`).join(', ')}</div><div class="chips">${spans.map((s, i) => `<button class="chip fix" data-i="${i}">Did you mean “${esc(s.suggestion)}”?</button>`).join('')}</div>`;
        if (r.changes && r.changes.length) html += `<div class="sub" style="margin:4px 0 0;font-size:12px">${r.changes.slice(0, 6).map(esc).join(' · ')}</div>`;
        out.innerHTML = html;
        out.querySelectorAll('.fix').forEach((b) => b.onclick = () => { const s = spans[+b.dataset.i]; ta.value = ta.value.replace(new RegExp(s.text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'i'), s.suggestion); b.remove(); });
      } catch (e) { out.innerHTML = `<span class="sub">Clean-up unavailable (${esc(e.message)}); your words are kept.</span>`; }
    }
    if (mic) attachMic(mic, interim, ta, (raw) => cleanup(raw, true));
    return card;
  }

  // ---------------------------------------------------------------- voice capture: Web Speech API (streaming) with server fallback
  function attachMic(btn, interimEl, ta, onFinal) {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    let rec = null, recording = false, mediaRec = null, chunks = [];
    const stop = () => { recording = false; btn.classList.remove('rec'); btn.textContent = '🎙 Speak'; };
    btn.onclick = async () => {
      if (recording) { if (rec) rec.stop(); if (mediaRec && mediaRec.state !== 'inactive') mediaRec.stop(); return; }
      recording = true; btn.classList.add('rec'); btn.textContent = '■ Stop'; interimEl.textContent = 'Listening…';
      if (SR) {
        rec = new SR(); rec.continuous = true; rec.interimResults = true; rec.lang = 'en-US';
        let finalText = ta.value ? ta.value.trim() + ' ' : '';
        rec.onresult = (e) => { let interim = ''; for (let i = e.resultIndex; i < e.results.length; i++) { const t = e.results[i][0].transcript; if (e.results[i].isFinal) finalText += t + ' '; else interim += t; } ta.value = finalText + interim; interimEl.textContent = interim ? '…' + interim : ''; };
        rec.onerror = async (e) => { if (e.error === 'not-allowed') { toast('Microphone permission denied', 'danger'); stop(); } else if (e.error === 'network' || e.error === 'audio-capture') { rec = null; stop(); await serverCapture(); } else { interimEl.textContent = 'Error: ' + e.error; } };
        rec.onend = () => { const was = recording; stop(); interimEl.textContent = ''; if (was !== null && finalText.trim()) onFinal(finalText.trim()); };
        try { rec.start(); } catch (_) { rec = null; stop(); await serverCapture(); }
      } else { await serverCapture(); }
    };
    async function serverCapture() {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        recording = true; btn.classList.add('rec'); btn.textContent = '■ Stop'; interimEl.textContent = 'Recording (server transcription)…';
        chunks = []; mediaRec = new MediaRecorder(stream); mediaRec.ondataavailable = (e) => chunks.push(e.data);
        mediaRec.onstop = async () => { stream.getTracks().forEach((t) => t.stop()); stop(); interimEl.textContent = 'Transcribing…'; btn.classList.add('busy');
          try { const fd = new FormData(); fd.append('file', new Blob(chunks, { type: 'audio/webm' }), 'audio.webm'); const r = await fetch('/api/voice/transcribe', { method: 'POST', body: fd }); if (!r.ok) throw new Error((await r.json()).detail || r.statusText); const j = await r.json(); ta.value = (ta.value ? ta.value.trim() + ' ' : '') + j.text; interimEl.textContent = ''; onFinal(ta.value.trim()); }
          catch (e) { interimEl.textContent = ''; toast(e.message, 'danger'); } finally { btn.classList.remove('busy'); } };
        mediaRec.start();
      } catch (e) { stop(); toast('Microphone unavailable: ' + e.message, 'danger'); }
    }
  }

  // ---------------------------------------------------------------- profile / settings / debug
  pages['/profile'] = async () => {
    const d = await api('/profile'); const p = d.profile;
    const sections = ['name', 'contact', 'address', 'education', 'work_authorization', 'eeo', 'preferences'];
    main.innerHTML = `<h1>Profile</h1><p class="sub">Facts the AI may use. Values marked [FILL IN] are never typed into a form; the question is asked of you instead.${d.placeholders.length ? ` <b>${d.placeholders.length} placeholder(s) left.</b>` : ''}</p>
      <div class="grid cols-2">${sections.map((s) => `<div class="card"><h3>${esc(s.replace(/_/g, ' '))}</h3>${Object.entries(p[s] || {}).map(([k, v]) => `<label>${esc(k.replace(/_/g, ' '))}</label><input data-sec="${s}" data-key="${k}" value="${esc(v)}" ${String(v).includes('[FILL IN') ? 'style="border-color:var(--warn)"' : ''}>`).join('')}</div>`).join('')}
      <div class="card"><h3>Resume</h3><div class="sub">${esc(d.resume_pdf)} ${d.resume_exists ? '✓' : '<span style="color:var(--danger)">missing</span>'}</div><pre style="white-space:pre-wrap;max-height:300px">${esc(d.resume_text)}</pre></div></div>
      <div class="row" style="margin-top:14px"><button class="btn" id="p-save">Save profile</button></div>`;
    $('#p-save').onclick = async () => { const body = {}; main.querySelectorAll('input[data-sec]').forEach((i) => { body[i.dataset.sec] = body[i.dataset.sec] || {}; body[i.dataset.sec][i.dataset.key] = i.value; }); await api('/profile', { method: 'PATCH', body }); toast('Profile saved', 'ok'); render(); };
  };

  pages['/settings'] = async () => {
    const [s, es] = await Promise.all([api('/settings'), api('/email/status')]);
    const pending = es.needs_confirmation ? await api('/email/events?match_status=needs_confirmation') : [];
    main.innerHTML = `<h1>Settings</h1>
      <h2>Email</h2><div class="card">${es.connected ? `<div class="row between wrap"><div>Connected: <b>${esc(es.account.email)}</b> · last sync ${fmt(es.account.last_sync_at) || 'never'} ${es.syncing ? '· syncing…' : ''}${es.last_error ? `<div style="color:var(--danger)">${esc(es.last_error)}</div>` : ''}<div class="sub" style="margin:4px 0 0">Read-only Gmail access. Only job-related emails are processed; nothing else is stored.</div></div><div class="row"><button class="btn secondary" id="e-sync">Sync now</button><button class="btn danger" id="e-disc">Disconnect</button></div></div>` :
        `<div class="row between wrap"><div>Connect Gmail (read-only) so assessments, interviews and rejections update your tracker automatically.<div class="sub" style="margin:4px 0 0">Requires a Google OAuth desktop client file at <code>${esc(es.client_file)}</code> ${es.client_file_exists ? '✓' : '— <span style="color:var(--warn)">missing</span> (see README → Email)'}${es.last_error ? `<div style="color:var(--danger)">${esc(es.last_error)}</div>` : ''}</div></div><button class="btn" id="e-conn" ${es.client_file_exists ? '' : 'disabled'}>Connect Gmail</button></div>`}
        ${pending.length ? `<h3 style="margin-top:14px">Possible matches (${pending.length})</h3>${pending.map((e) => `<div class="action"><div style="flex:1"><b>${esc(e.subject)}</b> ${pill(e.category)}<div class="sub" style="margin:0">${esc(e.sender)} · ${fmt(e.received_at)}</div><div class="sub" style="margin:0">Candidates: ${(e.candidates || []).map((c) => `${esc(c.company)} — ${esc(c.title)} (${c.score})`).join(' · ')}</div></div><div class="row"><select data-link="${e.id}"><option value="">Choose application…</option>${(e.candidates || []).map((c) => `<option value="${c.application_id}">${esc(c.company)} — ${esc(c.title)}</option>`).join('')}</select><button class="btn sm" data-confirm="${e.id}">Confirm</button><button class="ghost" data-ignore="${e.id}">Ignore</button></div></div>`).join('')}` : ''}
        <div class="sub" style="margin:10px 0 0">${es.unmatched ? `${es.unmatched} job-related email(s) could not be matched to an application.` : ''}</div></div>
      <h2>AI</h2><div class="card"><div class="kv"><dt>Anthropic key</dt><dd>${s.has_anthropic_key ? '✓ set in .env' : '<span style="color:var(--danger)">missing — inference, drafts, research and transcript clean-up are off</span>'}</dd><dt>Model</dt><dd>${esc(s.llm.model)}</dd><dt>Fast model (dictation)</dt><dd>${esc(s.llm.fast_model || '')}</dd><dt>Min confidence</dt><dd>${esc(s.llm.min_confidence)}</dd><dt>Company research</dt><dd>${s.llm.web_research ? 'on' : 'off'}</dd></div></div>
      <h2>Voice</h2><div class="card"><div class="sub" style="margin:0">Questions are shown, never read aloud. Speech is captured in this browser (Chrome/Edge/Safari) and cleaned up with context. Any system-wide dictation tool (e.g. Wispr Flow) also works: dictate straight into the answer box and press “Clean up”.</div><label style="text-transform:none;display:flex;gap:6px;align-items:center;margin-top:10px"><input type="checkbox" id="v-llm" ${s.voice.llm_cleanup ? 'checked' : ''} style="width:auto"> Use the fast model for punctuation / homophone clean-up</label></div>
      <h2>Filters (defaults for AutoApply)</h2><div class="card"><pre>${esc(JSON.stringify(s.filters, null, 2))}</pre><div class="sub">Edit config/settings.yaml for advanced options.</div></div>`;
    const ec = $('#e-conn'); if (ec) ec.onclick = async () => { try { const r = await api('/email/connect', { method: 'POST' }); toast(r.message || 'Connecting…', 'ok'); setTimeout(render, 8000); } catch (e) { toast(e.message, 'danger'); } };
    const ed = $('#e-disc'); if (ed) ed.onclick = async () => { if (confirm('Disconnect Gmail and delete the stored token?')) { await api('/email/disconnect', { method: 'POST' }); render(); } };
    const esy = $('#e-sync'); if (esy) esy.onclick = async () => { await api('/email/sync', { method: 'POST' }); toast('Sync started', 'ok'); };
    $('#v-llm').onchange = (e) => api('/settings', { method: 'PATCH', body: { voice: { llm_cleanup: e.target.checked } } });
    main.querySelectorAll('[data-confirm]').forEach((b) => b.onclick = async () => { const sel = main.querySelector(`select[data-link="${b.dataset.confirm}"]`); if (!sel.value) return toast('Choose an application', 'warn'); await api(`/email/events/${b.dataset.confirm}/link`, { method: 'POST', body: { application_id: +sel.value } }); render(); });
    main.querySelectorAll('[data-ignore]').forEach((b) => b.onclick = async () => { await api(`/email/events/${b.dataset.ignore}/link`, { method: 'POST', body: { application_id: null } }); render(); });
    refreshers.add(async (ev) => { if (['email_status', 'email_sync'].includes(ev.kind)) render(); });
  };

  pages['/debug'] = async () => {
    const d = await api('/debug');
    main.innerHTML = `<h1>Debug</h1><p class="sub">Developer view: session state, recent bus events, email monitor.</p>
      <div class="card"><h3>Session</h3><pre>${esc(JSON.stringify(d.session, null, 2))}</pre></div>
      <div class="card" style="margin-top:12px"><h3>Recent events</h3><pre id="dbg-events">${esc(d.bus_history.map((e) => `${new Date(e.ts * 1000).toLocaleTimeString()} ${e.kind} ${e.type || ''} ${e.message || ''}`).join('\n'))}</pre></div>
      <div class="card" style="margin-top:12px"><h3>Email</h3><pre>${esc(JSON.stringify(d.email, null, 2))}</pre></div>`;
    refreshers.add((ev) => { const p = $('#dbg-events'); if (p) p.textContent += `\n${new Date().toLocaleTimeString()} ${ev.kind} ${ev.type || ''} ${ev.message || ''}`; });
  };

  // ---------------------------------------------------------------- router
  async function render() {
    refreshers.clear();
    const hash = location.hash.replace(/^#/, '') || '/';
    route = hash;
    document.querySelectorAll('nav a').forEach((a) => a.classList.toggle('active', a.dataset.route === (hash.startsWith('/applications/') ? '/applications' : hash)));
    const m = hash.match(/^\/applications\/(\d+)$/);
    try { if (m) await pages['/applications/:id'](m[1]); else if (pages[hash]) await pages[hash](); else main.innerHTML = '<div class="empty">Not found.</div>'; }
    catch (e) { main.innerHTML = `<div class="card"><b>Error:</b> ${esc(e.message)}</div>`; console.error(e); }
  }
  window.addEventListener('hashchange', render);
  connectSSE(); loadNotifications(); render();
})();
