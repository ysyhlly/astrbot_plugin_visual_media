(() => {
 const UI=window.SeriesUI||{t:(s,p={})=>String(s).replace(/\{(\w+)\}/g,(m,k)=>Object.hasOwn(p,k)?String(p[k]??''):m),msg(s,p={}){return this.t(s,p);},text(n,v){n.textContent=String(v??'');return n;},bind(n,s,p={},a=''){if(a)n.setAttribute(a,this.t(s,p));else n.textContent=this.t(s,p);return n;},onLocaleChange:()=>()=>{}};

  const root = document.documentElement;
  const script = document.currentScript;
  const base = new URL('../', script.src);
  const query = new URLSearchParams(location.search);
  const validTheme = (value) => value === 'day' || value === 'night';
  let storedTheme;
  let activeUmo = query.get('umo')?.trim() || '';
  try { storedTheme = localStorage.getItem('chat_dynamics_ui'); } catch { /* An opaque iframe can deny storage. */ }
  const queryTheme = query.get('ui');
  let theme = validTheme(queryTheme) ? queryTheme : validTheme(storedTheme) ? storedTheme : 'day';
  const page = document.body.dataset.workshopPage || 'home';
  const selected = page === 'home' && query.get('section') === 'characters' ? 'characters' : page;
  const links = [
    ['home', '总览', 'index.html'],
    ['characters', '角色形象', 'index.html?section=characters'],
    ['memes', '表情图库', 'memes/index.html'],
    ['studio', '生图创作', 'studio/index.html'],
    ['tasks', '生成任务', 'tasks/index.html'],
    ['behavior', '表达与记录', 'behavior/index.html'],
  ];
  const header = document.createElement('header');
  header.className = 'series-nav';
  header.innerHTML = `<a class="series-brand" href="${new URL('index.html', base)}"><span class="series-mark" aria-hidden="true">VM</span><span data-series-text="视觉工坊">视觉工坊</span></a>
    <nav class="series-tabs" aria-label="工坊导航" data-series-aria-label="工坊导航">${links.map(([key, label, href]) =>
      `<a href="${new URL(href, base)}"${key === selected ? ' aria-current="page"' : ''} data-workshop-route data-series-text="${label}">${label}</a>`).join('')}</nav>
    <div class="series-nav-tools"><button class="series-theme" id="vm-theme-toggle" type="button" aria-pressed="false"><span class="ui-theme-label">日间模式</span><span class="series-switch" aria-hidden="true"></span></button></div>`;
  document.body.classList.add('vm-layout');
  document.body.prepend(header);
  const skip = document.createElement('a');
  skip.className = 'series-skip';
  skip.href = page === 'memes' ? '#app' : '#workspace';
  skip.textContent = '跳到工作区';
  skip.dataset.seriesText = '跳到工作区';
  header.before(skip);
  const toggle = document.getElementById('vm-theme-toggle');

  const carryContext = () => {
    document.querySelectorAll('a[href]').forEach(link => {
      try {
        const url = new URL(link.getAttribute('href'), location.href);
        if (url.origin !== base.origin || !url.pathname.startsWith(base.pathname) || !url.pathname.endsWith('.html') || link.getAttribute('href')?.startsWith('#')) return;
        url.searchParams.set('ui', theme);
        if (activeUmo || query.has('umo') || link.hasAttribute('data-workshop-route')) url.searchParams.set('umo', activeUmo);
        else url.searchParams.delete('umo');
        const language = window.getLanguage?.() || query.get('lang');
        if (language) url.searchParams.set('lang', language);
        link.href = url.href;
      } catch { /* Ignore non-workshop or malformed links. */ }
    });
  };
  function apply(value, save = false) {
    if (!validTheme(value)) return;
    theme = value;
    const night = value === 'night';
    root.dataset.uiTheme = value;
    root.dataset.theme = night ? 'dark' : 'light';
    root.style.colorScheme = night ? 'dark' : 'light';
    toggle.setAttribute('aria-pressed', String(night));
    toggle.querySelector('.ui-theme-label').textContent = UI.t(night ? '夜间模式' : '日间模式');
    toggle.setAttribute('aria-label', UI.t(night ? '切换到日间模式' : '切换到夜间模式'));
    if (save) {
      try { localStorage.setItem('chat_dynamics_ui', value); } catch { /* URL keeps the preference for this visit. */ }
      try {
        const url = new URL(location.href);
        url.searchParams.set('ui', value);
        history.replaceState(history.state, '', url);
      } catch { /* In-page controls still update in a sandboxed view. */ }
    }
    carryContext();
  }
  toggle.addEventListener('click', () => apply(theme === 'night' ? 'day' : 'night', true));
  window.addEventListener('storage', ({key, newValue}) => {
    if (key === 'chat_dynamics_ui' && validTheme(newValue)) apply(newValue);
  });
  window.addEventListener('dynamics-scopechange', e => {
    activeUmo = String(e.detail?.umo ?? '').trim();
    carryContext();
  });
  window.addEventListener('series-languagechange', carryContext);
  window.DynamicsGetActiveScope = () => activeUmo;
  window.VisualMediaUI = {current: () => theme, apply};
  apply(theme);

  function setupStudioRemixPrefill() {
    if (page !== 'studio') return;
    const remixChar = (query.get('character') || '').trim();
    const remixPrompt = (query.get('prompt') || '').trim();
    const hasReuse = Boolean((query.get('reuse') || '').trim());
    if (!remixChar && !remixPrompt) return;
    const applyPrefill = () => {
      const host = document.getElementById('panel-workbench');
      if (host && !document.getElementById('vm-remix-banner')) {
        const banner = document.createElement('div');
        banner.id = 'vm-remix-banner';
        banner.className = 'vm-remix-banner';
        const parts = [];
        if (remixChar) parts.push(`${UI.t('角色')}：${remixChar}`);
        if (remixPrompt) parts.push(`${UI.t('画面描述')}：${remixPrompt.slice(0, 96)}${remixPrompt.length > 96 ? '…' : ''}`);
        const textSpan = document.createElement('span');
        textSpan.textContent = `${UI.t('已带入重绘上下文')} · ${parts.join(' ｜ ')}`;
        banner.append(textSpan);
        host.prepend(banner);
      }
      if (hasReuse) return true;
      const input = document.getElementById('input-prompt');
      if (!input) return false;
      const combined = remixChar && remixPrompt && !remixPrompt.includes(remixChar)
        ? `[${remixChar}] ${remixPrompt}`
        : (remixPrompt || remixChar);
      if (combined && !input.value) {
        const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
        if (setter) setter.call(input, combined);
        else input.value = combined;
        input.dispatchEvent(new Event('input', { bubbles: true }));
      }
      return Boolean(input.value);
    };
    if (!applyPrefill()) {
      const obs = new MutationObserver(() => {
        if (applyPrefill()) obs.disconnect();
      });
      obs.observe(document.body, { childList: true, subtree: true });
      setTimeout(() => obs.disconnect(), 6000);
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    carryContext();
    setupStudioRemixPrefill();
  }, {once:true});
})();
