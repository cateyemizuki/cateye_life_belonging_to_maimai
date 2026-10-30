/* Fullscreen details layer: bottom-left entries activate one modal per section;
   the trigger button moves into the panel header and back on close.
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
 let current=null,lastTrigger=null,closeTimer=null;
 const entries=new Map();
 let changesCache={src:null,key:'',items:[]};
 const reduceMotion=()=>matchMedia('(prefers-reduced-motion: reduce)').matches;
 const finishMs=()=>{const raw=parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--maimai-detail-fade-ms'));return Number.isFinite(raw)&&raw>0?raw:220};
 // 「最近变化」：前端从 history.points / schedule.episodes / sleep.sessions 派生，不改 API。
 const CHANGES_KIND={current:'episode',companion:'none',state:'state',history:'state',sleep:'sleep',schedule:'episode',mood:'vad',sharing:'none',usage:'none',health:null};
 const CHANGE_REASONS={start:'启动观察',tick:'生活采样',sleep:'睡眠采样',episode:'活动边界',wake:'醒来结算',sleep_settlement:'睡眠结算',estimated_gap_fill:'估算'};
 const CHANGE_LABELS={...labels,mental_state:'精神状态'};
 const WAKE_REASONS={energy_threshold:'精力达标',lie_in_cap:'赖床上限',hard_commitment:'硬约定唤醒',interrupted_awake:'被暂时打断',manual:'手动唤醒',no_sleep_mode:'免睡模式',offline_reconnect:'离线补眠估算'};
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
   const endMinute=num(row.actual_end_minute)??num(row.actual_start_minute);
   if(endMinute===null)continue;
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
 function redraw(){requestAnimationFrame(()=>{if(data){drawHistory();renderSleep();renderModules()}})}
 function restoreButton(entry){if(entry?.marker){entry.marker.replaceWith(entry.button);entry.marker=null;entry.button.classList.remove('fish-in-heading')}}
 function moveButtonIntoHeading(entry){if(entry.marker)return;entry.marker=document.createComment('fish-button-home');entry.button.replaceWith(entry.marker);entry.button.classList.add('fish-in-heading');heading.insertBefore(entry.button,closeButton)}
 function close(restoreFocus=true){
  if(!current)return;
  const old=current;current=null;
  clearTimeout(closeTimer);
  restoreButton(old);old.element.classList.remove('fish-open');old.button.setAttribute('aria-expanded','false');
  for(const group of old.groups)store.append(group);
  const finish=()=>{
   overlay.hidden=true;overlay.classList.remove('is-open','is-closing');overlay.removeAttribute('data-section');
   document.body.classList.remove('fish-locked');
   if(typeof Mascot!=='undefined')Mascot.setAway(false);
   if(restoreFocus&&lastTrigger)lastTrigger.focus({preventScroll:true});
  };
  if(reduceMotion()){finish();return}
  overlay.classList.remove('is-open');overlay.classList.add('is-closing');
  closeTimer=setTimeout(finish,finishMs()+40);
 }
 function open(entry){
  if(current===entry)return;
  if(current)close(false);
  clearTimeout(closeTimer);overlay.classList.remove('is-closing');
  current=entry;lastTrigger=entry.button;
  for(const group of entry.groups){group.open=false;details.append(group)}
  moveButtonIntoHeading(entry);
  overlay.hidden=false;overlay.dataset.section=entry.key;
  void overlay.offsetWidth;
  overlay.classList.add('is-open');
  entry.element.classList.add('fish-open');entry.button.setAttribute('aria-expanded','true');
  document.body.classList.add('fish-locked');
  if(typeof Mascot!=='undefined')Mascot.setAway(true);
  refresh();
  redraw();
  closeButton.focus({preventScroll:true});
 }
 // Recover focus even if a refresh or host shortcut moved it outside the
 // overlay; listening only on the overlay misses that keyboard event.
 document.addEventListener('keydown',e=>{
  if(e.key!=='Tab'||!current)return;
  const focusables=[...panel.querySelectorAll('button,summary,input,textarea,select,[href],[tabindex]:not([tabindex="-1"])')].filter(node=>!node.disabled&&node.tabIndex>=0&&node.getClientRects().length);
  if(!focusables.length)return;
  const first=focusables[0],last=focusables[focusables.length-1];
  if(!focusables.includes(document.activeElement)){e.preventDefault();(e.shiftKey?last:first).focus({preventScroll:true})}
  else if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus({preventScroll:true})}
  else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus({preventScroll:true})}
 });
 document.addEventListener('keydown',e=>{if(e.key==='Escape'&&current){e.preventDefault();close(true)}});
 overlay.addEventListener('pointerdown',e=>{if(e.target===overlay)e.preventDefault()});
 overlay.addEventListener('click',e=>{if(e.target===overlay)close(true)});
 overlay.addEventListener('toggle',e=>{if(e.target.tagName==='DETAILS'&&e.target.open)redraw()},true);
 window.addEventListener('resize',()=>{if(current)redraw()});
 function metadataRow(label,value,explanation){const row=el('div','fish-meta-row'),caption=el('span','note',label);if(explanation)caption.append(infoTip(explanation));row.append(caption,el('span','',value??'暂无记录'));return row}
 function refresh(){if(!current)return;const e=current,d=data||{};title.textContent=e.title+'详情';summary.textContent=e.summary(d);
  const rows=[['更新时间',d.generated_at?clock(d.generated_at,true):'暂无记录',fullClock(d.generated_at)],['数据来源',imported?'导入快照':'生活引擎实时记录',''],...(e.meta?.(d)||[])];
  // Reconcile in place: a timer refresh must not remove the focused explanation.
  rows.forEach(([label,value,explanation],i)=>{let row=metadata.children[i];if(!row){row=metadataRow(label,value,explanation);metadata.append(row)}else{row.firstElementChild.firstChild.textContent=label;row.lastElementChild.textContent=value??'暂无记录';const tip=row.querySelector('.info-tooltip');if(tip)tip.textContent=explanation||'暂无说明'}});while(metadata.children.length>rows.length)metadata.lastElementChild.remove();
  renderChanges(e);
 }
 function fold(label,nodes){const list=nodes.filter(Boolean);if(!list.length)return null;let group;
  if(list.length===1&&list[0].tagName==='DETAILS'){group=list[0];group.open=false}
  else{group=el('details');group.append(el('summary','',label),...list)}group.classList.add('fish-fold');store.append(group);return group
 }
 function mount(key,titleText,element,summaryFn,metaFn=()=>[],groups=[],gridClasses=[]){if(!element)return;const content=el('div','fish-content');while(element.firstChild)content.append(element.firstChild);for(const cls of gridClasses){element.classList.remove(cls);content.classList.add(cls)}element.append(content);element.classList.add('fish-section');element.dataset.fishSection=key;
  const button=el('button','section-fish-button');button.type='button';button.id='fish-'+key;button.setAttribute('aria-label','查看'+titleText+'详情 · 麦麦');button.setAttribute('aria-controls',overlay.id);button.setAttribute('aria-expanded','false');button.title='查看'+titleText+'详情 · 麦麦';const clip=el('span','fish-peek');clip.setAttribute('aria-hidden','true');clip.append($('section-fish-art').content.cloneNode(true));button.append(clip);element.append(button);
  const allGroups=groups.filter(Boolean);
  const kind=CHANGES_KIND[key];
  let changesList=null;
  if(kind){const changesGroup=el('details','fish-fold fish-changes');changesList=el('ul','fish-changelist');changesGroup.append(el('summary','','最近变化'),changesList);store.append(changesGroup);allGroups.push(changesGroup)}
  entries.set(key,{key,title:titleText,element,content,button,summary:summaryFn,meta:metaFn,groups:allGroups,changesKind:kind,changesList,marker:null});
  // 只有点击/Enter/Space 显式激活全屏详情；hover 与普通聚焦不再打开。再次点击同一入口 = 关闭。
  button.onclick=()=>{const entry=entries.get(key);entry===current?close(true):open(entry)};
 }
 const safeTime=(value)=>value?clock(value,true):'暂无记录';
 mount('current','当前活动',$('hero-scene'),d=>d.schedule?.current?.name||'暂无正在进行的活动',d=>[['活动来源',source(d.schedule?.current?.source),'只显示实际处于进行状态的活动，计划意图不等于已发生。'],['状态时间',safeTime(d.state?.updated_at),fullClock(d.state?.updated_at)]]);
 const companionCard=$('tier').closest('article');mount('companion','陪伴',companionCard,d=>d.companion?.available?`${d.companion.tier_label||'关系记录'} · 今日主动 ${count(d.companion.speak_count)} / ${count(d.companion.daily_limit)}`:'暂无陪伴记录',d=>[['巡检时间',safeTime(d.companion?.last_tick_at),fullClock(d.companion?.last_tick_at)],['今日主动','巡检与想念合并统计','早安、晚安单独展示。']]);
 mount('state','身心状态',$('metrics'),d=>d.state_available?'五项状态反映当前快照；饥饿与压力越高，需求越明显。':'暂无生活状态记录',d=>[['采样间隔','约 5 分钟','正常tick每5分钟至多采样一次，另保留活动和醒沿观察。'],['更新时间',safeTime(d.state?.updated_at),fullClock(d.state?.updated_at)],['精神状态',num(d.state?.mental_state)===null?'暂无记录':pct(d.state.mental_state)+'%','由精力×专注×(1−压力)得到；专注为0表示注意力涣散，压力为0表示放松。']],[],['grid','metrics']);
 const historyCard=$('history-chart').closest('section');const sampleGroup=fold('采样明细',[$('samples-toggle'),$('samples')]);
 mount('history','生活起伏',historyCard,()=>selectedDay?selectedDay+'的真实记录与估算':'最近'+hours+'小时的状态变化',d=>[['保留期','最近 7 天','最多2304个真实观察点；达到上限淘汰最旧点。'],['真实点',points.filter(p=>!p.estimated).length+' 个','这些值来自实际采样，不包括中间估算点。'],['估算点',points.filter(p=>p.estimated).length+' 个','仅两端有真实记录、且中间缺口不超过2小时才线性估算；虚线不计入真实点数。'],['起始时间',safeTime(lifeSeries().started_at||points[0]?.at),fullClock(lifeSeries().started_at||points[0]?.at)]],[sampleGroup]);
 const sleepCard=document.querySelector('.sleep-combined');const archive=sleepCard.querySelector('.sleep-archive');const sleepGroups=[fold('睡眠债的变化',[$('debt-chart')]),fold('作息与会话说明',[$('sleep-anchors')]),fold('最近睡眠会话',[archive])];
 mount('sleep','睡眠与恢复',sleepCard,d=>d.sleep?.current?.asleep?'正在休息，恢复以真实会话记录为准。':'当前没有进行中的睡眠会话。',d=>[['睡眠债',fix(d.state?.sleep_debt)+' 小时','这是尚未补足的休息时长记录，范围0–12小时；正式醒来时按规则结算。'],['连续睡眠',fix(d.sleep?.current?.asleep?d.sleep.current.continuous_minutes:(d.state?.continuous_sleep_minutes??0),0)+' 分钟','连续段可能因已确认的打断重置，不代表本夜总时长。'],['来源','睡眠引擎会话','离线补全会明确标记；没有采样依据的段不会变成真实点。']],sleepGroups);
 const scheduleLists=document.querySelector('.schedulegrid');const scheduleGroup=fold('实际足迹与计划明细',[scheduleLists]);
 mount('schedule','一天的篇章',$('panel-schedule'),d=>`${selectedDay||d.schedule?.date||'当日'} · 计划与实际可以不同`,d=>[['来源','日意图与实际活动记录','计划与实际分列展示；中断、未完成、已清理的计划如实显示。'],['计划记录',dayView()?.intent?.plans_available===false?'已超过保留期':'查看下方明细',dayView()?.intent?.note||'当前计划最多80条、实际足迹最多120条；日视图每序列最多288点。']],[scheduleGroup]);
 const moodCard=$('vad-chart').closest('article'),vadLegend=moodCard.querySelector('.legend');const moodGroups=[fold('心情的变化',[vadLegend,$('vad-chart')]),fold('睡眠调整记录',[$('referee-health')])];
 mount('mood','心情底色',moodCard,d=>d.emotion?.available?(d.emotion.label||'心情平稳'):'暂无心情记录',d=>[['当前状态',d.emotion?.frozen?'睡眠冻结中':'自然变化中','睡眠期间暂停自然变化，醒后按真实睡眠会话进行调整。'],['最近调整',safeTime(d.emotion?.referee?.last_success_at),fullClock(d.emotion?.referee?.last_success_at)]],moodGroups);
 const qzoneCard=$('qzone-summary').closest('article');const qzoneGroups=[fold('最近发送记录',[$('delivery-list')]),fold('本地草稿预览',[qzoneCard.querySelector('.draft-preview')])];
 mount('sharing','空间与分享',qzoneCard,d=>d.qzone?.available?`今日分享 ${d.qzone.posts_today??0}/${d.qzone.daily_max_posts??0}`:'暂无分享记录',d=>[['最近状态获取',safeTime(d.qzone?.cookie_health?.last_success_at),fullClock(d.qzone?.cookie_health?.last_success_at)],['最近失败',safeTime(d.qzone?.cookie_health?.last_failure_at),fullClock(d.qzone?.cookie_health?.last_failure_at)],['发送结果','以确认记录为准','结果未知时不会自动重复发送。']],qzoneGroups);
 const usageCard=$('usage-metrics').closest('section'),usageGroup=fold('调用构成与模型明细',[usageCard.querySelector('.usage-details')]);
 mount('usage','模型调用',usageCard,d=>`最近 ${d.usage?.window_hours||24} 小时 · ${d.usage?.calls??0} 次调用`,d=>[['调用次数',d.usage?.calls??'暂无记录',usageTerms().calls],['Token 消耗',d.usage?.tokens??'暂无记录',usageTerms().tokens],['缺用量来源调用',d.usage?.missing_usage_source_calls??'暂无记录',usageTerms().missing_usage_source_calls]],[usageGroup]);
 const healthGroup=fold('数据来源与运行明细',[$('health-data'),$('warnings')]);
 mount('health','数据与运行说明',$('health-section'),d=>d.warnings?.length?`${d.warnings.length} 项数据提示`:'随真实记录更新',d=>[['新鲜度',d.stale?'状态快照可能已过期':'查看顶部更新状态','独立计时超过45秒提示可能过期，120秒提示陈旧。'],['刷新方式',imported?'离线快照':'约30秒读取一次','页面隐藏时暂停轮询；失败保留上次快照，8秒超时后可重试。']],[healthGroup]);
 return {close,refresh};
})();
