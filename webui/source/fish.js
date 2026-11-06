/* Fullscreen details layer (2026-11-01 U plan §4): each entry expands a sticky
   bubble anchored at that entry — hover previews (no focus steal), click pins,
   click again unpins, another entry switches. The entry never moves: the same
   element is lifted above the backdrop in place (a placeholder keeps its
   coordinates), and the panel opens/closes from the entry's origin with a
   curved tail tying the bubble to the entry.
   Legacy render targets are moved, never copied. Internal names kept on purpose. */
function fullClock(value){
 if(!value||!/^\d{4}-\d{2}-\d{2}/.test(String(value))||!date(value))return '暂无完整时间';
 try{return new Intl.DateTimeFormat('zh-CN',{timeZone:data?.timezone||'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23',timeZoneName:'short'}).format(date(value))+' · '+(data?.timezone||'Asia/Shanghai')}
 catch{return '时间格式暂不可用'}
}
const SectionFish=(()=>{
 const store=el('div');store.id='fish-detail-store';store.hidden=true;document.body.append(store);
 const overlay=el('div','fish-overlay section-fish-popover');overlay.id='section-fish-popover';overlay.hidden=true;overlay.setAttribute('role','dialog');overlay.setAttribute('aria-modal','true');overlay.setAttribute('aria-labelledby','fish-popup-title');
 const panel=el('div','fish-panel');
 const heading=el('div','fish-popup-heading'),title=el('h2','','详情'),closeButton=el('button','fish-close','关闭');title.id='fish-popup-title';closeButton.type='button';closeButton.setAttribute('aria-label','关闭详情');closeButton.onclick=()=>close(true);heading.append(title,closeButton);
 const body=el('div','fish-popup-body'),summary=el('p','fish-popup-summary'),metadata=el('div','fish-metadata'),details=el('div','fish-records');body.append(summary,metadata,details);panel.append(heading,body);overlay.append(panel);document.body.append(overlay);
 let current=null,lastTrigger=null,closeTimer=null,hoverTimer=null,pinnedOpen=false,floatSyncQueued=false;
 const entries=new Map();
 // 粘连气泡（0425 二次目检）：面板紧贴入口放置（空间更多的一侧），面板边缘在
 // 入口对应位置开一个三角缺口——气泡“长”在入口上，不再用细线远程连接。
 const tip=el('span','fish-tail-tip');tip.setAttribute('aria-hidden','true');tip.hidden=true;overlay.append(tip);
 let changesCache={src:null,key:'',items:[]};
 const reduceMotion=()=>matchMedia('(prefers-reduced-motion: reduce)').matches;
 // 锁定滚动时滚动条消失会让居中内容整体横移（“抽一下”）——用等宽右内边距补偿。
 const lockScroll=()=>{
  const w=innerWidth-document.documentElement.clientWidth;
  document.body.style.paddingRight=w>0?w+'px':'';
  document.body.classList.add('fish-locked');
 };
 const unlockScroll=()=>{
  document.body.classList.remove('fish-locked');
  document.body.style.paddingRight='';
 };
 const finishMs=()=>{const raw=parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--maimai-detail-fade-ms'));return Number.isFinite(raw)&&raw>0?raw:220};
 // 「最近变化」：前端从 history.points / schedule.episodes / sleep.sessions 派生，不改 API。
 const CHANGES_KIND={current:'episode',companion:'none',state:'state',history:'state',sleep:'sleep',mood:'vad',sharing:'none',health:null};
 const CHANGE_REASONS={start:'启动观察',tick:'生活采样',sleep:'睡眠采样',episode:'活动边界',wake:'醒来结算',sleep_settlement:'睡眠结算',estimated_gap_fill:'估算'};
 const CHANGE_LABELS={...labels,mental_state:'精神状态'};
 const WAKE_REASONS={energy_threshold:'精力达标',lie_in_cap:'赖床上限',wake_window_end:'作息窗口结束',hard_commitment:'硬约定唤醒',interrupted_awake:'被暂时打断',manual:'手动唤醒',no_sleep_mode:'免睡模式',offline_reconnect:'离线补眠估算'};
 const changeEps=()=>{const m=data&&typeof data==='object'?data.maimai||{}:{};
  const pick=(key,fall)=>typeof m[key]==='number'&&Number.isFinite(m[key])&&m[key]>0?m[key]:fall;
  return {state:pick('history_state_epsilon',.01),debt:pick('history_debt_epsilon',.1),
          limit:Math.min(30,Math.max(5,Math.round(pick('history_changes_limit',30))))}};
 const dedupe=list=>{const seen=new Set();return list.filter(item=>{const key=item.at+'|'+item.text;if(seen.has(key))return false;seen.add(key);return true}).sort((x,y)=>String(y.at).localeCompare(String(x.at)))};
 function buildStateChanges(eps,withPoints,withEpisodes){
  const out=[],tracked=['energy','hunger','focus','stress','social_battery','mental_state'];
  if(withPoints){
   const series=lifeSeries(),rows=Array.isArray(series.points)&&series.points.length?series.points:(data?.history?.points||[]);
   for(let i=1;i<rows.length;i++){
    const a=rows[i-1],b=rows[i];
    if(!a||!b||a.estimated||b.estimated)continue;
    // 估算点不进入变化列表；采样间差异只陈述观察，不冒充因果。
    const why=CHANGE_REASONS[b.reason]||b.reason||'状态观察';
    for(const key of tracked){
     const before=num(a[key]),after=num(b[key]);
     if(before===null||after===null||Math.abs(after-before)<eps.state)continue;
     out.push({at:b.at,text:`${CHANGE_LABELS[key]||key} ${pct(before)}% → ${pct(after)}%`,why});
    }
    const debtBefore=num(a.sleep_debt),debtAfter=num(b.sleep_debt);
    if(debtBefore!==null&&debtAfter!==null&&Math.abs(debtAfter-debtBefore)>=eps.debt)out.push({at:b.at,text:`睡眠债 ${fix(debtBefore)}h → ${fix(debtAfter)}h`,why});
   }
  }
  if(withEpisodes){
   const sch=dayRows();
   for(const ep of (sch.episodes||[]).slice(-40)){
    const before=ep.state_before||{},after=ep.state_after||{};
    const atMinute=num(ep.actual_start_minute)??num(ep.planned_start_minute);
    if(atMinute===null)continue;
    let at;try{at=new Date(zonedMidnight(ep.schedule_date||sch.date||selectedDay||dayText(data?.generated_at))+atMinute*60000).toISOString()}catch{continue}
    // state_before/after 是明确的来源证据，可以标注因果。
    const why='活动边界「'+String(ep.name||'未命名').slice(0,40)+'」';
    for(const key of tracked.concat('sleep_debt')){
     const valueBefore=num(before[key]),valueAfter=num(after[key]);
     if(valueBefore===null||valueAfter===null)continue;
     const threshold=key==='sleep_debt'?eps.debt:eps.state;
     if(Math.abs(valueAfter-valueBefore)<threshold)continue;
     const show=key==='sleep_debt'?fix(valueBefore)+'h':pct(valueBefore)+'%';
     out.push({at,text:`${CHANGE_LABELS[key]||key} ${show} → ${key==='sleep_debt'?fix(valueAfter)+'h':pct(valueAfter)}%`,why});
    }
   }
  }
  return dedupe(out);
 }
 function buildSleepChanges(eps){
  const out=[];
  for(const row of data?.sleep?.sessions||[]){
   // W-08：跨午夜会话（结束分钟早于开始分钟）结算时间戳补次日，排序贴近真实醒来时刻。
   let endMinute=num(row.actual_end_minute)??num(row.actual_start_minute);
   if(endMinute===null)continue;
   const startMinute=num(row.actual_start_minute);
   if(startMinute!==null&&endMinute<startMinute)endMinute+=1440;
   let at;try{at=new Date(zonedMidnight(row.schedule_date)+endMinute*60000).toISOString()}catch{continue}
   const why='醒来结算 · '+(WAKE_REASONS[row.wake_reason]||row.wake_reason||'未记录');
   const debtBefore=num(row.sleep_debt_before),debtAfter=num(row.sleep_debt_after);
   if(debtBefore!==null&&debtAfter!==null&&Math.abs(debtAfter-debtBefore)>=eps.debt)out.push({at,text:`睡眠债 ${fix(debtBefore)}h → ${fix(debtAfter)}h`,why});
   const energyStart=num(row.energy_start),energyEnd=num(row.energy_end);
   if(energyStart!==null&&energyEnd!==null&&Math.abs(energyEnd-energyStart)>=eps.state)out.push({at,text:`精力 ${pct(energyStart)}% → ${pct(energyEnd)}%`,why});
  }
  return dedupe(out);
 }
 function buildVadChanges(eps){
  const rows=Array.isArray(vadSeries().points)?vadSeries().points:[],out=[];
  for(let i=1;i<rows.length;i++){
   const a=rows[i-1],b=rows[i];
   if(!a||!b||a.estimated||b.estimated)continue;
   const why=CHANGE_REASONS[b.reason]||b.reason||'状态观察';
   for(const key of ['valence','arousal','dominance']){
    const before=num(a[key]),after=num(b[key]);
    if(before===null||after===null||Math.abs(after-before)<eps.state)continue;
    out.push({at:b.at,text:`${CHANGE_LABELS[key]} ${fix(before,2)} → ${fix(after,2)}`,why});
   }
  }
  return dedupe(out);
 }
 function renderChanges(e){
  if(!e.changesList)return;
  if(e.changesKind==='none'){e.changesList.replaceChildren(el('p','note','暂无对应历史记录'));return}
  const eps=changeEps(),cacheKey=e.changesKind+'|'+selectedDay;
  if(changesCache.src!==data||changesCache.key!==cacheKey){
   changesCache={src:data,key:cacheKey,items:e.changesKind==='sleep'?buildSleepChanges(eps):e.changesKind==='vad'?buildVadChanges(eps):buildStateChanges(eps,e.changesKind!=='episode',true)};
  }
  const items=changesCache.items.slice(0,eps.limit);
  if(!items.length){e.changesList.replaceChildren(el('p','note','最近没有达到记录阈值的变化'));return}
  e.changesList.replaceChildren(...items.map(item=>{
   const row=el('li');
   row.append(el('time','',clock(item.at,true)),el('span','change-what',item.text),el('span','change-why','来源：'+item.why));
   return row;
  }));
 }
 // W-15：详情打开/尺寸变化时只重渲染当前激活 tab 的内容（drawHistory/renderSleep/
 // renderMood/renderUsage 已由 renderActiveTab 按 tab 调度；详情只在其所属 tab 可达）。
 function redraw(){requestAnimationFrame(()=>{if(data){computePoints();renderActiveTab()}})}
 // 入口原位保留 + 提升到清晰交互层（U 计划 §4.2）：卡片自带 backdrop-filter 层叠
 // 上下文，单靠 z-index 升不到遮罩之上——portal **同一个元素**，占位标记保持原屏幕
 // 坐标（绝对定位按钮不参与布局流，视觉不搬家），关闭时原位归还。
 function unfloatEntry(entry){
  if(!entry?.floated)return;
  entry.floated=false;
  entry.button.classList.remove('fish-float');
  entry.button.style.cssText='';
  entry.home.replaceWith(entry.button);
  entry.home=null;
 }
 function floatEntry(entry){
  if(entry.floated)return;
  // 入口是绝对定位的小鱼按钮，不参与布局流：占位标记保持原屏幕坐标，
  // 关闭时原位归还（占位隐藏、不吃指针、不参与 Tab 序）。
  entry.home=el('span','section-fish-button fish-peek-home');
  entry.home.setAttribute('aria-hidden','true');
  entry.button.replaceWith(entry.home);
  document.body.append(entry.button);
  entry.button.classList.add('fish-float');
  entry.floated=true;
  syncFloat(entry);
 }
 function syncFloat(entry){
  if(!entry?.floated||!entry.home)return;
  const r=entry.home.getBoundingClientRect(),b=entry.button;
  b.style.left=r.left+'px';b.style.top=r.top+'px';
  b.style.width=r.width+'px';b.style.height=r.height+'px';b.style.bottom='auto';
 }
 // 面板高度确定后贴到入口旁：上/下贴附 + 三角缺口对准入入口（随滚动/缩放重校准）。
 function snapPanel(entry){
  if(!current)return;
  if(entry.key==='usage'&&pinnedOpen){
   panel.style.top='12px';panel.style.bottom='auto';tip.hidden=true;return;
  }
  const b=entry.button.getBoundingClientRect(),vh=innerHeight;
  const pRect=panel.getBoundingClientRect();
  const below=panel.dataset.side==='below';
  let top=below?Math.min(b.bottom+12,vh-pRect.height-10):Math.max(b.top-12-pRect.height,10);
  // 锚定边：面板从入口一侧的固定边“生长”（top/bottom 二选一锚定），高度变化
  // 不移动贴附边；缺口（三角）坐标只依赖入口矩形，恒定对准入入口。
  if(below){
   panel.style.top=Math.round(b.bottom+12)+'px';panel.style.bottom='auto';
   tip.style.left=Math.round(b.left+b.width/2-7)+'px';
   tip.style.top=Math.round(b.bottom+5)+'px';tip.style.bottom='auto';
  }else{
   panel.style.bottom=Math.round(vh-b.top+12)+'px';panel.style.top='auto';
   tip.style.left=Math.round(b.left+b.width/2-7)+'px';
   tip.style.top=Math.round(b.top-19)+'px';tip.style.bottom='auto';
  }
  tip.hidden=false;
 }
 // 滚动/缩放/旋转后重新校准（测量节流到每帧一次，不逐帧全页布局）。
 const recalibrate=()=>{
  if(floatSyncQueued||!current)return;
  floatSyncQueued=true;
  requestAnimationFrame(()=>{floatSyncQueued=false;if(!current)return;syncFloat(current);snapPanel(current)});
 };
 window.addEventListener('scroll',recalibrate,{passive:true,capture:true});
 function close(restoreFocus=true){
  if(!current)return;
  const old=current,wasPinned=pinnedOpen;current=null;pinnedOpen=false;
  clearTimeout(closeTimer);clearTimeout(hoverTimer);
  unfloatEntry(old);old.element.classList.remove('fish-open');old.button.setAttribute('aria-expanded','false');
  if(old.key==='schedule')body.insertBefore(metadata,details);
  for(const group of old.groups)store.append(group);
  tip.hidden=true;panel.style.transformOrigin='';panel.style.left='';panel.style.top='';panel.style.maxHeight='';
  const finish=()=>{
   overlay.hidden=true;overlay.classList.remove('is-open','is-closing');overlay.removeAttribute('data-section');delete overlay.dataset.align;
   unlockScroll();
   if(typeof Mascot!=='undefined')Mascot.setDetailMode(false);
   if(restoreFocus&&wasPinned&&lastTrigger)lastTrigger.focus({preventScroll:true});
  };
  if(reduceMotion()){finish();return}
  overlay.classList.remove('is-open');overlay.classList.add('is-closing');
  closeTimer=setTimeout(finish,finishMs()+40);
 }
 // 以本次入口为原点计算面板位置/缩放原点（U 计划 §4.2 + 0425 目检适配）：
 // 气泡放在「可用空间更多的一侧」（入口偏上→面板在其下方，偏下→在上方），
 // 面板最大高度适配该侧空间——入口与内容都完整可见，不拘泥粘连哪一边；
 // 连接边/气泡尾随后按实际位置绘制（drawTail 取最近边）。
 function placePanel(entry){
  const b=entry.button.getBoundingClientRect(),vh=innerHeight,vw=innerWidth,pad=12;
  const usagePinned=entry.key==='usage'&&pinnedOpen;
  const freeAbove=b.top,freeBelow=vh-b.bottom;
  const putBelow=freeBelow>=freeAbove;
  overlay.dataset.align=putBelow?'bottom':'top';
  panel.dataset.side=putBelow?'below':'above';
  const free=Math.max(putBelow?freeBelow:freeAbove,240)-24;
  panel.style.maxHeight=`${usagePinned?vh-pad*2:Math.min(Math.round(free),Math.round(vh*0.9))}px`;
  // 水平：面板左缘靠近入口（长内容向右铺开），不居中漂移；宽度显式设定
  // （absolute 子元素的百分比宽按 overlay 的 padding-box 解析，须自行钳制在视口内）
  const panelW=Math.min(1040,vw-pad*2);
  panel.style.width=`${Math.round(panelW)}px`;
  panel.style.left=`${Math.round(usagePinned?(vw-panelW)/2:Math.min(Math.max(b.left+b.width/2-140,pad),Math.max(pad,vw-pad-panelW)))}px`;
  const p=panel.getBoundingClientRect();
  panel.style.transformOrigin=
   `${Math.min(Math.max(b.left+b.width/2-p.left,0),p.width)}px ${Math.min(Math.max(b.top+b.height/2-p.top,0),p.height)}px`;
 }
 function pin(){
  if(!current||pinnedOpen)return;
  pinnedOpen=true;
  lockScroll();                                         // 固定后才启用滚动锁（补偿滚动条宽度）
  if(current.key==='usage'){placePanel(current);snapPanel(current)}
  closeButton.focus({preventScroll:true});
 }
 function open(entry,pinned=false){
  if(current===entry){if(pinned)pin();return}
  if(current)close(false);
  clearTimeout(closeTimer);clearTimeout(hoverTimer);overlay.classList.remove('is-closing');
  current=entry;lastTrigger=entry.button;pinnedOpen=pinned;
  for(const group of entry.groups){if(group.tagName==='DETAILS')group.open=true;details.append(group)}  // 折叠默认展开（0425 目检：详情内不再折叠）
  if(entry.key==='schedule')$('schedule-recovery').before(metadata);
  floatEntry(entry);
  overlay.hidden=false;overlay.dataset.section=entry.key;
  placePanel(entry);
  void overlay.offsetWidth;
  overlay.classList.add('is-open');
  entry.element.classList.add('fish-open');entry.button.setAttribute('aria-expanded','true');
  if(pinned)lockScroll();
  if(typeof Mascot!=='undefined')Mascot.setDetailMode(true);
  refresh();
  redraw();
  requestAnimationFrame(()=>{syncFloat(entry);snapPanel(entry)});
  if(pinned)closeButton.focus({preventScroll:true});    // 悬停预览不抢键盘焦点
 }
 // Recover focus even if a refresh or host shortcut moved it outside the
 // overlay; listening only on the overlay misses that keyboard event.
 // 固定态才启用焦点陷阱；悬停预览不抢焦点、不锁页面（U 计划 §4.1）。
 document.addEventListener('keydown',e=>{
  if(e.key!=='Tab'||!current||!pinnedOpen)return;
  const focusables=[...panel.querySelectorAll('button,summary,input,textarea,select,[href],[tabindex]:not([tabindex="-1"])')].filter(node=>!node.disabled&&node.tabIndex>=0&&node.getClientRects().length);
  if(!focusables.length)return;
  const first=focusables[0],last=focusables[focusables.length-1];
  if(!focusables.includes(document.activeElement)){e.preventDefault();(e.shiftKey?last:first).focus({preventScroll:true})}
  else if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus({preventScroll:true})}
  else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus({preventScroll:true})}
 });
 document.addEventListener('keydown',e=>{
  if(e.key!=='Escape'||!current)return;
  // Esc 优先关说明 tooltip（U 计划 §4.1）：说明还开着就不收详情。
  const tip=document.querySelector('.info-tooltip:not([hidden])');
  if(tip){
   const infoButton=tip.parentElement?.querySelector('.info-button');
   if(infoButton)infoButton.setAttribute('aria-expanded','false');
   tip.hidden=true;
   e.preventDefault();
   return;
  }
  e.preventDefault();close(true);
 });
 overlay.addEventListener('pointerdown',e=>{if(e.target===overlay)e.preventDefault()});
 overlay.addEventListener('click',e=>{if(e.target===overlay)close(true)});
 overlay.addEventListener('toggle',e=>{if(e.target.tagName==='DETAILS'&&e.target.open&&!e.target.closest('#usage-detail'))redraw()},true);
 // 从小麦麦移到气泡正文保持展开；离开连接区域与气泡才延迟收起（未固定时）。
 const previewCloseDelay=()=>{clearTimeout(hoverTimer);hoverTimer=setTimeout(()=>{if(current&&!pinnedOpen)close(false)},160)};
 panel.addEventListener('pointerenter',()=>clearTimeout(hoverTimer));
 panel.addEventListener('pointerleave',()=>{if(current&&!pinnedOpen)previewCloseDelay()});
 window.addEventListener('resize',()=>{if(current?.key==='usage'&&pinnedOpen)placePanel(current);recalibrate();if(current)redraw()});
 function metadataRow(label,value,explanation){const row=el('div','fish-meta-row'),caption=el('span','note',label);if(explanation)caption.append(infoTip(explanation));row.append(caption,el('span','',value??'暂无记录'));return row}
 function refresh(){if(!current)return;const e=current,d=data||{},schedule=e.key==='schedule'?schedulePageInfo():null;
  title.textContent=(schedule?.label||e.title)+'详情';summary.textContent=e.summary(d);
  const rows=e.key==='usage'||(schedule&&schedule.key!=='record')?[]:[['更新时间',d.generated_at?clock(d.generated_at,true):'暂无记录',fullClock(d.generated_at)],['数据来源',imported?'导入快照':'生活引擎实时记录',''],...(e.meta?.(d)||[])];
  // Reconcile in place: a timer refresh must not remove the focused explanation.
  rows.forEach(([label,value,explanation],i)=>{let row=metadata.children[i];if(!row){row=metadataRow(label,value,explanation);metadata.append(row)}else{row.firstElementChild.firstChild.textContent=label;row.lastElementChild.textContent=value??'暂无记录';const tip=row.querySelector('.info-tooltip');if(tip)tip.textContent=explanation||'暂无说明'}});while(metadata.children.length>rows.length)metadata.lastElementChild.remove();
  renderChanges(e);
 }
 function fold(label,nodes){const list=nodes.filter(Boolean);if(!list.length)return null;let group;
  if(list.length===1&&list[0].tagName==='DETAILS'){group=list[0];group.open=false}
  else{group=el('details');group.append(el('summary','',label),...list)}group.classList.add('fish-fold');store.append(group);return group
 }
 function mount(key,titleText,element,summaryFn,metaFn=()=>[],groups=[],gridClasses=[]){if(!element)return;const content=el('div','fish-content');while(element.firstChild)content.append(element.firstChild);for(const cls of gridClasses){element.classList.remove(cls);content.classList.add(cls)}element.append(content);element.classList.add('fish-section');element.dataset.fishSection=key;
  // 详情入口＝板块左下角探头小鱼（必须保留的设计）：点击固定详情气泡，hover 预览，
  // 再点收起。portal/焦点/滚动锁机制都挂在这颗按钮上，aria-controls 指向详情面板。
  const button=el('button','section-fish-button');button.type='button';button.id='fish-'+key;button.setAttribute('aria-label','查看'+titleText+'详情 · 麦麦');button.setAttribute('aria-controls',overlay.id);button.setAttribute('aria-expanded','false');button.title='查看'+titleText+'详情 · 麦麦';const clip=el('span','fish-peek');clip.setAttribute('aria-hidden','true');clip.append($('section-fish-art').content.cloneNode(true));button.append(clip);element.append(button);
  const allGroups=groups.filter(Boolean);
  const kind=CHANGES_KIND[key];
  let changesList=null;
  if(kind){const changesGroup=el('details','fish-fold fish-changes');changesList=el('ul','fish-changelist');changesGroup.append(el('summary','','最近变化'),changesList);store.append(changesGroup);allGroups.push(changesGroup)}
  entries.set(key,{key,title:titleText,element,content,button,summary:summaryFn,meta:metaFn,groups:allGroups,changesKind:kind,changesList,marker:null,floated:false,home:null});
  // 语义（U 计划 §4.1）：hover 临时预览（不抢焦点/不锁滚动）→ 移入气泡保持 → 点击固定
  // → 再点同入口取消固定并收起 → 点另一入口关闭旧并固定新。触屏/键盘激活=直接打开固定。
  const entry=entries.get(key);
  button.onclick=()=>{
   if(current===entry){pinnedOpen?close(true):pin()}
   else open(entry,true)};
  if(matchMedia('(hover:hover) and (pointer:fine)').matches){
   // 武装位：只有「真实离开入口」后才允许 hover 重新打开。portal/回流引发的伪
   // 进入/伪离开（DOM 变更后 Chrome 重算 hover）不打开、不收起、不重新武装——
   // 否则取消固定后的关闭会被指针停留处的伪 enter 立即重新打开。
   entry._canHoverOpen=true;
   button.addEventListener('pointerenter',e=>{
    if(e.pointerType!=='mouse')return;
    clearTimeout(hoverTimer);
    if(overlay.classList.contains('is-closing')||!entry._canHoverOpen)return;
    if(current!==entry){entry._canHoverOpen=false;open(entry,false)}
   });
   button.addEventListener('pointerleave',e=>{
    if(e.pointerType!=='mouse')return;
    const x=e.clientX,y=e.clientY;
    const r=entry.button.getBoundingClientRect();
    if(x>=r.left&&x<=r.right&&y>=r.top&&y<=r.bottom)return;   // 伪离开（坐标仍在入口内）
    if(current===entry&&!pinnedOpen){
     clearTimeout(hoverTimer);
     hoverTimer=setTimeout(()=>{if(current===entry&&!pinnedOpen)close(false)},160);
    }
    entry._canHoverOpen=true;                                  // 真实离开：重新武装
   });
  }
 }
 const safeTime=(value)=>value?clock(value,true):'暂无记录';
 mount('current','当前活动',$('hero-scene'),d=>d.schedule?.current?.name||'暂无正在进行的活动',d=>[['活动来源',source(d.schedule?.current?.source),'只显示实际处于进行状态的活动，计划意图不等于已发生。'],['计划时段',d.schedule?.current?.time_label||'暂无记录','后端给出的本轮计划起止（time_label）；跨午夜段已标「次日」，与时间线「计划」行同口径。'],['进度基准','按本轮活动计划','进度由生活引擎按本轮计划时长与已用时计算；日意图时段可能不同，两者不一致时以本卡片口径为准。'],['状态时间',safeTime(d.state?.updated_at),fullClock(d.state?.updated_at)]]);
 const companionCard=$('tier').closest('article');mount('companion','陪伴',companionCard,d=>d.companion?.available?`${d.companion.tier_label||'关系记录'} · 今日主动 ${count(d.companion.speak_count)} / ${count(d.companion.daily_limit)}`:'暂无陪伴记录',d=>[['巡检时间',safeTime(d.companion?.last_tick_at),fullClock(d.companion?.last_tick_at)],['今日主动','巡检与想念合并统计','早安、晚安单独展示。']]);
 mount('state','身心状态',$('metrics'),d=>d.state_available?'五项状态反映当前快照；饥饿与压力越高，需求越明显。':'暂无生活状态记录',d=>[['采样间隔','约 5 分钟','正常tick每5分钟至多采样一次，另保留活动和醒沿观察。'],['更新时间',safeTime(d.state?.updated_at),fullClock(d.state?.updated_at)],['精神状态',num(d.state?.mental_state)===null?'暂无记录':pct(d.state.mental_state)+'%','由精力×专注×(1−压力)得到；专注为0表示注意力涣散，压力为0表示放松。']],[],['grid','metrics']);
 const historyCard=$('history-chart').closest('section');
 // W-01：采样明细入口（按钮+表格）留在主页面 chartfoot 内联折叠，不再移入详情 store；
 // 详情层只保留「最近变化」等派生内容。
 mount('history','状态变化',historyCard,()=>selectedDay?selectedDay+'的真实记录与估算':'最近'+hours+'小时的状态变化',d=>[['保留期','最近 7 天','最多2304个真实观察点；达到上限淘汰最旧点。'],['真实点',points.filter(p=>!p.estimated).length+' 个','这些值来自实际采样，不包括中间估算点。'],['估算点',points.filter(p=>p.estimated).length+' 个','仅两端有真实记录、且中间缺口不超过2小时才线性估算；虚线不计入真实点数。'],['起始时间',safeTime(lifeSeries().started_at||points[0]?.at),fullClock(lifeSeries().started_at||points[0]?.at)]],[]);
 function dynamicSleepRows(info){
  if(!info)return [];
  const bands={awake:'清醒',drowsy:'有点困',winding_down:'准备休息',asleep:'睡眠中',waking:'刚醒'};
  const naps={taken:'已安排午休',late_wake:'起床太晚',not_sleepy:'还不困',busy:'活动进行中',hard_commitment:'临近约定',no_wake:'暂无今日醒来记录',window_closed:'已过午休窗口',ready:'可以安排'};
  const decisions={score:'困倦达到入睡条件',anchor_bound:'入睡窗口到点',manual:'管理员请求',hard_commitment:'临近约定',busy:'当前活动优先',waiting:'继续观察'};
  return [
   ['动态睡眠',bands[info.band]||'等待评估',decisions[info.decision]||'按当前状态和日程滚动评估。'],
   ['入睡评分',info.score??'暂无记录',['pressure','circadian','debt','schedule','activity'].map((k,i)=>['精力','节律','睡眠债','日程','活动'][i]+' '+(info[k]??'—')).join(' · ')],
   ['入睡窗口',safeTime(info.window_start)+' → '+safeTime(info.window_end),fullClock(info.window_start)+' → '+fullClock(info.window_end)],
   ['起床窗口',safeTime(info.earliest_wake)+' → '+safeTime(info.latest_wake),fullClock(info.earliest_wake)+' → '+fullClock(info.latest_wake)],
   ['节律调整',info.phase_minutes==null?'暂无记录':info.phase_minutes+' 分钟','只根据已记录的自然夜缓慢调整。'],
   ['午休',naps[info.nap_reason]||'等待午休窗口','清醒满三小时且确有困意时才安排。']
  ];
 }
 const sleepCard=document.querySelector('.sleep-combined');const archive=sleepCard.querySelector('.sleep-archive');const sleepGroups=[fold('睡眠债的变化',[$('debt-chart')]),fold('作息与会话说明',[$('sleep-anchors')]),fold('最近睡眠会话',[archive])];
 mount('sleep','睡眠与恢复',sleepCard,d=>d.sleep?.current?.asleep?'正在休息，恢复以真实会话记录为准。':'当前没有进行中的睡眠会话。',d=>[['睡眠债',fix(d.state?.sleep_debt)+' 小时','这是尚未补足的休息时长记录，范围0–12小时；正式醒来时按规则结算。'],['连续睡眠',fix(d.sleep?.current?.asleep?d.sleep.current.continuous_minutes:(d.state?.continuous_sleep_minutes??0),0)+' 分钟','连续段可能因已确认的打断重置，不代表本夜总时长。'],['来源','睡眠引擎会话','离线补全会明确标记；没有采样依据的段不会变成真实点。']].concat(dynamicSleepRows(d.sleep?.dynamic)),sleepGroups);
 const scheduleDetail=$('schedule-detail'),poolDetail=$('schedule-pool-detail');store.append(scheduleDetail,poolDetail);
 mount('schedule','行程记录',$('panel-schedule'),d=>schedulePageInfo().key==='record'?
  selectedDay||d.schedule?.date||'当日':schedulePageInfo().note,()=>[],[scheduleDetail,poolDetail]);
 const moodCard=$('vad-chart').closest('article'),vadLegend=moodCard.querySelector('.legend');const moodGroups=[fold('心情的变化',[vadLegend,$('vad-chart')]),fold('睡眠调整记录',[$('referee-health')])];
 mount('mood','当前情绪',moodCard,d=>d.emotion?.available?(d.emotion.label||'心情平稳'):'暂无心情记录',d=>[['当前状态',d.emotion?.frozen?'睡眠冻结中':'自然变化中','睡眠期间暂停自然变化，醒后按真实睡眠会话进行调整。'],['最近调整',safeTime(d.emotion?.referee?.last_success_at),fullClock(d.emotion?.referee?.last_success_at)]],moodGroups);
 const qzoneCard=$('qzone-summary').closest('article');const qzoneGroups=[fold('最近发送记录',[$('delivery-list')]),fold('本地草稿预览',[qzoneCard.querySelector('.draft-preview')])];
 mount('sharing','空间与分享',qzoneCard,d=>d.qzone?.available?`今日分享 ${d.qzone.posts_today??0}/${d.qzone.daily_max_posts??0}`:'暂无分享记录',d=>[['最近状态获取',safeTime(d.qzone?.cookie_health?.last_success_at),fullClock(d.qzone?.cookie_health?.last_success_at)],['最近失败',safeTime(d.qzone?.cookie_health?.last_failure_at),fullClock(d.qzone?.cookie_health?.last_failure_at)],['发送结果','以确认记录为准','结果未知时不会自动重复发送。']],qzoneGroups);
 const usageCard=$('usage-metrics').closest('section'),usageDetail=$('usage-detail');store.append(usageDetail);
 mount('usage','模型调用',usageCard,()=>'',()=>[],[usageDetail]);
 const healthGroup=fold('数据来源与运行明细',[$('health-data'),$('warnings')]);
 mount('health','数据与运行说明',$('health-section'),d=>d.warnings?.length?`${d.warnings.length} 项数据提示`:'随真实记录更新',d=>[['新鲜度',d.stale?'状态快照可能已过期':'查看顶部更新状态','独立计时超过45秒提示可能过期，120秒提示陈旧。'],['刷新方式',imported?'离线快照':'约30秒读取一次','页面隐藏时暂停轮询；失败保留上次快照，8秒超时后可重试。']],[healthGroup]);
 return {close,refresh};
})();
