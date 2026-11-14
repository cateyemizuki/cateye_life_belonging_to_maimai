/* LBM_THEME_SWITCHER */
/* 主题切换器（2026-11-14 主题对比批 → 同日主题精简批）
   用途：两版保留皮肤（A 精致原风格 / C 数据仪表盘）可切换。右上角常驻紧凑
   pill，点击循环 A→C→A；写 document.documentElement.dataset.lbmTheme，
   localStorage（键 lbmTheme）持久化并在加载时恢复，默认 A。旧值迁移：存储为
   ''（原版）/'b'（手账）/非法值时一律落到 'a' 并写回，'a'/'c' 保留。明暗仍随
   宿主/离线 data-theme。原版与 B 手账已移除（见 CHANGELOG「主题精简批」）；
   切换按钮暂留，最终选定后移除切换器并默认化所选主题。

   装配：经 LBM_EXTENSIONS token 注入 panel.js IIFE 内部、排在 maimai.js/fish.js/
   layout_fix.js 之后，共享闭包（$/el 等），不重声明既有 const；整体独立 IIFE，
   additive，不改既有函数。皮肤行为脚本忠实提取自设计素材（A _variant_a.js、
   C work/variant-c.js），全部按当前激活皮肤门控：非激活皮肤的观察器回调直接
   返回；切换时先 lbmResetShared() 清除两版皮肤留下的内联痕迹（概括条原始色/
   meta 行内 display 与去重类/datebar 可见性/路径 title），再应用目标皮肤，
   避免跨皮肤颜色键失配与残留。不再有「原版=无痕迹」态，加载即应用 A。 */
