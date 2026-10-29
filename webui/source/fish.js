/* One shared, modeless details layer; legacy render targets are moved, never copied. */
function fullClock(value){
 if(!value||!/^\d{4}-\d{2}-\d{2}/.test(String(value))||!date(value))return '暂无完整时间';
 try{return new Intl.DateTimeFormat('zh-CN',{timeZone:data?.timezone||'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23',timeZoneName:'short'}).format(date(value))+' · '+(data?.timezone||'Asia/Shanghai')}
 catch{return '时间格式暂不可用'}
}
const SectionFish=(()=>{
 const store=el('div');store.id='fish-detail-store';store.hidden=true;document.body.append(store);
 const popup=el('aside','section-fish-popover');popup.id='section-fish-popover';popup.hidden=true;popup.setAttribute('role','dialog');popup.setAttribute('aria-modal','false');popup.setAttribute('aria-labelledby','fish-popup-title');
 const heading=el('div','fish-popup-heading'),title=el('h2','','详情'),pinNote=el('span','note'),closeButton=el('button','fish-close','关闭');title.id='fish-popup-title';closeButton.type='button';closeButton.setAttribute('aria-label','关闭板块详情');heading.append(title,pinNote,closeButton);
 const body=el('div','fish-popup-body'),summary=el('p','fish-popup-summary'),metadata=el('div','fish-metadata'),details=el('div','fish-records');body.append(summary,metadata,details);popup.append(heading,body);document.body.append(popup);
 let current=null,pinned=false,hideTimer=null,skipFocus=false,redrawFrame=null;
 const entries=new Map(),mobile=()=>matchMedia('(max-width:700px), (hover:none)').matches;
 function cancelHide(){clearTimeout(hideTimer);hideTimer=null}
 function scheduleHide(){cancelHide();if(!pinned)hideTimer=setTimeout(()=>{if(!pinned&&!popup.contains(document.activeElement))close()},150)}
 function redraw(){cancelAnimationFrame(redrawFrame);redrawFrame=requestAnimationFrame(()=>{if(data){drawHistory();renderSleep();renderModules()}position()})}
 function restoreButton(entry){if(entry?.marker){entry.marker.replaceWith(entry.button);entry.marker=null;entry.button.classList.remove('fish-in-heading')}}
 function position(){if(!current||popup.hidden)return;if(mobile()){if(!current.marker){current.marker=document.createComment('fish-button-home');current.button.replaceWith(current.marker);current.button.classList.add('fish-in-heading');heading.insertBefore(current.button,closeButton)}popup.style.left='12px';popup.style.top='';popup.style.bottom='calc(78px + env(safe-area-inset-bottom))';return}restoreButton(current);
  popup.style.bottom='';const box=current.button.getBoundingClientRect(),width=Math.min(370,innerWidth-24),height=popup.getBoundingClientRect().height;
  const left=Math.max(12,Math.min(innerWidth-width-12,box.right-width+18));let top=box.bottom+8;if(top+height>innerHeight-12&&box.top-height-8>=12)top=box.top-height-8;top=Math.max(12,Math.min(top,innerHeight-height-12));
  popup.style.left=left+'px';popup.style.top=top+'px';popup.style.setProperty('--fish-arrow-x',Math.max(16,Math.min(width-24,box.left+box.width/2-left))+'px');popup.dataset.above=String(top<box.top)
 }
 function close(restoreFocus=false){cancelHide();if(!current)return;const old=current;restoreButton(old);old.element.classList.remove('fish-open');old.button.setAttribute('aria-expanded','false');old.button.setAttribute('aria-pressed','false');for(const group of old.groups)store.append(group);popup.hidden=true;popup.removeAttribute('data-section');current=null;pinned=false;if(restoreFocus){skipFocus=true;old.button.focus({preventScroll:true});skipFocus=false}}
 function metadataRow(label,value,explanation){const row=el('div','fish-meta-row'),caption=el('span','note',label);if(explanation)caption.append(infoTip(explanation));row.append(caption,el('span','',value??'暂无记录'));return row}
 function refresh(){if(!current)return;const e=current,d=data||{};title.textContent=e.title+'详情';summary.textContent=e.summary(d);
  const rows=[['更新时间',d.generated_at?clock(d.generated_at,true):'暂无记录',fullClock(d.generated_at)],['数据来源',imported?'导入的只读快照':'插件只读查询','只展示已记录字段；不读取聊天原文、账号或凭据。'],...(e.meta?.(d)||[])];
  // Reconcile in place: a timer refresh must not remove the focused explanation.
  rows.forEach(([label,value,explanation],i)=>{let row=metadata.children[i];if(!row){row=metadataRow(label,value,explanation);metadata.append(row)}else{row.firstElementChild.firstChild.textContent=label;row.lastElementChild.textContent=value??'暂无记录';const tip=row.querySelector('.info-tooltip');if(tip)tip.textContent=explanation||'暂无说明'}});while(metadata.children.length>rows.length)metadata.lastElementChild.remove();
  pinNote.textContent=pinned?'已固定':'悬停查看';popup.dataset.pinned=String(pinned);e.button.setAttribute('aria-pressed',String(pinned));position()
 }
 function open(e,pin=false,keyboard=false){cancelHide();if(current!==e){close();current=e;for(const group of e.groups){group.open=false;details.append(group)}}pinned=pin||pinned;popup.hidden=false;popup.dataset.section=e.key;e.element.classList.add('fish-open');e.button.setAttribute('aria-expanded','true');refresh();redraw();if(keyboard)closeButton.focus({preventScroll:true})}
 function fold(label,nodes){const list=nodes.filter(Boolean);if(!list.length)return null;let group;
  if(list.length===1&&list[0].tagName==='DETAILS'){group=list[0];group.open=false}
  else{group=el('details');group.append(el('summary','',label),...list)}group.classList.add('fish-fold');store.append(group);return group
 }
 function mount(key,title,element,summaryFn,metaFn=()=>[],groups=[],gridClasses=[]){if(!element)return;const content=el('div','fish-content');while(element.firstChild)content.append(element.firstChild);for(const cls of gridClasses){element.classList.remove(cls);content.classList.add(cls)}element.append(content);element.classList.add('fish-section');element.dataset.fishSection=key;
  const button=el('button','section-fish-button');button.type='button';button.id='fish-'+key;button.setAttribute('aria-label','查看'+title+'详情');button.setAttribute('aria-controls',popup.id);button.setAttribute('aria-expanded','false');button.setAttribute('aria-pressed','false');button.title='查看'+title+'详情';const clip=el('span','fish-peek');clip.setAttribute('aria-hidden','true');clip.append($('section-fish-art').content.cloneNode(true));button.append(clip);element.append(button);
  const entry={key,title,element,content,button,summary:summaryFn,meta:metaFn,groups:groups.filter(Boolean)};entries.set(key,entry);
  button.onpointerenter=e=>{if(e.pointerType!=='touch'&&!mobile())open(entry)};button.onpointerleave=scheduleHide;button.onfocus=()=>{if(!skipFocus&&!mobile())open(entry)};
  button.onclick=e=>{if(current===entry&&pinned){close();return}open(entry,true,e.detail===0)};
 }
 popup.onpointerenter=cancelHide;popup.onpointerleave=scheduleHide;closeButton.onclick=()=>close(true);
 popup.addEventListener('toggle',e=>{if(e.target.tagName==='DETAILS'&&e.target.open)redraw()},true);
 document.addEventListener('keydown',e=>{if(e.key==='Escape'&&current){e.preventDefault();close(true)}});
 document.addEventListener('pointerdown',e=>{if(current&&!popup.contains(e.target)&&!e.target.closest('.section-fish-button'))close()});
 document.addEventListener('focusin',e=>{if(current&&!popup.contains(e.target)&&!current.button.contains(e.target))close()});
 window.addEventListener('resize',()=>{position();if(current)redraw()});window.addEventListener('scroll',position,{passive:true});
 const safeTime=(value)=>value?clock(value,true):'暂无记录';
 mount('current','当前活动',$('hero-scene'),d=>d.schedule?.current?.name||'暂无正在进行的活动',d=>[['活动来源',source(d.schedule?.current?.source),'只显示实际处于进行状态的活动，计划意图不等于已发生。'],['状态时间',safeTime(d.state?.updated_at),fullClock(d.state?.updated_at)]]);
 const companionCard=$('tier').closest('article');mount('companion','陪伴',companionCard,d=>d.companion?.available?`${d.companion.tier_label||'关系记录'} · 今日主动 ${d.companion.speak_count??0}/${d.companion.daily_limit??'—'}`:'暂无陪伴记录',d=>[['巡检时间',safeTime(d.companion?.last_tick_at),fullClock(d.companion?.last_tick_at)],['今日主动','巡检与想念合并统计','早安、晚安单独展示；这里不会触发任何消息。']]);
 mount('state','身心状态',$('metrics'),d=>d.state_available?'五项状态反映当前快照；饥饿与压力越高，需求越明显。':'暂无生活状态记录',d=>[['采样间隔','约 5 分钟','正常tick每5分钟至多采样一次，另保留活动和醒沿观察。'],['更新时间',safeTime(d.state?.updated_at),fullClock(d.state?.updated_at)],['精神状态',num(d.state?.mental_state)===null?'暂无记录':pct(d.state.mental_state)+'%','由精力×专注×(1−压力)得到；专注为0表示注意力涣散，压力为0表示放松。']],[],['grid','metrics']);
 const historyCard=$('history-chart').closest('section');const sampleGroup=fold('采样明细',[$('samples-toggle'),$('samples')]);
 mount('history','生活起伏',historyCard,()=>selectedDay?selectedDay+'的真实记录与估算':'最近'+hours+'小时的状态变化',d=>[['保留期','最近 7 天','最多2304个真实观察点；达到上限淘汰最旧点。'],['真实点',points.filter(p=>!p.estimated).length+' 个','这些值来自实际采样，不包括中间估算点。'],['估算点',points.filter(p=>p.estimated).length+' 个','仅两端有真实记录、且中间缺口不超过2小时才线性估算；虚线不计入真实点数。'],['起始时间',safeTime(lifeSeries().started_at||points[0]?.at),fullClock(lifeSeries().started_at||points[0]?.at)]],[sampleGroup]);
 const sleepCard=document.querySelector('.sleep-combined');const archive=sleepCard.querySelector('.sleep-archive');const sleepGroups=[fold('睡眠债的变化',[$('debt-chart')]),fold('作息与会话说明',[$('sleep-anchors')]),fold('最近睡眠会话',[archive])];
 mount('sleep','睡眠与恢复',sleepCard,d=>d.sleep?.current?.asleep?'正在休息，恢复以真实会话记录为准。':'当前没有进行中的睡眠会话。',d=>[['睡眠债',fix(d.state?.sleep_debt)+' 小时','这是尚未补足的休息时长记录，范围0–12小时；正式醒来时按规则结算。'],['连续睡眠',fix(d.sleep?.current?.asleep?d.sleep.current.continuous_minutes:(d.state?.continuous_sleep_minutes??0),0)+' 分钟','连续段可能因已确认的打断重置，不代表本夜总时长。'],['来源','睡眠引擎会话','离线补全会明确标记；没有采样依据的段不会变成真实点。']],sleepGroups);
 const scheduleLists=document.querySelector('.schedulegrid');const scheduleGroup=fold('实际足迹与计划明细',[scheduleLists]);
 mount('schedule','一天的篇章',$('panel-schedule'),d=>`${selectedDay||d.schedule?.date||'当日'} · 计划与实际可以不同`,d=>[['来源','日意图与实际活动记录','双时间轴分别表示计划和实际；中断、未完成、已清理的计划如实显示。'],['计划记录',dayView()?.intent?.plans_available===false?'已超过保留期':'查看下方明细',dayView()?.intent?.note||'当前计划最多80条、实际足迹最多120条；日视图每序列最多288点。']],[scheduleGroup]);
 const moodCard=$('vad-chart').closest('article'),vadLegend=moodCard.querySelector('.legend');const moodGroups=[fold('心情的变化',[vadLegend,$('vad-chart')]),fold('睡眠调整记录',[$('referee-health')])];
 mount('mood','心情底色',moodCard,d=>d.emotion?.available?(d.emotion.label||'心情平稳'):'暂无心情记录',d=>[['当前状态',d.emotion?.frozen?'睡眠冻结中':'自然变化中','睡眠期间暂停自然变化，醒后按真实睡眠会话进行调整。'],['最近调整',safeTime(d.emotion?.referee?.last_success_at),fullClock(d.emotion?.referee?.last_success_at)]],moodGroups);
 const qzoneCard=$('qzone-summary').closest('article');const qzoneGroups=[fold('最近发送记录',[$('delivery-list')]),fold('本地草稿预览',[qzoneCard.querySelector('.draft-preview')])];
 mount('sharing','空间与分享',qzoneCard,d=>d.qzone?.available?`今日分享 ${d.qzone.posts_today??0}/${d.qzone.daily_max_posts??0}`:'暂无分享记录',d=>[['最近状态获取',safeTime(d.qzone?.cookie_health?.last_success_at),fullClock(d.qzone?.cookie_health?.last_success_at)],['最近失败',safeTime(d.qzone?.cookie_health?.last_failure_at),fullClock(d.qzone?.cookie_health?.last_failure_at)],['发送结果','以确认记录为准','结果未知时不会自动重复发送；这里不展示或读取任何凭据内容。']],qzoneGroups);
 const usageCard=$('usage-metrics').closest('section'),usageGroup=fold('调用构成与模型明细',[usageCard.querySelector('.usage-details')]);
 mount('usage','模型调用',usageCard,d=>`最近 ${d.usage?.window_hours||24} 小时 · ${d.usage?.calls??0} 次调用`,d=>[['调用次数',d.usage?.calls??'暂无记录',usageTerms().calls],['Token 消耗',d.usage?.tokens??'暂无记录',usageTerms().tokens],['缺用量来源调用',d.usage?.missing_usage_source_calls??'暂无记录',usageTerms().missing_usage_source_calls]],[usageGroup]);
 const healthGroup=fold('数据来源与运行明细',[$('health-data'),$('warnings')]);
 mount('health','数据与运行说明',$('health-section'),d=>d.warnings?.length?`${d.warnings.length} 项数据提示`:'只读展示，随真实记录更新',d=>[['新鲜度',d.stale?'状态快照可能已过期':'查看顶部更新状态','页面读取成功不等于状态新鲜；独立计时超过45秒提示可能过期、120秒提示陈旧。'],['刷新方式',imported?'离线快照':'约30秒读取一次','页面隐藏时暂停轮询；失败保留上次快照，8秒超时后可重试。']],[healthGroup]);
 return {close,refresh,position};
})();
