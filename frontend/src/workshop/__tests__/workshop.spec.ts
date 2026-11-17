import {beforeEach, describe, expect, it, vi} from 'vitest';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';

const html = readFileSync(resolve(process.cwd(), '../views/workshop/index.html'), 'utf8');
const source = readFileSync(resolve(process.cwd(), '../views/workshop/workshop.js'), 'utf8');
const referenceSource = readFileSync(resolve(process.cwd(), '../views/workshop/shared/references.js'), 'utf8');
const flush = async () => { for(let i=0; i<15; i++) await Promise.resolve(); };
let apiGet: ReturnType<typeof vi.fn>, apiPost: ReturnType<typeof vi.fn>;
async function boot() { new Function(referenceSource)(); await new Function(`return (async () => {${source}\n})();`)(); await flush(); }

beforeEach(() => {
  document.documentElement.innerHTML = html.replace(/<!doctype html>/i, '').replace(/<\/?html[^>]*>/gi, '');
  (HTMLDialogElement.prototype as any).showModal = function () { this.open = true; };
  (HTMLDialogElement.prototype as any).close = function () { this.open = false; };
  vi.stubGlobal('IntersectionObserver', class { callback: any; constructor(cb: any) {this.callback=cb;} observe(target: any) {this.callback([{target,isIntersecting:true}]);} unobserve() {} disconnect() {} });
  apiGet = vi.fn(async (path: string) => {
    if(path === 'media/overview') return {status:'ok',data:{memes:3,generation:{ready:true},profiles:[],history:{items:[{job_id:'test-job',prompt:'开心',images:['result.png']}]}}};
    if(path.startsWith('webui/image_b64/')) return {status:'ok',data:{mime:'image/png',b64:'eA=='}};
    if(path === 'images') return {images:[{hash:'public',scope_mode:'public',desc:'公开'},{hash:'private',scope_mode:'local',desc:'私有'}]};
    if(path === 'media/references') return {items:[{hash:'public',description:'公开'}],total:1,page:1,size:24};
    if(path === 'media/reference') return {url:'data:image/png;base64,eA=='};
    throw new Error(path);
  });
  apiPost = vi.fn(async () => ({status:'ok',data:{hash:'saved'}}));
  (window as any).AstrBotPluginPage = {ready: async()=>{},apiGet,apiPost};
});

describe('Integrated workshop',()=>{
  it('loads overview and displays the actual API image format',async()=>{
    await boot();
    expect(document.getElementById('meme-count')!.textContent).toBe('3');
    expect(document.querySelector('.image-card img')!.getAttribute('src')).toBe('data:image/png;base64,eA==');
    expect(document.getElementById('page-title')!.textContent).toBe('工坊总览');
  });
  it('saves an image privately by default with its zero-based index',async()=>{
    await boot();
    (document.querySelector('.image-card button') as HTMLButtonElement).click();
    const form = document.getElementById('save-form') as HTMLFormElement;
    form.dispatchEvent(new Event('submit',{cancelable:true})); await flush();
    expect(apiPost).toHaveBeenCalledWith('media/save',expect.objectContaining({generation_id:'test-job',image_index:0,public:false}));
  });
  it('offers only public assets as shared character references',async()=>{
    await boot();
    document.getElementById('new-profile')!.click(); await flush();
    const select = document.querySelector('#profile-form select') as HTMLSelectElement;
    const option=document.querySelector('.vm-ref-select') as HTMLButtonElement;
    expect(option.dataset.hash).toBe('public');option.click();
    expect([...select.options].map(o=>o.value)).toEqual(['public']);
  });
  it('keeps the form open and reports failed saves',async()=>{
    apiPost.mockRejectedValue(new Error('disk unavailable'));
    await boot(); (document.querySelector('.image-card button') as HTMLButtonElement).click();
    document.getElementById('save-form')!.dispatchEvent(new Event('submit',{cancelable:true})); await flush();
    expect((document.getElementById('save-dialog') as HTMLDialogElement).open).toBe(true);
    expect(document.getElementById('notice')!.textContent).toBe('disk unavailable');
    expect(document.querySelector('#save-dialog [role="alert"]')!.textContent).toBe('disk unavailable');
    expect((document.querySelector('#save-form [type="submit"]') as HTMLButtonElement).disabled).toBe(false);
  });
  it('keeps the overview compact and searches all characters safely',async()=>{
    const profiles=Array.from({length:14},(_,i)=>({key:`role_${i}`,name:`角色${i}`,appearance:'原作外观',references:[]}));
    apiGet.mockImplementation(async(path:string)=>path==='media/overview'?{memes:3,generation:{ready:true},profiles,history:{items:[]}}:{});
    await boot();
    expect(document.querySelectorAll('.profile')).toHaveLength(3);
    const search=document.getElementById('profile-search') as HTMLInputElement;
    search.value='role_13';search.dispatchEvent(new Event('input'));
    expect(document.querySelectorAll('.profile')).toHaveLength(1);
    expect(document.querySelector('.profile h3')!.textContent).toBe('角色13');
    search.value='<script>';search.dispatchEvent(new Event('input'));
    expect(document.querySelectorAll('.profile')).toHaveLength(0);
    expect(document.querySelector('#profiles .empty')!.textContent).toContain('没有匹配');
  });
});
