(() => {
 const UI=window.SeriesUI||{t:(s,p={})=>String(s).replace(/\{(\w+)\}/g,(m,k)=>Object.hasOwn(p,k)?String(p[k]??''):m),msg(s,p={}){return this.t(s,p);},text(n,v){n.textContent=String(v??'');return n;},bind(n,s,p={},a=''){if(a)n.setAttribute(a,this.t(s,p));else n.textContent=this.t(s,p);return n;},onLocaleChange:()=>()=>{}};

 const cache=new Map(),queue=[];let running=0;
 const node=(tag,text,cls)=>{const n=document.createElement(tag);if(text)UI.text(n,text);if(cls)n.className=cls;return n;};
 function unwrap(r){if(r?.status==='error'||r?.success===false)throw new Error(r.message||r.error||UI.t("读取失败"));return r?.status==='ok'?r.data:r;}
 function drain(){while(running<4&&queue.length){const job=queue.shift();running++;job().finally(()=>{running--;drain();});}}
 function load(hash,size=240){const key=`${hash}:${size}`;if(cache.has(key))return cache.get(key);
  const promise=new Promise((resolve,reject)=>queue.push(async()=>{try{const r=unwrap(await (window.DynamicsPanelBridge || window.AstrBotPluginPage).apiGet('media/reference',{hash,size}));resolve(r.url);}catch(e){cache.delete(key);reject(e);}}));cache.set(key,promise);while(cache.size>128)cache.delete(cache.keys().next().value);drain();return promise;
 }
 function preview(hash,label=UI.t("参考图")){
  const dialog=node('dialog','','vm-image-dialog'),header=node('div','','dialog-head'),close=node('button',UI.msg("关闭"));close.type='button';close.onclick=()=>dialog.close();header.append(node('h2',label),close);
  const image=node('img');image.alt=label;const status=node('p',UI.msg("正在读取预览…"));dialog.append(header,image,status);document.body.append(dialog);dialog.addEventListener('close',()=>dialog.remove(),{once:true});dialog.showModal();load(hash,960).then(url=>{if(dialog.isConnected){image.src=url;status.remove();}}).catch(e=>status.textContent=e.message);
 }
 function create(container,{onChange=()=>{}}={}){
  let selected=[],items=[],page=1,total=0,epoch=0,dragged='',timer;
  const search=node('input');search.type='search';search.placeholder=UI.t("搜索公开参考图、角色或描述");UI.bind(search,'搜索参考图',{},'aria-label');UI.bind(search,'搜索公开参考图、角色或描述',{},'placeholder');
  const chosen=node('div','','vm-ref-selected'),summary=node('p','','hint'),grid=node('div','','vm-ref-grid'),status=node('p','','hint');status.setAttribute('role','status');
  const prev=node('button',UI.msg("上一页")),next=node('button',UI.msg("下一页")),pageText=node('span'),pager=node('div','','vm-ref-pager');prev.type=next.type='button';pager.append(prev,pageText,next);
  container.replaceChildren(summary,chosen,search,status,grid,pager);
  function thumb(hash,label){const img=node('img');img.alt=label;img.loading='lazy';load(hash).then(url=>{if(img.isConnected)img.src=url;}).catch(()=>{img.alt=UI.t("参考图缺失或不再公开");});return img;}
  function move(hash,offset){const at=selected.indexOf(hash),to=at+offset;if(to<0||to>=selected.length)return;[selected[at],selected[to]]=[selected[to],selected[at]];changed();}
  function changed(){onChange([...selected]);render();}
  function render(){
   summary.textContent=UI.t("已选 {0} / 8 张 · 第一张作为角色封面，可拖动或用左右按钮排序",{0:selected.length});
   chosen.replaceChildren();
   for(const [i,hash] of selected.entries()){
    const card=node('div','','vm-ref-choice');card.draggable=true;card.dataset.hash=hash;
    const zoom=node('button','','vm-ref-zoom');zoom.type='button';zoom.setAttribute('aria-label',UI.t("放大第 {0} 张参考图",{0:i+1}));zoom.append(thumb(hash,UI.t("参考图 {0}",{0:i+1})));zoom.onclick=()=>preview(hash);
    const controls=node('div','','vm-ref-controls');for(const [label,fn,disabled] of [[UI.t("左移"),()=>move(hash,-1),i===0],[UI.t("右移"),()=>move(hash,1),i===selected.length-1],[UI.t("移除"),()=>{selected=selected.filter(x=>x!==hash);changed();},false]]){const b=node('button',label);b.type='button';b.disabled=disabled;b.setAttribute('aria-label',UI.t("{0}第 {1} 张参考图",{0:label,1:i+1}));b.onclick=fn;controls.append(b);}
    card.append(zoom,node('span',i===0?UI.t("封面"):String(i+1),'vm-ref-index'),controls);
    card.ondragstart=e=>{dragged=hash;e.dataTransfer?.setData('text/plain',hash);};card.ondragover=e=>e.preventDefault();card.ondrop=e=>{e.preventDefault();if(dragged&&selected.includes(dragged)&&dragged!==hash){const from=selected.indexOf(dragged),to=selected.indexOf(hash);selected.splice(from,1);selected.splice(to,0,dragged);changed();}dragged='';};chosen.append(card);
   }
   if(!selected.length)chosen.append(node('p',UI.msg("尚未选择参考图，可从下方图库勾选。"),'empty'));
   grid.replaceChildren();for(const item of items){const card=node('div','','vm-ref-tile'),select=node('button','','vm-ref-select'),zoom=node('button',UI.msg("放大"));select.type=zoom.type='button';select.setAttribute('aria-pressed',String(selected.includes(item.hash)));select.setAttribute('aria-label',UI.t("选择参考图：{0}",{0:item.description}));select.dataset.hash=item.hash;
    select.append(thumb(item.hash,item.description),node('span',item.description));select.onclick=()=>{if(selected.includes(item.hash))selected=selected.filter(x=>x!==item.hash);else if(selected.length<8)selected.push(item.hash);else{status.textContent=UI.t("最多选择 8 张，请先移除一张。");return;}changed();};zoom.onclick=()=>preview(item.hash,item.description);card.append(select,zoom);grid.append(card);}
   if(!items.length)grid.append(node('p',UI.msg("没有匹配的公开图片。"),'empty'));
   prev.disabled=page<=1;next.disabled=page*24>=total;pageText.textContent=UI.t("第 {0} 页 · {1} 张",{0:page,1:total});
  }
  async function fetchPage(){const version=++epoch;status.textContent=UI.t("正在读取公开图库…");try{const data=unwrap(await (window.DynamicsPanelBridge || window.AstrBotPluginPage).apiGet('media/references',{page,q:search.value}));if(version!==epoch)return;items=data.items;total=data.total;status.textContent=UI.t("只展示公开图片，当前选择会在翻页和搜索时保留。");render();}catch(e){if(version===epoch)status.textContent=e.message;}}
  search.oninput=()=>{clearTimeout(timer);timer=setTimeout(()=>{page=1;fetchPage();},250);};prev.onclick=()=>{page--;fetchPage();};next.onclick=()=>{page++;fetchPage();};
  UI.onLocaleChange(render);return {open(refs=[]){clearTimeout(timer);epoch++;selected=[...refs];items=[];page=1;total=0;search.value='';changed();return fetchPage();},value:()=>[...selected]};
 }
 window.VisualReferences={create,load,preview};
})();
