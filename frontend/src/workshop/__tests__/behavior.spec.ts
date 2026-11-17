import {beforeEach,describe,expect,it,vi} from 'vitest';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
const html=readFileSync(resolve(process.cwd(),'../views/workshop/behavior/index.html'),'utf8');
const source=readFileSync(resolve(process.cwd(),'../views/workshop/behavior/behavior.js'),'utf8');
const flush=async()=>{for(let i=0;i<20;i++)await Promise.resolve();};
let apiPost:ReturnType<typeof vi.fn>;
beforeEach(()=>{
 document.documentElement.innerHTML=html.replace(/<!doctype html>/i,'').replace(/<\/?html[^>]*>/gi,'');
 apiPost=vi.fn(async()=>({status:'ok',data:{reset:true}}));
 (window as any).AstrBotPluginPage={ready:async()=>{},apiPost,apiGet:async()=>({status:'ok',data:{preferences:[{scope:'a'.repeat(24),member:'b'.repeat(24),data:{frequency:'less',style:'水彩',blocked:[]}}],records:[{scope:'a'.repeat(24),member:'b'.repeat(24),trace:'c'.repeat(24),mode:'generate',phase:'submitted',reason:'character_task_queued',created:1,details:{task_id:'job'}},{scope:'a'.repeat(24),member:'b'.repeat(24),trace:'c'.repeat(24),mode:'character_check',phase:'mismatch',reason:'reference_comparison',created:2,details:{issues:['<img src=x onerror=alert(1)>'],reason:'发色不符'}}]}})};
});
async function boot(){await new Function(`return (async()=>{${source}\n})();`)();await flush();}
describe('Media behavior console',()=>{
 it('shows effective disabled settings instead of fixed defaults',async()=>{(window as any).AstrBotPluginPage.apiGet=async()=>({status:'ok',data:{preferences:[],records:[],policy:{preferences_enabled:false,repeat_guard_enabled:false,character_check_enabled:false,decision_records_enabled:false,dropped_audit_records:2}}});await boot();const policy=document.getElementById('policy')!.textContent;expect(policy).toContain('偏好记忆：关闭');expect(policy).toContain('角色核对：关闭');expect(policy).toContain('停止新增');expect(policy).toContain('日志丢弃：2');});
 it('separates submission from delivery and safely renders model output',async()=>{await boot();expect(document.querySelectorAll('.record')).toHaveLength(2);expect(document.body.textContent).toContain('已提交');expect(document.querySelector('.record .badge')!.textContent).not.toBe('已交付');expect(document.querySelectorAll('img')).toHaveLength(0);});
 it('filters checks and resets exactly the selected member preference',async()=>{await boot();const select=document.getElementById('kind') as HTMLSelectElement;select.value='character_check';select.dispatchEvent(new Event('change'));expect(document.querySelectorAll('.record')).toHaveLength(1);(document.querySelector('.preference-card button') as HTMLButtonElement).click();await flush();expect(apiPost).toHaveBeenCalledWith('media/behavior/reset',{scope:'a'.repeat(24),member:'b'.repeat(24)});});
 it('shows reset failures without claiming a successful reset',async()=>{await boot();apiPost.mockRejectedValue(new Error('save failed'));(document.querySelector('.preference-card button') as HTMLButtonElement).click();await flush();expect(document.getElementById('notice')!.textContent).toBe('save failed');expect(document.querySelectorAll('.preference-card')).toHaveLength(1);});
});
