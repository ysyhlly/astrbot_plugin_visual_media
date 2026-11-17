/* Shared presentation contract. Catalogs come from AstrBot's existing i18n context. */
(() => {
  if (window.SeriesUI) return;
  const root = document.documentElement;
  const supported = {zh:'zh-CN',en:'en-US',ja:'ja-JP',ru:'ru-RU'};
  const bindings = new Map(), listeners = new Set();
  let context = {}, activeLocale = 'zh-CN', subscribed = false, lastLocale = '', lastCatalog = '';
  const bridge = () => window.AstrBotPluginPage || window.AstrBotPluginView;
  const normalize = value => supported[String(value || '').toLowerCase().split('-')[0]] || 'zh-CN';
  function locale() { return typeof window.getLanguage === 'function' ? normalize(window.getLanguage()) : activeLocale; }
  function t(source, params = {}) {
    const catalog = context.i18n?.[locale()]?.series_ui || {};
    const fallback = window.DynamicsUI?.tr?.(source, ...Object.values(params));
    const value = typeof catalog[source] === 'string' ? catalog[source] : fallback ?? source;
    return String(value).replace(/\{(\w+)\}/g, (match, key) => Object.hasOwn(params,key) ? String(params[key] ?? '') : match);
  }
  function bind(node, source, params = {}, attribute = '') {
    if (!node) return node;
    let entries = bindings.get(node);
    if (!entries) { entries = new Map(); bindings.set(node, entries); }
    entries.set(attribute,{source,params});
    if (attribute) node.setAttribute(attribute,t(source,params)); else node.textContent=t(source,params);
    return node;
  }
  const msg = (source, params={}) => ({__series_message:true,source,params});
  function text(node, value) {
    if (value?.__series_message === true) return bind(node,value.source,value.params);
    bindings.get(node)?.delete('');
    node.textContent=String(value ?? ''); return node;
  }
  function applyStatic(scope=document) {
    scope.querySelectorAll('[data-series-i18n]').forEach(n=>bind(n,n.dataset.seriesI18n));
    for (const [data,attr] of [['seriesPlaceholder','placeholder'],['seriesAria','aria-label'],['seriesTitle','title']]) {
      scope.querySelectorAll(`[data-${data.replace(/[A-Z]/g,c=>'-'+c.toLowerCase())}]`).forEach(n=>bind(n,n.dataset[data],{},attr));
    }
  }
  function refreshLanguage() {
    activeLocale=normalize(context.locale || root.lang || navigator.language);
    root.lang=locale();
    const signature=JSON.stringify(context.i18n?.[locale()]?.series_ui||{});
    if(lastLocale===locale()&&lastCatalog===signature){syncThemeLabels();return;}
    lastLocale=locale();lastCatalog=signature;
    for(const [node,entries] of bindings){
      if(!node.isConnected){bindings.delete(node);continue;}
      for(const [attribute,{source,params}] of entries) {
        if(attribute)node.setAttribute(attribute,t(source,params));else node.textContent=t(source,params);
      }
    }
    syncThemeLabels();tableLabels();
    for(const callback of listeners)callback(locale());
  }
  function notice(node, message, state='info') {
    if(!node)return;
    node.hidden=!message;node.dataset.uiState=state;
    node.setAttribute('role',state==='error'?'alert':'status');
    node.setAttribute('aria-live',state==='error'?'assertive':'polite');
    text(node,message?.__series_message?message:msg(String(message||'')));
  }
  async function action(button, operation) {
    if(button.disabled)return;
    const label=button.textContent;button.disabled=true;button.setAttribute('aria-busy','true');
    button.classList.add('series-busy');
    try{return await operation();}
    finally{button.disabled=false;button.removeAttribute('aria-busy');button.classList.remove('series-busy');if(!button.textContent)button.textContent=label;}
  }
  function syncThemeLabels(){
    const night=root.dataset.uiTheme==='night';
    for(const id of ['theme-label','series-theme-label']){const n=document.getElementById(id);if(n)bind(n,night?'夜间模式':'日间模式');}
    const n=document.querySelector('.dw-theme-label');if(n)bind(n,night?'夜间模式':'日间模式');
    for(const id of ['theme-toggle','dwTheme','series-theme-toggle']){
      const b=document.getElementById(id);if(b && (root.dataset.seriesOwner!=='memory'||id==='series-theme-toggle')) {
        b.setAttribute('aria-label',t(night?'切换到日间模式':'切换到夜间模式'));
      }
    }
  }
  function memoryTheme(){
    if(root.dataset.seriesOwner!=='memory')return;
    const paint=value=>{window.LMAppearance?.setSharedTheme?.(value);root.dataset.uiTheme=value==='night'?'night':'day';root.style.colorScheme=value==='night'?'dark':'light';syncThemeLabels();};
    let stored;try{stored=localStorage.getItem('chat_dynamics_ui');}catch{}
    paint(new URLSearchParams(location.search).get('ui')||stored||'day');
    const button=document.createElement('button');button.type='button';button.id='series-theme-toggle';button.className='series-theme-toggle';
    const label=document.createElement('span');label.id='series-theme-label';button.append(label);button.onclick=()=>{
      const next=root.dataset.uiTheme==='night'?'day':'night';paint(next);
      try{localStorage.setItem('chat_dynamics_ui',next);const url=new URL(location.href);url.searchParams.set('ui',next);history.replaceState(history.state,'',url);}catch{}
      button.setAttribute('aria-pressed',String(next==='night'));
    };
    const footer=document.querySelector('.sidebar-footer');if(footer)footer.prepend(button);
    button.setAttribute('aria-pressed',String(root.dataset.uiTheme==='night'));syncThemeLabels();
    window.addEventListener('storage',e=>{if(e.key==='chat_dynamics_ui'&&['day','night'].includes(e.newValue))paint(e.newValue);});
  }
  function mobileNavigation(){
    const selectors={memory:['.sidebar','.sidebar-nav','.nav-item[data-page]'],world:['.dw-navigation','#wbTabs','[data-view]'],learning:['.side-rail','.side-nav','[data-tab]']};
    const selected=selectors[root.dataset.seriesOwner];if(!selected)return;
    const rail=document.querySelector(selected[0]),nav=document.querySelector(selected[1]);if(!rail||!nav)return;
    if(!nav.id)nav.id='series-workspace-nav';
    const toggle=document.createElement('button');toggle.type='button';toggle.className='series-mobile-nav';toggle.setAttribute('aria-controls',nav.id);toggle.setAttribute('aria-expanded','false');bind(toggle,'切换工作区');rail.append(toggle);
    const shortcut={memory:['[data-page="memory"]','记忆列表'],world:['[data-view="books"]','世界书'],learning:['[data-tab="proposals"]','调整审查']}[root.dataset.seriesOwner];
    const quick=document.createElement('button');quick.type='button';quick.className='series-mobile-nav series-quick-nav';bind(quick,shortcut[1]);quick.onclick=()=>nav.querySelector(shortcut[0])?.click();rail.insertBefore(quick,toggle);
    function close(){root.dataset.seriesNavOpen='false';toggle.setAttribute('aria-expanded','false');}
    toggle.onclick=()=>{const open=toggle.getAttribute('aria-expanded')!=='true';root.dataset.seriesNavOpen=String(open);toggle.setAttribute('aria-expanded',String(open));};
    nav.addEventListener('click',event=>{if(event.target.closest?.(selected[2])&&window.matchMedia('(max-width: 760px)').matches){close();toggle.focus({preventScroll:true});}});
    document.addEventListener('keydown',event=>{if(event.key==='Escape'&&root.dataset.seriesNavOpen==='true'){close();toggle.focus({preventScroll:true});}});
    function current(){nav.querySelectorAll(selected[2]).forEach(button=>{
      const active=button.classList.contains('active')||button.classList.contains('selected')||button.getAttribute('aria-selected')==='true';
      if(active)button.setAttribute('aria-current','page');else button.removeAttribute('aria-current');
    });}
    new MutationObserver(current).observe(nav,{subtree:true,attributes:true,attributeFilter:['class','aria-selected']});current();
  }
  function tableLabels(scope=document){
    scope.querySelectorAll('table.series-responsive-table').forEach(table=>{
      const headers=[...table.querySelectorAll('thead th, tr:first-child th')].map(n=>n.textContent);
      table.querySelectorAll('tr').forEach(row=>row.querySelectorAll('td').forEach((cell,index)=>cell.dataset.label=headers[index]||''));
    });
  }
  function boot(){
    applyStatic();memoryTheme();mobileNavigation();syncThemeLabels();tableLabels();
    new MutationObserver(()=>syncThemeLabels()).observe(root,{attributes:true,attributeFilter:['data-ui-theme']});
  }
  window.SeriesUI={t,msg,text,bind,notice,action,locale,applyStatic,tableLabels,onLocaleChange(fn){listeners.add(fn);return()=>listeners.delete(fn);}};
  setInterval(()=>{for(const [node] of bindings)if(!node.isConnected)bindings.delete(node);},30000);
  if(root.dataset.seriesOwner)root.classList.add('series-aligned');
  try{context=bridge()?.getContext?.()||{};}catch{}
  activeLocale=normalize(context.locale||root.lang);window.addEventListener('languagechange',refreshLanguage);window.addEventListener('series-languagechange',refreshLanguage);
  const subscribe=()=>{const b=bridge();if(b?.onContext&&!subscribed){subscribed=true;b.onContext(next=>{context={...context,...next};refreshLanguage();});}try{context={...context,...b?.getContext?.()};}catch{}refreshLanguage();};
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',()=>{boot();subscribe();},{once:true});else{boot();subscribe();}
  if(bridge()?.ready)Promise.resolve(bridge().ready()).then(subscribe).catch(()=>{});
})();
