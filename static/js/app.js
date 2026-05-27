

const IC = {
  check:`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>`,
  x:`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`,
  warn:`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>`,
  info:`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>`,
  copy:`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>`,
  eye:`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>`,
  eyeoff:`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17.94 17.94A10.07 10.07 0 0112 20c-7 0-11-8-11-8a18.45 18.45 0 015.06-5.94"/><path d="M9.9 4.24A9.12 9.12 0 0112 4c7 0 11 8 11 8a18.5 18.5 0 01-2.16 3.19"/><line x1="1" y1="1" x2="23" y2="23"/></svg>`,
  bolt:`<svg width="40" height="40" viewBox="0 0 24 24"><path fill="#8D1B28" d="M12 18.857a6.858 6.858 0 0 0 6.36-9.429h-3.34a.43.43 0 0 0-.373.219l-.591 1.067h1.371a.43.43 0 0 1 .429.429v1.285a.43.43 0 0 1-.428.429h-2.572l-2.036 3.639a.43.43 0 0 1-.373.218H7.02A6.84 6.84 0 0 0 12 18.857M5.64 14.57a6.857 6.857 0 0 1 11.34-7.286h-3.214a.43.43 0 0 0-.373.22l-1.796 3.209H9a.43.43 0 0 0-.429.429v1.285a.43.43 0 0 0 .429.429h1.4l-.835 1.496a.43.43 0 0 1-.373.218z"/></svg>`,
  home:`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 9l9-7 9 7v11a2 2 0 01-2 2H5a2 2 0 01-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg>`,
  addr:`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0116 0z"/><circle cx="12" cy="10" r="3"/></svg>`,
  pay:`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 000 7h5a3.5 3.5 0 010 7H6"/></svg>`,
  clock:`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>`,
  users:`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87"/><path d="M16 3.13a4 4 0 010 7.75"/></svg>`,
  chart:`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>`,
  log:`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>`,
  server:`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/></svg>`,
  logout:`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>`,
  settings:`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.07 4.93l-1.41 1.41M4.93 4.93l1.41 1.41M4.93 19.07l1.41-1.41M19.07 19.07l-1.41-1.41M12 2v2M12 20v2M2 12h2M20 12h2"/></svg>`,
  lock:`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0110 0v4"/></svg>`,
  unlock:`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 019.9-1"/></svg>`,
  dot:`<svg viewBox="0 0 8 8"><circle cx="4" cy="4" r="4" fill="currentColor"/></svg>`,
  refresh:`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.49 15"/></svg>`,
};


const Store = {
  get:(k)=>{try{return JSON.parse(localStorage.getItem(k))}catch{return null}},
  set:(k,v)=>localStorage.setItem(k,JSON.stringify(v)),
  // Token storage is now the httponly access_token cookie managed by the server.
  // These helpers are kept as no-ops for backward compatibility with older screens.
  getToken:()=>null,
  setTokens:(_at,_rt)=>{try{['access_token','refresh_token'].forEach(k=>localStorage.removeItem(k))}catch(_){} },
  clearTokens:()=>{try{['access_token','refresh_token'].forEach(k=>localStorage.removeItem(k))}catch(_){} },
};


