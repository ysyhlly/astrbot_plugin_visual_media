import {msg,tr,setText,getSharedScope,setSharedScope} from '../series-ui.js';
const $=id=>document.getElementById(id);
const api=window.AstrBotPluginPage;
let data={records:[],preferences:[]};
let currentScope=getSharedScope();
const names={image:'其他图片',text:'文字',meme:'表情',generate:'生图',preference:'偏好',character_check:'角色核对',selected:'已选择',filtered:'候选过滤',blocked:'已拦截',delivered:'已交付',submitted:'已提交',generated:'已生成',failed:'失败',saved:'已保存',pass:'核对通过',mismatch:'存在差异',unknown:'未能确认',skipped:'已关闭'};
const reasons={plain_reply:'普通文字回复',model_selected_text:'模型选择文字',model_selected_candidate:'模型选择候选表情',native_generation_tool:'调用生图工具',confirmed_send:'表情发送已确认',confirmed_image_send:'图片发送已确认',candidate_filter:'根据成员偏好及近期发送记录筛选',member_blocked_image:'成员不想再看这张图片',member_prefers_text:'成员偏好文字',member_prefers_fewer_memes:'成员希望减少表情',recent_exact_repeat:'近期已经发送相同图片',concurrent_repeat:'同一图片正在发送',explicit_user_preference:'记住明确表达的偏好',character_task_queued:'角色生图任务已提交',character_generation:'角色图片生成结果',reference_comparison:'与标准参考图比对',image_tool_receipt:'通用图片工具发送回执',platform_send_failed:'平台发送失败'};
const frequencyLabels={auto:'自然选择',less:'少用表情',off:'不主动发',more:'合适时多用'};
function node(tag,text,cls){const n=document.createElement(tag);setText(n,text||'');if(cls)n.className=cls;return n;}
function unwrap(r){if(r?.status==='error'||r?.success===false)throw new Error(r.message||tr('请求失败'));return r?.status==='ok'?r.data:r;}
function render(){
 const p=data.policy||{};
 setText($('policy'),msg('偏好记忆：{0} · 相同图去重：{1} · 角色核对：{2} · 记录：{3} · 日志丢弃：{4}',
  msg(p.preferences_enabled===false?'关闭':'开启'),
  p.repeat_guard_enabled===false?msg('关闭'):msg('{0} 分钟 / {1} 次',p.exact_repeat_minutes??30,p.recent_receipts??12),
  p.character_check_enabled===false?msg('关闭'):msg('开启（最多 {0} 张 / {1} 秒）',p.character_check_max_images??2,p.character_check_timeout??60),
  p.decision_records_enabled===false?msg('停止新增'):msg('{0} 天 / {1} 条',p.record_retention_days??30,p.max_records??5000),
  p.dropped_audit_records??0));

 const rows=data.records.filter(r=>(!$('kind').value||r.mode===$('kind').value)&&(!$('scope').value||r.scope===$('scope').value));
 setText($('count'),msg('{0} 条记录',rows.length));$('records').replaceChildren();
 for(const row of rows){
  const card=node('article','','record'),head=node('div','','record-head');
  head.append(node('strong',Object.hasOwn(names,row.mode)?msg(names[row.mode]):row.mode),node('span',Object.hasOwn(names,row.phase)?msg(names[row.phase]):row.phase,`badge ${['unknown','filtered','blocked'].includes(row.phase)?'warn':['failed','mismatch'].includes(row.phase)?'fail':''}`),node('time',new Date(row.created*1000).toLocaleString(document.documentElement.lang)));
  card.append(head,node('p',Object.hasOwn(reasons,row.reason)?msg(reasons[row.reason]):row.reason),node('p',msg('会话 {0} · 成员 {1} · 回合 {2}',row.scope.slice(0,10),row.member.slice(0,8)||'—',row.trace.slice(0,8)),'meta'));
  if(row.mode==='character_check'){card.append(node('p',row.details.reason||''));for(const issue of row.details.issues||[])card.append(node('p',`• ${issue}`));}
  const details=node('details'),summary=node('summary',msg('查看记录详情'));details.append(summary,node('pre',JSON.stringify(row.details,null,2)));card.append(details);$('records').append(card);
 }
 if(!rows.length)$('records').append(node('p',msg('当前筛选下没有记录。自然聊天后刷新即可查看。'),'empty'));
 $('preferences').replaceChildren();
 const preferences=data.preferences.filter(pref=>!currentScope||pref.scope===currentScope);
 for(const pref of preferences){
  const card=node('article','','preference-card');
  card.append(node('h3',msg('会话 {0} · 成员 {1}',pref.scope.slice(0,10),pref.member.slice(0,8))),
   node('p',msg('表情频率：{0}',msg(frequencyLabels[pref.data.frequency]||frequencyLabels.auto))),
   node('p',msg('画风：{0}',pref.data.style||msg('跟随本轮要求'))),
   node('p',msg('不再推荐：{0} 张',(pref.data.blocked||[]).length)));
  const reset=node('button',msg('恢复默认'));reset.type='button';reset.onclick=async()=>{reset.disabled=true;try{unwrap(await api.apiPost('media/behavior/reset',{scope:pref.scope,member:pref.member}));await refresh();}catch(e){$('notice').textContent=e.message;}finally{reset.disabled=false;}};card.append(reset);$('preferences').append(card);
 }
 if(!preferences.length)$('preferences').append(node('p',msg('暂未保存明确的表达偏好。'),'empty'));
}
function renderScopes(){
 const select=$('scope');
 const available=new Set([...data.records,...data.preferences].map(row=>row.scope));
 if(currentScope)available.add(currentScope);
 select.replaceChildren(new Option(tr('全部会话'),''));
 for(const scope of available)if(scope)select.add(new Option(tr('会话 {0}',scope.slice(0,10)),scope));
 select.value=currentScope;
}
async function refresh(){
 const button=$('refresh');button.disabled=true;
 try{
  if(!api)throw new Error(tr('请从 AstrBot 控制台打开'));
  await api.ready();
  data=unwrap(await api.apiGet('media/behavior'));
  renderScopes();render();setText($('notice'),'');
 }catch(error){setText($('notice'),msg('读取失败：{0}',error.message));}
 finally{button.disabled=false;}
}
$('refresh').onclick=refresh;
$('kind').onchange=render;
$('scope').onchange=()=>{
 currentScope=$('scope').value;
 setSharedScope(currentScope,{syncUrl:true,emit:false});
 render();
};
window.addEventListener('dynamics-scopechange',event=>{
 currentScope=String(event.detail?.umo||'').trim();
 renderScopes();render();
});
window.addEventListener('series-languagechange',()=>{renderScopes();render();});
void refresh();
