import {beforeEach, describe, expect, it, vi} from 'vitest';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
const source=readFileSync(resolve(process.cwd(),'../views/workshop/shared/shell.js'),'utf8');
function boot(page='home') {
  document.body.dataset.workshopPage=page;
  Object.defineProperty(document,'currentScript',{configurable:true,value:{src:`${location.origin}/api/v1/plugins/visual/views/workshop/_t/test-token/shared/shell.js`}});
  new Function(source)();
}
beforeEach(()=>{
  document.documentElement.innerHTML='<head></head><body></body>';
  history.replaceState({},'', '/api/v1/plugins/visual/views/workshop/_t/test-token/index.html');
  localStorage.clear(); vi.restoreAllMocks();
});
describe('Shared workshop shell',()=>{
  it('keeps every entry inside the signed view and marks the current page',()=>{
    boot('memes');
    const links=[...document.querySelectorAll('.vm-side nav a')] as HTMLAnchorElement[];
    expect(links).toHaveLength(6);
    expect(links.every(a=>new URL(a.href).pathname.startsWith('/api/v1/plugins/visual/views/workshop/_t/test-token/'))).toBe(true);
    expect(document.querySelector('[aria-current]')!.textContent).toContain('表情图库');
  });
  it('persists day/night and maps legacy components to the same theme',()=>{
    boot();(document.querySelector('[data-vm-theme="night"]') as HTMLButtonElement).click();
    expect(document.documentElement.dataset.uiTheme).toBe('night');
    expect(document.documentElement.dataset.theme).toBe('dark');
    expect(localStorage.getItem('visual_media_ui')).toBe('night');
    expect(document.querySelector('[data-vm-theme="night"]')!.getAttribute('aria-pressed')).toBe('true');
  });
  it('carries theme through late-rendered links when storage is blocked',()=>{
    vi.spyOn(Storage.prototype,'getItem').mockImplementation(()=>{throw new Error('sandbox');});
    vi.spyOn(Storage.prototype,'setItem').mockImplementation(()=>{throw new Error('sandbox');});
    boot();document.body.insertAdjacentHTML('beforeend','<a id="create" href="studio/index.html">创作</a>');
    (document.querySelector('[data-vm-theme="night"]') as HTMLButtonElement).click();
    document.dispatchEvent(new Event('DOMContentLoaded'));
    expect((document.getElementById('create') as HTMLAnchorElement).search).toBe('?ui=night');
    expect(document.getElementById('vm-theme-status')!.textContent).toContain('随页面导航保留');
  });
  it('honors the dedicated character entry and navigation theme',()=>{
    history.replaceState({},'', '?section=characters&ui=night');boot();
    expect(document.querySelector('[aria-current]')!.textContent).toContain('角色形象');
    expect(document.documentElement.dataset.uiTheme).toBe('night');
  });
});