function escHtml(s){
  if(s==null)return'';
  return String(s)
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;')
    .replace(/'/g,'&#39;');
}


const API = {
  // Endpoints that SHOULD trigger auto-logout when they return 401.
  // Everything else just returns an error object — stale/slow token on
  // a single request must NOT kick the user out of the whole app.
  _AUTH_CRITICAL: /\/api\/auth\/(me|refresh)(\?|$)/,
  async _fetch(m,p,b=null,retry=true){
    const h={'Content-Type':'application/json'};
    const o={method:m,headers:h,credentials:'include',cache:'no-store'};if(b)o.body=JSON.stringify(b);
    let r=await fetch(p,o);
    if(r.status===401&&retry){
      const ok=await API._ref();
      if(ok)return API._fetch(m,p,b,false);
      // Only force logout when the call is an explicit auth check.
      // Plain API calls just bubble up the 401 so the caller can show
      // a non-destructive error instead of ejecting the whole session.
      if (API._AUTH_CRITICAL.test(p)) { Auth.logout(); return null; }
    }
    return r;
  },
  async _ref(){
    try{
      const r=await fetch('/api/auth/refresh',{method:'POST',credentials:'include',cache:'no-store',headers:{'Content-Type':'application/json'},body:'{}'});
      return r.ok;
    }catch{return false}
  },
  async json(m,p,b=null){
    const r=await API._fetch(m,p,b);
    if(!r)return{error:'Unauthorized'};
    const d=await r.json().catch(()=>({}));
    if(!r.ok){
      // If the detail is a structured object (e.g. { error, message,
      // requires_2fa_enable }), preserve it so callers can branch on it.
      if (d && typeof d.detail === 'object' && d.detail !== null) {
        const det = d.detail;
        return { error: det.message || det.error || `Error ${r.status}`, details: det };
      }
      return{error:d.detail||d.message||`Error ${r.status}`};
    }
    return d;
  },
  get:(p)=>API.json('GET',p),
  post:(p,b)=>API.json('POST',p,b),
  put:(p,b)=>API.json('PUT',p,b),
  patch:(p,b)=>API.json('PATCH',p,b),
};


const Auth = {
  _cachedIsLoggedIn: null,
  _bc: (() => {
    // Cross-tab auth state broadcaster. When a user signs out in one tab,
    // every other open tab gets notified and drops its cached login state —
    // so stale tabs don't bounce between /login and / on their next action.
    try { return new BroadcastChannel('firogate-auth'); } catch(_) { return null; }
  })(),
  async checkSession(){
    try{
      const r=await fetch('/api/auth/me',{credentials:'include',cache:'no-store'});
      Auth._cachedIsLoggedIn = r.ok;
      return r.ok;
    }catch{ Auth._cachedIsLoggedIn=false; return false; }
  },
  isLoggedIn(){ return Auth._cachedIsLoggedIn === true; },
  async login(u,p,totp){
    const body={username:u,password:p};
    if(totp)body.totp_code=totp;
    const r=await fetch('/api/auth/login',{method:'POST',credentials:'include',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d=await r.json().catch(()=>({}));
    if(!r.ok)return{error:d.detail||`Error ${r.status}`,...d};
    return d;
  },
  async register(u,e,p,agreed=false){
    const r=await fetch('/api/auth/register',{method:'POST',credentials:'include',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,email:e,password:p,agreed_to_terms:agreed})});
    const d=await r.json().catch(()=>({}));
    if(!r.ok)return{error:d.detail||`Error ${r.status}`};
    return d;
  },
  async logout(){
    try{ await fetch('/api/auth/logout',{method:'POST',credentials:'include',cache:'no-store'}); }catch(_){ }
    Store.clearTokens();
    Auth._cachedIsLoggedIn=false;
    try { if (Auth._bc) Auth._bc.postMessage({ type:'logout', ts: Date.now() }); } catch(_){}
    if (window._fgLoopBreak) { console.warn('[Auth] circuit-breaker armed — skipping logout redirect'); return; }
    if (!Auth._navGuardCheck()) { console.warn('[Auth] nav loop — skipping logout redirect'); return; }
    window.location.href=window.LOGIN_URL||'/login';
  },
  async requireAuth(){
    if (window._fgLoopBreak) { console.warn('[Auth] circuit-breaker armed — staying put'); return; }
    const ok = await Auth.checkSession();
    if (ok) return;
    await new Promise(res => setTimeout(res, 600));
    const ok2 = await Auth.checkSession();
    if (ok2) return;
    if (window._fgLoopBreak) { console.warn('[Auth] circuit-breaker armed — staying put'); return; }
    if (!Auth._navGuardCheck()) { console.warn('[Auth] nav loop — staying put'); return; }
    window.location.href = window.LOGIN_URL || '/login';
  },
  async requireGuest(){
    if (window._fgLoopBreak) { console.warn('[Auth] circuit-breaker armed — staying put'); return; }
    const ok = await Auth.checkSession();
    if (!ok) return;
    if (!Auth._navGuardCheck()) { console.warn('[Auth] nav loop — staying put'); return; }
    window.location.href = window.DASHBOARD_URL || '/';
  },
  // Loop-guard: if more than 2 auth navigations fire within 10s, block further
  // ones. Previously set to 3, but nginx's burst limit 503s kicked in before
  // we could react, so we stop one step earlier.
  _navGuardCheck() {
    try {
      const now = Date.now();
      const raw = sessionStorage.getItem('fg_auth_nav');
      const arr = raw ? JSON.parse(raw) : [];
      const recent = arr.filter(t => now - t < 10000);
      recent.push(now);
      sessionStorage.setItem('fg_auth_nav', JSON.stringify(recent));
      if (recent.length > 2) {
        Auth._showLoopBanner();
        return false;
      }
      return true;
    } catch(_) { return true; }
  },
  _showLoopBanner() { /* banner suppressed — breaker is silent */ },
};


const Toast = {
  _el:null,
  _c(){if(!this._el){this._el=document.getElementById('tc')||Object.assign(document.createElement('div'),{id:'tc'});document.body.appendChild(this._el)}return this._el},
  show(msg,type='info',dur=3500){
    const icons={success:IC.check,error:IC.x,warning:IC.warn,info:IC.info};
    const colors={success:'var(--green)',error:'var(--red)',warning:'var(--yellow)',info:'var(--blue)'};
    const t=document.createElement('div');
    t.className=`toast t-${type==='success'?'ok':type==='error'?'err':'warn'}`;
    // Build with DOM methods — msg via textContent to prevent XSS from server error strings
    const iconSpan=document.createElement('span');
    iconSpan.style.color=colors[type]||'var(--blue)';
    iconSpan.innerHTML=icons[type]||IC.info; // trusted static SVG strings
    const msgSpan=document.createElement('span');
    msgSpan.style.flex='1';
    msgSpan.textContent=msg;
    const closeBtn=document.createElement('button');
    closeBtn.setAttribute('style','background:none;border:none;color:var(--g3);cursor:pointer;line-height:1;padding:0 0 0 8px;font-size:1.1rem');
    closeBtn.textContent='×';
    closeBtn.addEventListener('click',()=>t.remove());
    t.appendChild(iconSpan);t.appendChild(msgSpan);t.appendChild(closeBtn);
    this._c().appendChild(t);
    setTimeout(()=>{t.style.transition='opacity .28s';t.style.opacity='0';setTimeout(()=>t.remove(),280)},dur);
  },
  success:(m,d)=>Toast.show(m,'success',d),
  error:(m,d)=>Toast.show(m,'error',d),
  warning:(m,d)=>Toast.show(m,'warning',d),
  info:(m,d)=>Toast.show(m,'info',d),
};


function copyText(text,label='Copied!'){
  navigator.clipboard.writeText(text).then(()=>Toast.success(label)).catch(()=>{
    const el=Object.assign(document.createElement('textarea'),{value:text,style:'position:fixed;opacity:0'});
    document.body.appendChild(el);el.select();document.execCommand('copy');el.remove();Toast.success(label);
  });
}


function fmtDate(iso){if(!iso)return'—';return new Date(iso).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'})}
function fmtFiro(a){return(parseFloat(a)||0).toFixed(8)+' FIRO'}
function truncate(s,n=16){if(!s||s==='—')return'—';if(s.length<=n)return s;return s.slice(0,8)+'…'+s.slice(-6)}

function badge(s){
  const cls={active:'b-active',confirmed:'b-confirmed',pending:'b-pending',
    confirming:'b-confirming',inactive:'b-inactive',expired:'b-expired',
    failed:'b-failed',admin:'b-admin',user:'b-user',
    spark:'b-spark',transparent:'b-transparent'};
  return`<span class="badge ${cls[s]||'b-inactive'}">${IC.dot}${s}</span>`;
}

function alertHTML(msg,type='err'){
  const m={ok:'al-ok',err:'al-err',warn:'al-warn',info:'al-info'};
  const ic={ok:IC.check,err:IC.x,warn:IC.warn,info:IC.info};

  return`<div class="alert ${m[type]||'al-err'}">${ic[type]||IC.x}${escHtml(msg)}</div>`;
}


const _cdTimers = {};

function startCountdown(iso, id) {
  const el = document.getElementById(id);
  if (!el) return;


  if (_cdTimers[id]) {
    clearTimeout(_cdTimers[id]);
    delete _cdTimers[id];
  }


  let isoStr = String(iso || '');
  if (isoStr && !isoStr.endsWith('Z') && !/[+\-]\d{2}:\d{2}$/.test(isoStr)) {
    isoStr += 'Z';
  }
  const target = new Date(isoStr).getTime();
  if (isNaN(target)) { el.textContent = '--:--'; return; }

  function tick() {
    const diff = target - Date.now();

    if (diff <= 0) {
      el.textContent = '00:00';
      el.classList.add('urgent');
      delete _cdTimers[id];
      return;
    }

    const totalSec = Math.floor(diff / 1000);
    const mins     = Math.floor(totalSec / 60);
    const secs     = totalSec % 60;

    el.textContent = String(mins).padStart(2, '0') + ':' + String(secs).padStart(2, '0');

    if (diff < 300000) el.classList.add('urgent');
    else               el.classList.remove('urgent');


    const delay = (diff % 1000) || 1000;
    _cdTimers[id] = setTimeout(tick, delay);
  }

  tick();
}

function stopCountdown(id) {
  if (_cdTimers[id]) { clearTimeout(_cdTimers[id]); delete _cdTimers[id]; }
}


function showTab(name,sbId,mobId){
  document.querySelectorAll('.tab').forEach(t=>{t.style.display='none';t.classList.remove('fi')});
  document.querySelectorAll('.ni').forEach(i=>i.classList.remove('active'));
  document.querySelectorAll('.mb').forEach(b=>b.classList.remove('active'));
  const tab=document.getElementById('t-'+name);
  if(tab){tab.style.display='block';tab.style.animation='tab-in .25s ease both'}
  if(sbId)document.getElementById(sbId)?.classList.add('active');
  if(mobId)document.getElementById(mobId)?.classList.add('active');
}
