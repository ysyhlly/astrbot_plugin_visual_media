import {afterEach,beforeEach,describe,expect,it,vi} from 'vitest';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
const refs=readFileSync(resolve(process.cwd(),'../views/workshop/shared/references.js'),'utf8');
const tasks=readFileSync(resolve(process.cwd(),'../views/workshop/shared/tasks.js'),'utf8');
const flush=async()=>{for(let i=0;i<45;i++)await Promise.resolve();};
let apiGet:ReturnType<typeof vi.fn>,apiPost:ReturnType<typeof vi.fn>,controller:any;
let rows:any[];
beforeEach(()=>{
 document.body.innerHTML='<div id="root"></div>';
 Object.defineProperty(document,'currentScript',{configurable:true,value:{src:`${location.origin}/api/v1/plugins/v/views/workshop/_t/test/shared/tasks.js`}});
 (HTMLDialogElement.prototype as any).showModal=function(){this.open=true;};
 (HTMLDialogElement.prototype as any).close=function(){this.open=false;this.dispatchEvent(new Event('close'));};
 rows=[{job_id:'job-1',stage:'generation_failed',prompt:'<img src=x onerror=alert(1)>',target:'仅保存在工坊',images:[],can_retry:true,created_at:'2026-11-12T01:00:00Z',finished_at:'2026-11-12T01:00:30Z',check:{}}];
 apiGet=vi.fn(async(path:string,params:any)=>{
  if(path==='media/references')return {items:Array.from({length:10},(_,i)=>({hash:`hash-${i}`,description:`图${i}`})),total:30,page:params.page,size:24};
  if(path==='media/reference'||path==='media/task-preview')return {url:'data:image/png;base64,eA=='};
  if(path==='media/tasks')return {items:rows,total:rows.length,page:1,size:20};
  if(path.startsWith('webui/image_b64'))return {b64:'eA==',mime:'image/png'};
  throw Error(path);
 });
 apiPost=vi.fn(async()=>({status:'ok',data:{ok:true,job_id:'new-job',message:'已创建'}}));
 (window as any).AstrBotPluginPage={ready:async()=>{},apiGet,apiPost};
 new Function(refs)();new Function(tasks)();
});
afterEach(()=>{controller?.destroy();controller=null;vi.useRealTimers();});
describe('Visual reference picker',()=>{
 it('keeps off-page selections, enforces eight, and saves the visible order',async()=>{
  const changes:any[]=[];const picker=(window as any).VisualReferences.create(document.getElementById('root'),{onChange:(x:any)=>changes.push(x)});
  await picker.open(['off-page']);await flush();
  expect(picker.value()).toEqual(['off-page']);
  for(let i=0;i<8;i++)(document.querySelector(`[data-hash="hash-${i}"].vm-ref-select`) as HTMLButtonElement).click();
  expect(picker.value()).toHaveLength(8);expect(document.body.textContent).toContain('最多选择 8 张');
  (document.querySelector('[aria-label="右移第 1 张参考图"]') as HTMLButtonElement).click();
  expect(picker.value()[1]).toBe('off-page');expect(changes[changes.length-1]).toEqual(picker.value());
  (document.querySelector('[aria-label="移除第 2 张参考图"]') as HTMLButtonElement).click();
  expect(picker.value()).not.toContain('off-page');
 });
 it('retains chosen images during paging and opens preview without selecting',async()=>{
  const picker=(window as any).VisualReferences.create(document.getElementById('root'));
  await picker.open(['hash-2']);await flush();
  ([...document.querySelectorAll('button')].find(e=>e.textContent==='下一页') as HTMLButtonElement).click();await flush();
  expect(picker.value()).toEqual(['hash-2']);
  (document.querySelector('.vm-ref-tile>button:last-child') as HTMLButtonElement).click();await flush();
  expect(document.querySelector('dialog[open] img')!.getAttribute('src')).toContain('data:image/png');
  expect(picker.value()).toEqual(['hash-2']);
 });
 it('ignores a previous character request that finishes after another is opened',async()=>{
  let resolveFirst:any;let count=0;apiGet.mockImplementation(async(path:string)=>{
   if(path==='media/reference')return {url:'data:image/png;base64,eA=='};
   if(count++===0)return await new Promise(resolve=>resolveFirst=resolve);
   return {items:[{hash:'new',description:'新'}],total:1};
  });
  const picker=(window as any).VisualReferences.create(document.getElementById('root'));
  const first=picker.open(['old']);await flush();await picker.open(['new']);resolveFirst({items:[{hash:'old',description:'旧'}],total:1});await first;
  expect(picker.value()).toEqual(['new']);expect(document.querySelector('.vm-ref-select')!.getAttribute('data-hash')).toBe('new');
 });
});
describe('Unified task cards',()=>{
 async function mount(){controller=(window as any).VisualTaskCards.mount(document.getElementById('root'));await flush();}
 it('escapes provider content and requires review before a billable retry',async()=>{
  await mount();expect(document.querySelectorAll('img')).toHaveLength(0);
  ([...document.querySelectorAll('button')].find(e=>e.textContent==='重试生成') as HTMLButtonElement).click();
  expect(apiPost).not.toHaveBeenCalled();expect(document.querySelector('dialog')!.textContent).toContain('再调用一次生图服务');
  ([...document.querySelectorAll('dialog button')].find(e=>e.textContent==='确认重试生成') as HTMLButtonElement).click();await flush();
  expect(apiPost).toHaveBeenCalledWith('media/tasks/recover',expect.objectContaining({job_id:'job-1',action:'retry',request_id:expect.any(String)}));
 });
 it('reuses the same request identifier after a network error and never edits target',async()=>{
  rows=[{...rows[0],stage:'delivery_failed',images:['image.png'],target:'QQ · 私聊 · …1234',can_retry:false,can_resend:true}];await mount();
  ([...document.querySelectorAll('button')].find(e=>e.textContent==='重发已有图片') as HTMLButtonElement).click();
  apiPost.mockRejectedValue(new Error('network'));
  const button=[...document.querySelectorAll('dialog button')].find(e=>e.textContent==='确认发送到原会话') as HTMLButtonElement;
  button.click();await flush();button.click();await flush();
  expect(apiPost.mock.calls[0]![1]).toEqual(apiPost.mock.calls[1]![1]);expect(apiPost.mock.calls[0]![1]).not.toHaveProperty('target');
  expect(document.querySelector('dialog')!.textContent).toContain('不会调用生图模型');
 });
 it('keeps filters and check details through refresh; does not claim mismatch is a send failure',async()=>{
  rows=[{...rows[0],stage:'ready',can_retry:false,images:['image.png'],check:{status:'mismatch',issues:['发色不符'],reason:'颜色'}}];await mount();
  const filter=document.querySelector('select') as HTMLSelectElement;filter.value='attention';filter.dispatchEvent(new Event('change'));
  (document.querySelector('details') as HTMLDetailsElement).open=true;await controller.refresh();
  expect(filter.value).toBe('attention');expect((document.querySelector('details') as HTMLDetailsElement).open).toBe(true);
  expect(document.body.textContent).toContain('已保存到工坊');expect(document.body.textContent).not.toContain('重试生成');
 });
 it('shows an outage while preserving the previous tasks',async()=>{
  await mount();apiGet.mockRejectedValue(new Error('offline'));await controller.refresh();
  expect(document.querySelectorAll('.vm-task-card')).toHaveLength(1);expect(document.body.textContent).toContain('保留上次结果');
 });
});