(()=>{
'use strict';
const LBM_SKINS=['a','c'];
const LBM_SKIN_NAMES={'a':'A · 精致','c':'C · 仪表盘'};
const LBM_SKIN_KEY='lbmTheme';
let lbmSkin=(()=>{
 let v=null;
 try{v=localStorage.getItem(LBM_SKIN_KEY)}catch(e){}
 if(LBM_SKINS.includes(v))return v;
 /* 旧值迁移：''（原版）/'b'（手账）/非法/缺失 → 'a'，并写回清除旧值 */
 try{localStorage.setItem(LBM_SKIN_KEY,'a')}catch(e){}
 return 'a';
})();
const lbmActive=s=>lbmSkin===s;

/* ── 共享重置：切换前清除两版皮肤的内联痕迹，再应用目标 ─────────────────── */
function lbmResetShared(){
 /* 概括条色段 → 捕获的原始内联色（见下方 capture 观察器）。A 写 oklch、C 写
    var(--ov-*)，两版画家都只认原始 hex/rgb 键，不先还原则跨皮肤切换失配。 */
 try{
  const track=$('overview-track');
  if(track)for(const seg of track.querySelectorAll('.overview-seg')){
   if(seg.dataset.lbmBase!==undefined)seg.style.background=seg.dataset.lbmBase;
  }
 }catch(e){}
 /* C 的「数据来源」去重用内联 display:none 隐藏通用行；A 用 va-meta-dupe 类 */
 try{for(const row of document.querySelectorAll('.fish-meta-row')){row.style.display='';row.classList.remove('va-meta-dupe')}}catch(e){}
 /* C 的 S2 会在娱乐 tab 隐藏 datebar；按 panel.js activateTab 同口径还原 */
 try{
  const tab=document.querySelector('[role=tab][aria-selected="true"]');
  const key=tab?tab.dataset.tab:'now';
  const db=$('datebar');if(db)db.hidden=key==='now'||key==='system'||key==='core';
 }catch(e){}
 /* 皮肤补的 title 全文 */
 try{for(const id of['core-registry-path','notice']){const n=$(id);if(n)n.removeAttribute('title')}}catch(e){}
}

/* ── 皮肤 A 行为（源 variant-a/_variant_a.js，按激活门控） ─────────────────── */
const lbmA=(()=>{
 /* A2 概括条：写死 hex → 与状态卡同纪律的 oklch（随明暗） */
 const HEX={sleep:'#5b6ee1',rest:'#8a9df0',idle:'#9aa5b1',meal:'#e8a13c',work:'#3d8bfd',exercise:'#2fbf71',entertainment:'#e05fa0',daily_routine:'#4fb8c9',social:'#8f6fe8',free_time:'#67c98f',watch:'#c9a227',game:'#ef7d3a',leisure:'#58b89a',social_maintenance:'#b085e0',activity:'#4a90d9',transition:'#8a8f98'};
 const HUE={sleep:265,rest:258,idle:215,meal:72,work:230,exercise:155,entertainment:345,daily_routine:195,social:290,free_time:150,watch:88,game:52,leisure:168,social_maintenance:300,activity:220,transition:215};
 const GRAY={idle:1,transition:1};
 const RGB2GOAL={};
 Object.keys(HEX).forEach(g=>{const h=HEX[g];RGB2GOAL['rgb('+parseInt(h.slice(1,3),16)+', '+parseInt(h.slice(3,5),16)+', '+parseInt(h.slice(5,7),16)+')']=g});
 function recolor(){
  if(!lbmActive('a'))return;
  const track=$('overview-track');if(!track)return;
  const dark=document.documentElement.dataset.theme==='dark';
  const L=dark?74:56,C=dark?0.11:0.12;
  const segs=track.querySelectorAll('.overview-seg');
  for(let i=0;i<segs.length;i++){
   const goal=RGB2GOAL[segs[i].style.backgroundColor];if(!goal)continue;
   segs[i].style.background='oklch('+L+'% '+(GRAY[goal]?0.025:C)+' '+HUE[goal]+')';
  }
 }
 /* A4 用量 Top chip 千分位（幂等） */
 function groupTop(){
  if(!lbmActive('a'))return;
  const ut=$('usage-top');if(!ut)return;
  const kids=ut.children;
  for(let i=0;i<kids.length;i++){const t=kids[i].textContent;const n=t.replace(/\d{4,}/g,m=>Number(m).toLocaleString('en-US'));if(n!==t)kids[i].textContent=n}
 }
 /* A7 详情 meta 同标签去重（class 隐藏，CSS 已 scoped 到皮肤 A） */
 function dedupe(md){
  if(!md)return;const seen={};const rows=md.children;
  for(let i=0;i<rows.length;i++){
   const row=rows[i];row.classList.remove('va-meta-dupe');
   const cap=row.firstElementChild;const label=cap&&cap.firstChild?String(cap.firstChild.textContent).trim():'';
   if(!label)continue;
   if(seen[label])seen[label].classList.add('va-meta-dupe');
   seen[label]=row;
  }
 }
 function watchMeta(){
  if(!lbmActive('a'))return;
  const md=document.querySelector('.fish-metadata');if(!md)return;
  dedupe(md);
  if(!md.__vaWatch){md.__vaWatch=true;new MutationObserver(()=>{if(lbmActive('a'))dedupe(md)}).observe(md,{childList:true,characterData:true,subtree:true})}
 }
 /* S4 滚动渐隐提示（class 由 CSS scoped 到皮肤 A 消费） */
 let pl=null,scrollTimer=null;
 function updateScroll(){
  if(!lbmActive('a')||!pl)return;
  const scrollable=pl.scrollHeight>pl.clientHeight+2;
  pl.classList.toggle('is-scrollable',scrollable);
  pl.classList.toggle('at-end',pl.scrollTop+pl.clientHeight>=pl.scrollHeight-4);
 }
 function queueScroll(){if(!lbmActive('a')||scrollTimer)return;scrollTimer=setTimeout(()=>{scrollTimer=null;updateScroll()},40)}
 function init(){
  try{
   const track=$('overview-track');
   if(track)new MutationObserver(recolor).observe(track,{childList:true,subtree:true,attributes:true,attributeFilter:['style']});
   new MutationObserver(recolor).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
  }catch(e){}
  try{const ut=$('usage-top');if(ut)new MutationObserver(groupTop).observe(ut,{childList:true,characterData:true,subtree:true})}catch(e){}
  try{
   document.addEventListener('lbm:detail-open',e=>{
    if(!lbmActive('a'))return;
    setTimeout(watchMeta,0);
    /* S3 详情入口在折线下时滚入视口（usage 固定态除外） */
    if(e.detail&&e.detail.key==='usage')return;
    setTimeout(()=>{
     const btn=document.querySelector('.section-fish-button.fish-float')||document.querySelector('.section-fish-button[aria-expanded="true"]');
     if(!btn)return;const r=btn.getBoundingClientRect();
     if(r.bottom<8||r.top>innerHeight-8)btn.scrollIntoView({block:'center'});
    },0);
   });
  }catch(e){}
  try{
   pl=document.querySelector('.panel-layout');
   if(pl){
    pl.addEventListener('scroll',queueScroll,{passive:true});
    if(window.ResizeObserver)new ResizeObserver(queueScroll).observe(pl);
    new MutationObserver(queueScroll).observe(pl,{childList:true,subtree:true,attributes:true,attributeFilter:['hidden','class']});
    window.addEventListener('resize',queueScroll);
   }
  }catch(e){}
  try{
   const rp=$('core-registry-path');
   if(rp)new MutationObserver(()=>{if(lbmActive('a'))rp.title=rp.textContent||''}).observe(rp,{childList:true,characterData:true,subtree:true});
  }catch(e){}
 }
 function apply(){
  if(!lbmActive('a'))return;
  try{recolor()}catch(e){}
  try{groupTop()}catch(e){}
  try{watchMeta()}catch(e){}
  try{updateScroll()}catch(e){}
  try{const rp=$('core-registry-path');if(rp)rp.title=rp.textContent||''}catch(e){}
 }
 return {init,apply};
})();

/* ── 皮肤 C 行为（源 work/variant-c.js，按激活门控） ───────────────────────── */
const lbmC=(()=>{
 const OV={'#5b6ee1':'--ov-sleep','#8a9df0':'--ov-rest','#9aa5b1':'--ov-idle','#e8a13c':'--ov-meal','#3d8bfd':'--ov-work','#2fbf71':'--ov-exercise','#e05fa0':'--ov-entertainment','#4fb8c9':'--ov-daily','#8f6fe8':'--ov-social','#67c98f':'--ov-free','#c9a227':'--ov-watch','#ef7d3a':'--ov-game','#58b89a':'--ov-leisure','#b085e0':'--ov-socialm','#4a90d9':'--ov-activity','#8a8f98':'--ov-transition'};
 function rgbToHex(s){
  const m=/^rgb\((\d+),\s*(\d+),\s*(\d+)\)$/.exec(s||'');
  if(!m)return null;
  return '#'+[m[1],m[2],m[3]].map(v=>(+v).toString(16).padStart(2,'0')).join('');
 }
 function paintOverview(){
  if(!lbmActive('c'))return;
  const track=$('overview-track');if(!track)return;
  const segs=track.querySelectorAll('.overview-seg');
  for(let i=0;i<segs.length;i++){
   const n=segs[i],bg=n.style.backgroundColor;
   if(!bg||bg.indexOf('var(')>=0)continue; /* 已是令牌色（含 planned）则跳过 */
   const v=OV[rgbToHex(bg)];
   if(v)n.style.background='var('+v+')';
  }
 }
 function fmtUsageTop(){
  if(!lbmActive('c'))return;
  const box=$('usage-top');if(!box)return;
  const spans=box.querySelectorAll('span');
  for(let i=0;i<spans.length;i++){
   const t=spans[i].textContent;
   const u=t.replace(/\d{4,}/g,m=>Number(m).toLocaleString('en-US'));
   if(u!==t)spans[i].textContent=u;
  }
 }
 /* S2 娱乐 tab 的 datebar 无效 → 选中娱乐时隐藏 */
 function syncDatebar(){
  if(!lbmActive('c'))return;
  const mood=$('tab-mood'),db=$('datebar');
  if(mood&&db&&mood.getAttribute('aria-selected')==='true')db.hidden=true;
 }
 function dedupeMeta(){
  if(!lbmActive('c'))return;
  const overlay=document.querySelector('.fish-overlay');if(!overlay)return;
  const mds=overlay.querySelectorAll('.fish-metadata');
  for(let k=0;k<mds.length;k++){
   const rows=Array.prototype.slice.call(mds[k].children),src=[];
   for(let i=0;i<rows.length;i++){
    const c=rows[i].firstElementChild;
    const label=c&&c.firstChild?String(c.firstChild.textContent||'').trim():'';
    if(label==='数据来源')src.push(rows[i]);
   }
   if(src.length>1)for(let j=0;j<src.length;j++){
    const v=src[j].lastElementChild?src[j].lastElementChild.textContent.trim():'';
    if(v==='生活引擎实时记录'||v==='导入快照')src[j].style.display='none';
   }
  }
 }
 function syncPathTitle(){
  if(!lbmActive('c'))return;
  const p=$('core-registry-path');
  if(p&&p.title!==p.textContent)p.title=p.textContent;
 }
 function init(){
  try{
   const track=$('overview-track');
   if(track)new MutationObserver(paintOverview).observe(track,{childList:true,subtree:true,attributes:true,attributeFilter:['style']});
   const top=$('usage-top');
   if(top)new MutationObserver(fmtUsageTop).observe(top,{childList:true,subtree:true,characterData:true});
   const tabs=document.querySelectorAll('[role=tab]');
   if(tabs.length){const o=new MutationObserver(syncDatebar);for(let i=0;i<tabs.length;i++)o.observe(tabs[i],{attributes:true,attributeFilter:['aria-selected']})}
   /* 同步执行回调（微任务批处理）：只改 title/style.display，不在观察的
      childList/characterData 范围内，不会自激；不用 rAF（headless 虚拟时间兼容）。 */
   new MutationObserver(()=>{dedupeMeta();syncPathTitle()}).observe(document.body,{childList:true,subtree:true,characterData:true});
  }catch(e){}
 }
 function apply(){
  if(!lbmActive('c'))return;
  try{paintOverview();fmtUsageTop();syncDatebar();dedupeMeta();syncPathTitle()}catch(e){}
 }
 return {init,apply};
})();

/* ── 概括条原始色捕获：必须先于各皮肤画家观察器注册（同一 childList 批次内
      按注册序执行），保证新色段先记录 panel.js 写入的原始内联色再被重映射。 ── */
try{
 const track=$('overview-track');
 if(track){
  const capture=()=>{for(const seg of track.querySelectorAll('.overview-seg'))if(seg.dataset.lbmBase===undefined)seg.dataset.lbmBase=seg.style.background};
  capture();
  new MutationObserver(capture).observe(track,{childList:true});
 }
}catch(e){}

/* ── 常驻切换 pill（fixed，z-index 970 > 详情 900/usage 960） ───────────────── */
const lbmPill=el('button','lbm-theme-pill');
lbmPill.id='lbm-theme-switcher';lbmPill.type='button';
const lbmPillIcon=el('span','lbm-pill-icon','⟳');lbmPillIcon.setAttribute('aria-hidden','true');
const lbmPillName=el('span','lbm-pill-name','');
lbmPill.append(lbmPillIcon,lbmPillName);
document.body.append(lbmPill);
document.documentElement.setAttribute('data-lbm-pill','');

function lbmSyncButton(){
 lbmPillName.textContent=LBM_SKIN_NAMES[lbmSkin];
 const next=LBM_SKINS[(LBM_SKINS.indexOf(lbmSkin)+1)%LBM_SKINS.length];
 lbmPill.dataset.skin=lbmSkin;
 lbmPill.title='主题切换：点击循环 A 精致 → C 仪表盘。当前：'+LBM_SKIN_NAMES[lbmSkin]+'，下一个：'+LBM_SKIN_NAMES[next]+'（选择本地记忆，默认 A）';
 lbmPill.setAttribute('aria-label','当前主题：'+LBM_SKIN_NAMES[lbmSkin]+'；点击切换到'+LBM_SKIN_NAMES[next]);
}

/* 定位：默认右上角；详情打开时若与面板/关闭按钮相交 → 移到面板右侧或左侧
   空档；窄视口无侧边空档 → 右下角兜底（面板关闭按钮恒在顶部，永不遮挡）。
   同时给 mast 让出右侧空档（--lbm-pill-lane），避免盖住品牌区/工具条。 */
function lbmPosition(){
 const vw=innerWidth,vh=innerHeight,w=lbmPill.offsetWidth,h=lbmPill.offsetHeight;
 if(!w||!h)return;
 let left=vw-w-12,top=10;
 const ov=$('section-fish-popover');
 if(ov&&!ov.hidden){
  const panel=ov.querySelector('.fish-panel'),close=ov.querySelector('.fish-close');
  if(panel){
   const pr=panel.getBoundingClientRect();
   const cr=close?close.getBoundingClientRect():pr;
   const hitClose=(l,t)=>l<cr.right+8&&l+w>cr.left-8&&t<cr.bottom+8&&t+h>cr.top-8;
   const hitPanel=(l,t)=>l<pr.right+4&&l+w>pr.left-4&&t<pr.bottom+4&&t+h>pr.top-4;
   if(hitClose(left,top)||hitPanel(left,top)){
    if(vw-pr.right>=w+20){left=pr.right+8;top=Math.max(10,Math.min(pr.top,vh-h-10))}
    else if(pr.left>=w+20){left=pr.left-w-8;top=Math.max(10,Math.min(pr.top,vh-h-10))}
    else{left=vw-w-12;top=vh-h-12}
   }
  }
 }
 lbmPill.style.left=Math.round(left)+'px';
 lbmPill.style.top=Math.round(top)+'px';
 try{
  /* 空档让在 mast 子项（toolbar/mast-side）的 margin 上，mast 自身宽度不变
     （panel.css .mast{width:100%} 锁宽），判定与空档无反馈、不会振荡。 */
  const mast=document.querySelector('.mast');
  let lane=0;
  if(mast){const mr=mast.getBoundingClientRect();if(left<mr.right-4)lane=w+20}
  document.documentElement.style.setProperty('--lbm-pill-lane',lane+'px');
 }catch(e){}
}
let lbmPosQueued=false;
function lbmQueuePos(){
 if(lbmPosQueued)return;
 lbmPosQueued=true;
 setTimeout(()=>{lbmPosQueued=false;lbmPosition()},0);
 if(window.requestAnimationFrame)requestAnimationFrame(()=>lbmPosition());
}

/* ── 切换：写 root 属性 + 持久化 + 重置 + 应用 + 重校准 ─────────────────────── */
function lbmSetSkin(next,persist=true){
 lbmSkin=next;
 document.documentElement.dataset.lbmTheme=next;
 if(persist){try{localStorage.setItem(LBM_SKIN_KEY,next)}catch(e){}}
 lbmResetShared();
 if(next==='a')lbmA.apply();else if(next==='c')lbmC.apply();
 lbmSyncButton();
 lbmPosition();
 /* 皮肤切换改变 mast/详情面板尺寸：让 fish.js（resize 重校准）与布局层补算 */
 try{window.dispatchEvent(new Event('resize'))}catch(e){}
}
lbmPill.onclick=()=>lbmSetSkin(LBM_SKINS[(LBM_SKINS.indexOf(lbmSkin)+1)%LBM_SKINS.length]);

/* ── 装配：观察器一次性安装（门控），恢复持久化选择（迁移后必为 a/c），
      接线定位触发 ─────────────────────────────────────────────────────── */
lbmA.init();lbmC.init();
lbmSetSkin(lbmSkin,false);
window.addEventListener('resize',lbmQueuePos);
window.addEventListener('scroll',lbmQueuePos,{passive:true,capture:true});
/* 详情打开瞬间面板还在 scale 入场动画里，getBoundingClientRect 是中间态——
   动画结束后必须按最终矩形重判避让，否则 pill 可能压住 .fish-close。 */
document.addEventListener('lbm:detail-open',()=>{lbmQueuePos();setTimeout(lbmPosition,320)});
window.addEventListener('maibot:context',lbmQueuePos);
try{
 const ov=$('section-fish-popover');
 if(ov){
  new MutationObserver(lbmQueuePos).observe(ov,{attributes:true,attributeFilter:['hidden','class','data-section']});
  const panel=ov.querySelector('.fish-panel');
  if(panel)panel.addEventListener('transitionend',lbmQueuePos);
 }
 if(window.ResizeObserver){const mast=document.querySelector('.mast');if(mast)new ResizeObserver(lbmQueuePos).observe(mast)}
}catch(e){}
})();