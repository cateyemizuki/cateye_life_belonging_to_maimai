
(()=>{'use strict';
// W-04 嵌入态标记：存在宿主桥（window.maibot）即视为嵌入宿主 iframe，给 <html> 加
// data-embedded——隐藏插件自身品牌区并收纳导出入口；离线独立打开 index.html 时不标记，
// 保留完整品牌 mast 与导入入口（§2.5 保留项）。
if(window.maibot)document.documentElement.dataset.embedded='true';
const $=id=>document.getElementById(id),NS='http://www.w3.org/2000/svg';
const spec=[['energy','精力','↑ 更有活力','EN'],['hunger','饥饿','↑ 更饿','HU'],['focus','专注','↑ 更能集中','FO'],['stress','压力','↑ 更紧绷','ST'],['social_battery','社交电量','↑ 更愿意交流','SO']];
const colors={energy:'--energy',hunger:'--hunger',focus:'--focus',stress:'--stress',social_battery:'--social',sleep_debt:'--debt',valence:'--energy',arousal:'--focus',dominance:'--stress'},labels=Object.fromEntries(spec.map(s=>[s[0],s[1]]));labels.sleep_debt='睡眠债';labels.valence='愉悦';labels.arousal='唤醒';labels.dominance='掌控';
const selected=new Set(spec.map(s=>s[0]));let data=null,hours=24,usageHours=(()=>{try{const v=Number(localStorage.getItem('lbmUsageHours'));return [24,168,720].includes(v)?v:720}catch(e){return 720}})(),usageRowsLoaded=false,usageRowsAll=[],usageFilter="",usageParamSupported=true,busy=false,live=false,timer=null,failures=0,points=[],imported=false,zoom=null,selectedDay="",activeTab="now",requestSeq=0,lastSuccessAt=0,lastError="",freshnessTimer=null,importSeq=0;
const num=v=>typeof v==='number'&&Number.isFinite(v)?v:null,pct=v=>num(v)===null?'—':Math.round(Math.max(0,Math.min(1,v))*100),fix=(v,d=1)=>num(v)===null?'—':v.toFixed(d),text=(id,s)=>{$(id).textContent=String(s??'—')};
// 次数展示口径：有限非负整数及等价数字字符串（5 / 5.0 / "5.0"）→ 整数；null/空/非法 → —。
// 不四舍五入业务值，非整数异常按缺失展示，不改底层值。
const count=v=>{const n=typeof v==='number'?v:(typeof v==='string'&&v.trim()?Number(v):NaN);return Number.isSafeInteger(n)&&n>=0?String(n):'—'};
const group=v=>{const n=Number(v);return typeof v==='string'&&v.trim()!==''&&Number.isSafeInteger(n)&&n>=0?n.toLocaleString('en-US'):v};
function el(tag,cls,content){const e=document.createElement(tag);if(cls)e.className=cls;if(content!==undefined)e.textContent=String(content);return e}
function date(v){const t=new Date(v);return v&&Number.isFinite(+t)?t:null}
function clock(v,withDate=false){if(!date(v))return '—';try{return new Intl.DateTimeFormat('zh-CN',{timeZone:data?.timezone||'Asia/Shanghai',...(withDate?{month:'2-digit',day:'2-digit'}:{}),hour:'2-digit',minute:'2-digit',hour12:false}).format(date(v))}catch{return '时间格式不可用'}}
// 分钟展示与 planning/time_utils.format_day_minute（唯一实现）同口径：偏移恒 ±1，
// 同向偏移统一标「次日」，不做多日标号；负值防御标「前日」。位移不在本函数做。
function minute(v){if(num(v)===null)return '—';const t=Math.floor(v),d=Math.floor(t/1440);return(d>0?'次日 ':d<0?'前日 ':'')+String(Math.floor(((t%1440)+1440)%1440/60)).padStart(2,'0')+':'+String(((t%60)+60)%60).padStart(2,'0')}
// W-08 跨午夜补次日（2026-11-03）：段结束早于开始（end<start，如 23:00→01:00）时，
// 展示层把结束分钟补 +1440（「次日」），避免被误读成 2 小时内的先后顺序。
// 数据侧 planning/time_utils.format_day_minute 已对 >=1440 的分钟能标「次日」；
// 这里只兜住数据里仍用 0 点环表示跨午夜段的情形，不改业务值。
function spanEnd(start,end){const s=num(start),e=num(end);return e===null?null:(s!==null&&e<s?e+1440:e)}
function empty(target,title,desc){const box=el('div','empty');box.append(el('strong','',title),el('span','note',desc));target.replaceChildren(box)}
function notice(message,error=false){$('notice').hidden=!message;$('notice').classList.toggle('error',error);text('notice',message)}
function connection(label,status=''){$('connection').className='status '+status;text('connection-label',label)}
for(const[key,label,direction,code]of spec){const card=el('article','card maibot-card metric'),head=el('div','metrichead'),value=el('div','metricvalue','—'),meter=el('div','meter'),fill=el('i');card.style.setProperty('--mc','var('+colors[key]+')');head.append(el('h3','',label),el('span','metricicon',code));value.id='value-'+key;fill.id='fill-'+key;fill.style.background=`var(${colors[key]})`;meter.append(fill);const assessment=el('div','note','等待数据');assessment.id='assessment-'+key;assessment.title=direction;const trend=el('span','metrictrend');trend.id='trend-'+key;trend.hidden=true;const foot=el('div','metricfoot');foot.append(assessment,trend);card.append(head,value,meter,foot);$('metrics').append(card);const b=el('button'),dot=el('i','swatch');dot.style.background=`var(${colors[key]})`;b.setAttribute('aria-pressed','true');b.append(dot,document.createTextNode(label));b.onclick=()=>{selected.has(key)?selected.delete(key):selected.add(key);b.setAttribute('aria-pressed',String(selected.has(key)));drawHistory()};$('legend').append(b)}
// 趋势 chip：取当前序列最后两个真实观察点之差（百分点），估算点不参与；不足两点隐藏。
function trendFor(key){const rows=(lifeSeries().points||[]).filter(p=>!p.estimated&&num(p[key])!==null);if(rows.length<2)return null;const a=rows[rows.length-2],b=rows[rows.length-1];return {d:num(b[key])-num(a[key]),at:b.at}}
function assess(key,value){if(num(value)===null)return '状态暂不可用';const levels={energy:[[.15,'精疲力竭'],[.35,'有些疲惫'],[.65,'精力尚可'],[.85,'精力充沛'],[2,'活力十足']],hunger:[[.25,'不太饿'],[.5,'稍有饥饿'],[.7,'有些饿了'],[.85,'很饿'],[2,'需要进食']],focus:[[.15,'注意力涣散'],[.35,'难以集中'],[.6,'专注一般'],[.85,'专注良好'],[2,'高度专注']],stress:[[.15,'很放松'],[.35,'比较平静'],[.6,'有些紧绷'],[.8,'压力较高'],[2,'压力很高']],social_battery:[[.15,'更想独处'],[.35,'社交电量偏低'],[.65,'愿意交流'],[.85,'社交电量充足'],[2,'交流意愿很高']]};return levels[key].find(([limit])=>Math.max(0,Math.min(1,value))<limit)[1]}
function render(){const s=data.state||{},sch=data.schedule||{},cur=sch.current,co=data.companion||{};connection(imported?'离线快照':data.stale||data.warnings?.length?'已连接 · 留意数据':'实时连接',imported||data.stale||data.warnings?.length?'warn':'live');const updatedLabel=(imported?'快照时间 ':'更新于 ')+clock(data.generated_at,true)+' · '+(data.timezone||'');text('updated',updatedLabel);$('updated').title=updatedLabel;
text('current-kind',cur?(cur.goal_type==='sleep'?(data.sleep?.current?.asleep?'睡眠中':'准备入睡'):'进行中'):sch.enabled?(sch.runtime_ready?'空闲':'未就绪'):'未启用');text('current-name',cur?.name||(sch.enabled?(sch.runtime_ready?'此刻空闲':'生活日程正在准备'):'日程未启用'));const meta=$('current-meta');meta.replaceChildren();if(cur){
 // W-08 时间语义（2026-11-03）：计划结束时间消费后端 planned_end_minute（跨午夜由
 // spanEnd 补次日；后端 _row 另给 time_label 供详情层展示），进度基准注明「按本轮
 // 活动」，让 34% 之类的分母可解释——不去掩饰与日意图时段的差异。
 const planEnd=spanEnd(cur.actual_start_minute??cur.planned_start_minute,cur.planned_end_minute);
 meta.append(el('span','',minute(cur.actual_start_minute??cur.planned_start_minute)+' 开始'),el('span','',planEnd===null?'计划结束待定':'计划至 '+minute(planEnd)),el('span','',source(cur.source)),el('span','',(cur.goal_type==='sleep'?(data.sleep?.current?.asleep?'正在睡眠，适合稍后再聊':'准备休息'):({now:'可以聊一会儿',brief:'适合简短交流',later:'此刻更专注'}[cur.attention_mode]||'注意力未记录'))));}
else meta.append(el('span','',sch.enabled?'尚无正在进行的活动记录':'启用日程后查看真实活动'));const progress=cur?Math.max(0,Math.min(100,(num(cur.progress)??0)*100)):0;$('current-progress').firstElementChild.style.width=progress+'%';$('current-progress').setAttribute('aria-valuenow',String(Math.round(progress)));$('current-progress').setAttribute('aria-label','当前活动进度 · 按本轮活动计划时长计算');text('current-percent',cur?Math.round(progress)+'%':'—');$('current-percent').title='进度按本轮活动的计划时长与已用时计算；与时间线中的日意图时段可能不同';text('current-state',data.state_available?(data.stale?'状态快照已超过 15 分钟':'根据运行态更新'):'生活状态暂不可用');
text('tier',co.available?co.tier_label:'陪伴尚未就绪');text('score',co.available?fix(co.score):'—');text('baseline',co.available?'基线 '+fix(co.baseline):'等待陪伴数据');text('patrol',co.available?(co.patrol_running?'巡检运行中':'巡检未运行'):'未就绪');text('budget',co.available?(co.today_valid?`${count(co.speak_count)} / ${count(co.daily_limit)}`:'今日尚未滚动'):'—');$('budget').classList.toggle('over',!!(co.available&&co.today_valid&&num(co.speak_count)!==null&&num(co.daily_limit)!==null&&num(co.speak_count)>num(co.daily_limit)));for(const[key,id]of[['morning_sent','morning'],['night_sent','night']])text(id,co.available?(co.today_valid?(co[key]?'已发送':'尚未发送'):'尚无今日记录'):'—');
for(const[key]of spec){text('value-'+key,pct(s[key]));text('assessment-'+key,assess(key,s[key]));$('fill-'+key).style.width=(num(s[key])===null?0:Math.max(0,Math.min(1,s[key]))*100)+'%';const tr=trendFor(key),tEl=$('trend-'+key);if(tr&&Math.abs(tr.d)>=.005){const pp=Math.round(Math.abs(tr.d)*100);tEl.hidden=false;tEl.textContent=(tr.d>0?'↑ ':tr.d<0?'↓ ':'→ ')+pp;tEl.title='较上一真实采样 '+clock(tr.at,true)+' · '+labels[key]+'（百分点）'}else{tEl.hidden=true}}text('state-at','当前快照 · '+clock(s.updated_at));$('observations').replaceChildren(...(data.observations||[]).slice(0,5).map(o=>el('span','observation',o)));computePoints();renderActiveTab();$('export').disabled=false;if($('more-export'))$('more-export').disabled=false;renderExtras();updateFreshness()}
// W-15 按激活 tab 渲染（最小版调度器，2026-11-03）：30 秒刷新只更新全局轻量区域
// （render 里）+ 当前激活 tab 的重内容（图表/表格/时间线）；切换 tab（activateTab）、
// 窗口尺寸变化、详情层 redraw 时再渲染目标 tab。共享派生数据 points 由调用方先算。
function renderActiveTab(){
 if(activeTab==='history'){renderHistoryView();return}
 if(activeTab==='schedule'){renderSchedulePage();return}
 if(activeTab==='mood'){renderMood();return}
 if(activeTab==='system'){renderUsage();renderHealth();renderQzoneVideoChips();return}
 // PR-ENTERTAINMENT（3.17.13）：情绪卡片并入「此刻」（生活状态之后、睡眠之前），
 // now 路径渲染 renderEmotion；情绪数据投影与渲染逻辑本身零改动（仅展示位置迁移）。
 renderEmotion();renderSleep();
}
// PR-D1（H10）：系统板块「空间视频候选/资产/回执」计数芯片——只读投影 data.qzone_video
// （planning/dashboard._extension_summary），沿用既有 summarychips 组件风格，不重构页面；
// 未装配/不可用 → 整块隐藏（不造假 0）。计数口径与 ledger snapshot/assets_overview 一致。
function renderQzoneVideoChips(){const box=$('qzone-video-chips');if(!box)return;const qv=data?.qzone_video;
 if(!qv||!qv.available){box.hidden=true;box.replaceChildren();return}
 const sum=o=>Object.values(o||{}).reduce((a,b)=>a+(num(b)??0),0);
 const detail=o=>Object.entries(o||{}).map(([k,v])=>`${k} ${count(v)}`).join(' · ');
 const mb=num(qv.asset_total_bytes);
 const chips=[`空间视频候选 ${count(sum(qv.candidate_states))}${detail(qv.candidate_states)?'（'+detail(qv.candidate_states)+'）':''}`,
  `截图资产 ${count(sum(qv.asset_states))} 张 · ${mb===null?'—':(mb/1048576).toFixed(1)} MB`,
  `发布回执 ${count(sum(qv.publish_attempts))}${detail(qv.publish_attempts)?'（'+detail(qv.publish_attempts)+'）':''}`,
  `事实同步 ${detail(qv.fact_sync)||'暂无'}`];
 box.hidden=false;box.replaceChildren(...chips.map(s=>el('span','',s)))}
function dayView(){return data?.day?.available&&data.day.date===selectedDay?data.day:null}
function lifeSeries(){return selectedDay?(dayView()?.state_history||{}):(data?.history||{})}
function vadSeries(){return selectedDay?(dayView()?.vad_history||{}):(data?.emotion?.history||{})}
function dayRows(){const view=dayView();return view?{...(data?.schedule||{}),episodes:view.footprint?.episodes??view.footprint?.items??[],intents:view.intent?.items||[],summary:view.summary}:selectedDay?{enabled:!!data?.schedule?.enabled,episodes:[],intents:[],summary:{}}:(data?.schedule||{})}
// W-15 按激活 tab 渲染（最小版，2026-11-03）：points 是共享派生数据（睡眠迷你条、
// 详情层「最近变化」与采样表都消费），始终计算；但历史曲线与采样表只属于「历史」tab，
// 只在该 tab 激活时重建——隐藏 tab 不再每 30 秒按 1000px 兜底宽度重画 SVG。
function computePoints(){const all=Array.isArray(lifeSeries().points)?lifeSeries().points:[],end=date(data?.generated_at);points=all.filter(p=>date(p.at)&&(selectedDay||!end||(+date(p.at)>=+end-hours*3600000&&+date(p.at)<=+end)))}
function renderHistoryView(){
const real=points.filter(p=>!p.estimated).length,estimated=points.length-real;
text('history-caption',selectedDay?selectedDay+' · 按日查看真实记录与估算段':`最近 ${hours} 小时 · 每 5 分钟采样，另保留活动与醒沿观察`);
text('history-info',points.length?`${real} 个真实观察点 · ${estimated} 个估算点${lifeSeries().downsampled?' · 已抽样显示':''} · 估算不计入真实统计${points.some(p=>num(p.stress)===0)&&!points.some(p=>num(p.stress)>0)?' · 压力持续为 0，曲线贴在横轴上':''}`:'这段时间还没有真实记录；没有观测依据的部分保持空白');drawHistory();renderSamples()}
function svg(tag,attrs={},content){const e=document.createElementNS(NS,tag);for(const[k,v]of Object.entries(attrs))e.setAttribute(k,String(v));if(content!==undefined)e.textContent=String(content);return e}
function visiblePoints(){return zoom?points.filter(p=>+date(p.at)>=zoom[0]&&+date(p.at)<=zoom[1]):points}
function smoothPath(segment,step){if(!segment.length)return '';let d=`M${segment[0][0]} ${segment[0][1]}`;if(step){for(let i=1;i<segment.length;i++)d+=`H${segment[i][0]}V${segment[i][1]}`;return d}const slopes=segment.slice(1).map((p,i)=>(p[1]-segment[i][1])/Math.max(.000001,p[0]-segment[i][0])),m=segment.map((p,i)=>i===0?slopes[0]:i===segment.length-1?slopes.at(-1):slopes[i-1]*slopes[i]<=0?0:2/(1/slopes[i-1]+1/slopes[i]));for(let i=1;i<segment.length;i++){const a=segment[i-1],b=segment[i],dx=(b[0]-a[0])/3;d+=`C${a[0]+dx} ${a[1]+m[i-1]*dx} ${b[0]-dx} ${b[1]-m[i]*dx} ${b[0]} ${b[1]}`}return d}
function chart(target,rows,keys,max=1,height=260,min=0){
 target.replaceChildren();const day=dayView(),sleepSegments=day?.sleep?.estimated_segments||[];
 const dayBounds=selectedDay&&day? [Number(date(day.window_start)),Number(date(day.window_end))]:null;
 if(!rows.length&&!sleepSegments.length){empty(target,'还没有这段时间的记录','真实采样到来后，曲线会在这里慢慢展开。');return}
 if(!keys.length){empty(target,'曲线已隐藏','点击图例选择想看的状态。');return}
 const w=Math.max(280,Math.min(1100,target.clientWidth||1000)),l=38,r=18,top=22,bottom=52;
 const t0=zoom?.[0]??dayBounds?.[0]??Number(date(rows[0]?.at)),t1=zoom?.[1]??dayBounds?.[1]??Number(date(rows.at(-1)?.at));
 const span=Math.max(300000,t1-t0),x=t=>l+(t-t0)/span*(w-l-r),y=v=>height-bottom-Math.max(0,Math.min(1,(v-min)/(max-min)))*(height-top-bottom),step=document.documentElement.dataset.dashboardStyle==='millennium';
 const root=svg('svg',{viewBox:`0 0 ${w} ${height}`,role:'img',tabindex:'0','aria-label':min<0?'情绪 VAD 趋势，纵轴 −1 到 1':max===1?'生活状态趋势，纵轴百分比，拖动或双指缩放；按 Escape 重置':'睡眠债趋势，纵轴小时'});
 root.style.userSelect='none';root.append(svg('title',{},'实线为真实观察；虚线为中间缺口估算；斜纹为离线睡眠估算。拖动选择、双指缩放，Escape 或双击重置。'));
 const clipId=target.id+'-clip',hatchId=target.id+'-hatch',defs=svg('defs'),clip=svg('clipPath',{id:clipId});clip.append(svg('rect',{x:l,y:top,width:w-l-r,height:height-top-bottom}));defs.append(clip);
 const hatch=svg('pattern',{id:hatchId,patternUnits:'userSpaceOnUse',width:8,height:8,patternTransform:'rotate(45)'});hatch.append(svg('line',{x1:0,y1:0,x2:0,y2:8,stroke:'var(--focus)','stroke-width':2,opacity:.18}));defs.append(hatch);root.append(defs);
 const under=svg('g',{'clip-path':`url(#${clipId})`});
 // Background bands are descriptive, never added to the numerical samples.
 for(const segment of sleepSegments){const a=Math.max(t0,Number(date(segment.start))),b=Math.min(t1,Number(date(segment.end)));if(!(b>a))continue;const band=svg('rect',{x:x(a),y:top,width:x(b)-x(a),height:height-top-bottom,fill:`url(#${hatchId})`,'data-estimated-sleep':'true'});band.append(svg('title',{},'离线睡眠估算段 · 无连续采样，不计为真实观察点'));under.append(band)}
 let sleepStart=null;for(let i=0;i<=rows.length;i++){const p=rows[i],asleep=p&&(p.sleeping===true||p.frozen===true);if(asleep&&sleepStart===null)sleepStart=Number(date(p.at));if(!asleep&&sleepStart!==null){const end=Number(date(p?.at||rows[i-1].at));under.append(svg('rect',{x:x(sleepStart),y:top,width:Math.max(1,x(end)-x(sleepStart)),height:height-top-bottom,fill:'var(--focus)',opacity:.055}));sleepStart=null}}
 root.append(under);
 for(let i=0;i<=4;i++){const yy=y(min+(max-min)*i/4);root.append(svg('line',{x1:l,x2:w-r,y1:yy,y2:yy,stroke:'var(--maibot-border)','stroke-width':step?1.5:.8,'stroke-dasharray':step?'none':'3 3'}),svg('text',{x:l-9,y:yy+4,'text-anchor':'end',fill:'var(--maibot-muted)','font-size':11},min===0&&max===1?i*25:Number((min+(max-min)*i/4).toFixed(2))))}
 root.append(svg('line',{x1:l,x2:l,y1:top,y2:height-bottom,stroke:'var(--maibot-border)'}),svg('line',{x1:l,x2:w-r,y1:height-bottom,y2:height-bottom,stroke:'var(--maibot-border)'}));
 const ticks=w<600?4:7;for(let i=0;i<ticks;i++){const at=t0+span*i/(ticks-1),xx=x(at),yy=height-bottom+15;root.append(svg('text',{x:xx,y:yy,transform:`rotate(-35 ${xx} ${yy})`,'text-anchor':'end',fill:'var(--maibot-muted)','font-size':11},clock(new Date(at).toISOString(),!selectedDay&&hours>24)))}
 const curves=svg('g',{'clip-path':`url(#${clipId})`});
 const downsampled=(target.id==='vad-chart'||target.id==='emotion-history-chart')?vadSeries().downsampled:lifeSeries().downsampled;
 for(const key of keys){
  // 实线只连真实观察点、虚线只连估算填充点：抽稀后两者交错时，旧逻辑把任一端点
  // 为估算的边画成虚线，连真实观察也被画成虚线（历史按日首屏全虚线）。分开构建后
  // 真实观察恒为实线；估算填充按原始索引相邻成段，不跨真实区域连接不同缺口。
  const build=estimated=>{const paths=[];let segment=[],prev=null,prevIndex=-1;const flush=()=>{if(segment.length)paths.push(smoothPath(segment,step&&!estimated));segment=[]};
   for(let i=0;i<rows.length;i++){const p=rows[i];if(!!p.estimated!==estimated)continue;const at=Number(date(p.at));if(num(p[key])===null){flush();prev=null;prevIndex=-1;continue}
    if(prev){const separated=estimated?(i!==prevIndex+1):(p.gap_before||(!downsampled&&at-Number(date(prev.at))>900000));if(separated)flush()}
    segment.push([x(at),y(p[key])]);prev=p;prevIndex=i;
   }flush();return paths};
  const real=build(false),estimated=build(true);
  if(real.length)curves.append(svg('path',{d:real.join(''),fill:'none',stroke:`var(${colors[key]})`,'stroke-width':key==='stress'?3:(step?3:2),'stroke-linejoin':'round','stroke-linecap':'round','data-series':key}));
  if(estimated.length)curves.append(svg('path',{d:estimated.join(''),fill:'none',stroke:`var(${colors[key]})`,'stroke-width':2,'stroke-dasharray':'5 5',class:'estimated-line','data-series':key}));
  if(rows.length===1&&num(rows[0][key])!==null)curves.append(svg('circle',{cx:x(Number(date(rows[0].at))),cy:y(rows[0][key]),r:3,fill:`var(${colors[key]})`}))
 }
 root.append(curves);
 // A bounded set of actual episode boundaries, using their own calendar date.
 if(target.id==='history-chart')for(const ep of (dayRows().episodes||[]).slice(-24)){
  if(num(ep.actual_start_minute)===null)continue;const at=zonedMidnight(ep.schedule_date||selectedDay||data.schedule.date||dayText(data.generated_at))+ep.actual_start_minute*60000;if(at<t0||at>t1)continue;
  const xx=x(at),g=svg('g',{'data-activity-boundary':'true'});g.append(svg('line',{x1:xx,x2:xx,y1:top+12,y2:height-bottom,stroke:'var(--maibot-muted)','stroke-dasharray':'2 5',opacity:.25}),svg('text',{x:xx,y:top-7,'text-anchor':'middle','font-size':12,fill:'var(--maibot-muted)'},ep.goal_type==='sleep'?'☾':'▴'),svg('title',{},`${ep.name||'活动开始'} · ${minute(ep.actual_start_minute)}`));root.append(g)
 }
 const nowAt=Number(date(data.generated_at));if(nowAt>=t0&&nowAt<=t1){const xx=x(nowAt);root.append(svg('line',{x1:xx,x2:xx,y1:top,y2:height-bottom,stroke:'var(--maibot-primary)','stroke-width':1,'stroke-dasharray':'4 3','data-now':'true'}),svg('text',{x:Math.max(l+24,Math.min(w-r-12,xx-4)),y:top-7,'text-anchor':'end',fill:'var(--maibot-primary)','font-size':10},'当前快照'))}
 const cross=svg('line',{x1:l,x2:l,y1:top,y2:height-bottom,stroke:'var(--maibot-muted)','stroke-dasharray':'3 3',visibility:'hidden'}),brush=svg('rect',{x:l,y:top,width:0,height:height-top-bottom,fill:'var(--maibot-primary)',opacity:.12,visibility:'hidden'});root.append(cross,brush);target.append(root);
 const tip=el('div','tooltip');tip.hidden=true;target.append(tip);let drag=null;const pointers=new Map();let pinch=null;
 const coordinate=e=>{const b=root.getBoundingClientRect();return Math.max(l,Math.min(w-r,(e.clientX-b.left)/b.width*w))};
 function applyZoom(a,b){if(b-a<300000)return;zoom=[Math.max(t0,a),Math.min(t1,b)];$('zoom-reset').hidden=false;drawHistory()}
 root.onpointermove=e=>{const box=root.getBoundingClientRect(),pos=coordinate(e),at=t0+(pos-l)/(w-l-r)*span;if(pointers.has(e.pointerId))pointers.set(e.pointerId,pos);
  if(pinch&&pointers.size>=2){e.preventDefault();return}
  if(drag!==null){brush.setAttribute('x',Math.min(drag,pos));brush.setAttribute('width',Math.abs(pos-drag));brush.setAttribute('visibility','visible');return}
  if(!rows.length)return;const p=rows.reduce((best,item)=>Math.abs(Number(date(item.at))-at)<Math.abs(Number(date(best.at))-at)?item:best,rows[0]),xx=x(Number(date(p.at)));cross.setAttribute('x1',xx);cross.setAttribute('x2',xx);cross.setAttribute('visibility','visible');tip.replaceChildren(el('strong','',clock(p.at,true)+(p.estimated?' · 估算':' · 真实观察')),...keys.map(k=>el('div','',`${labels[k]} ${min<0?fix(p[k],2):max===1?pct(p[k])+'%':fix(p[k])+'h'}`)));tip.hidden=false;tip.style.left=Math.max(0,Math.min(box.width-190,e.clientX-box.left+12))+'px';tip.style.top='8px'
 };
 root.onpointerleave=()=>{tip.hidden=true;cross.setAttribute('visibility','hidden')};
 root.onpointerdown=e=>{if(e.button!==0&&e.pointerType!=='touch')return;pointers.set(e.pointerId,coordinate(e));root.setPointerCapture(e.pointerId);tip.hidden=true;if(pointers.size===2){const vals=[...pointers.values()];pinch={distance:Math.abs(vals[1]-vals[0]),middle:(vals[1]+vals[0])/2};drag=null}else if(e.pointerType!=='touch')drag=coordinate(e)};
 root.onpointerup=e=>{if(pinch&&pointers.size>=2){const vals=[...pointers.values()],factor=pinch.distance/Math.max(12,Math.abs(vals[1]-vals[0])),center=t0+(pinch.middle-l)/(w-l-r)*span;pointers.clear();pinch=null;applyZoom(center-span*factor/2,center+span*factor/2);return}
  pointers.delete(e.pointerId);if(drag===null)return;const end=coordinate(e),first=drag;drag=null;brush.setAttribute('visibility','hidden');if(Math.abs(end-first)>12)applyZoom(t0+(Math.min(first,end)-l)/(w-l-r)*span,t0+(Math.max(first,end)-l)/(w-l-r)*span)
 };
 root.onpointercancel=()=>{drag=null;pinch=null;pointers.clear();brush.setAttribute('visibility','hidden')};root.ondblclick=resetZoom;root.onkeydown=e=>{if(e.key==='Escape'){e.preventDefault();resetZoom()}};
}

// W-15：zoom 状态由 drawHistory 消费；睡眠债/VAD 图各自在所属 tab 激活时（activateTab
// → renderActiveTab）按最新 zoom 重画，不再在缩放路径里重建隐藏 tab 的图表。
function resetZoom(){zoom=null;$('zoom-reset').hidden=true;drawHistory()}
$('zoom-reset').onclick=resetZoom;
function drawHistory(){chart($('history-chart'),visiblePoints(),[...selected]);renderEmotionHistory()}
function renderSamples(){const wrap=$('samples');if(!points.length){empty(wrap,'暂无采样值','这里只显示实际记录。');return}
 // W-11：口径可见——表格固定展示最近 60 条，说明行写明当前筛选范围的总点数。
 const colh=t=>{const h=el('th','',t);h.setAttribute('scope','col');return h};
 const table=el('table','maibot-table'),head=el('tr'),thead=el('thead'),body=el('tbody');for(const label of['采样时间',...spec.map(s=>s[1]),'睡眠债','来源'])head.append(colh(label));thead.append(head);for(const p of points.slice(-60).reverse()){const row=el('tr');row.append(el('td','',clock(p.at,true)));for(const[k]of spec)row.append(el('td','',pct(p[k])));row.append(el('td','',fix(p.sleep_debt)+'h'),el('td','',(p.estimated?'估算 · 中间缺口':({start:'启动观察',tick:'生活采样',sleep:'睡眠采样',episode:'活动边界',wake:'醒来结算',sleep_settlement:'睡眠结算'}[p.reason]||'状态观察'))));body.append(row)}table.append(thead,body);wrap.replaceChildren(el('p','note',`显示最近 60 条 · 当前范围共 ${points.length} 点`),table)}
// 历史心情变化（PR-FIXED-CHROME 3.17.15，规格§1G）：VAD 曲线复用 chart()+vadSeries()
// （data.emotion.history.points，量程 −1..1），跟随历史页 range（hours）/zoom/selectedDay
// （与 history-chart 同口径，经 drawHistory 挂入既有重渲染链）。情绪不可用/旧快照无
// emotion.history → 空态「暂无心情记录」降级，不报错。此刻页 vad-chart 保留不动。
function emotionHistoryPoints(){const series=vadSeries(),all=Array.isArray(series.points)?series.points.filter(p=>date(p.at)):[],end=date(data?.generated_at);
 let rows=all.filter(p=>selectedDay||!end||(+date(p.at)>=+end-hours*3600000&&+date(p.at)<=+end));
 if(zoom)rows=rows.filter(p=>+date(p.at)>=zoom[0]&&+date(p.at)<=zoom[1]);return {rows,downsampled:series.downsampled}}
function renderEmotionHistory(){const target=$('emotion-history-chart');if(!target)return;const {rows,downsampled}=emotionHistoryPoints();
 if(!rows.length){empty(target,'暂无心情记录','情绪记录就绪后，愉悦/唤醒/掌控曲线会在这里展开。');text('emotion-history-info','暂无心情记录');renderEmotionSamples([]);return}
 chart(target,rows,['valence','arousal','dominance'],1,260,-1);
 text('emotion-history-info',`${rows.length} 个心情采样点${downsampled?' · 已抽样显示':''} · 量程 −1 到 1`);renderEmotionSamples(rows)}
function renderEmotionSamples(rows){const wrap=$('emotion-history-samples');if(!wrap)return;
 if(!rows.length){empty(wrap,'暂无心情采样','这里只显示实际记录。');return}
 const colh=t=>{const h=el('th','',t);h.setAttribute('scope','col');return h};
 const table=el('table','maibot-table'),head=el('tr'),thead=el('thead'),body=el('tbody');
 for(const label of['采样时间','愉悦','唤醒','掌控'])head.append(colh(label));thead.append(head);
 for(const p of rows.slice(-60).reverse()){const row=el('tr');row.append(el('td','',clock(p.at,true)),el('td','',fix(p.valence,2)),el('td','',fix(p.arousal,2)),el('td','',fix(p.dominance,2)));body.append(row)}
 table.append(thead,body);wrap.replaceChildren(el('p','note',`显示最近 60 条 · 当前范围共 ${rows.length} 点`),table)}
// 睡眠债迷你曲线（W-01 主卡微型预览，48–64px）：只呈现趋势、无数值轴（数值在三项统计里）；
// 全量交互曲线 #debt-chart 仍在详情层（D4：摘要+微型预览+详情全量），两者不共享 DOM。
function sparkDebt(target,rows){
 // 评审修正（2026-11-03）：只画真实观察点——估算点不与真实观察混成同一条实线
 // （§9「不把真实观察/估算合并」）；aria-hidden 装饰性趋势，数值与三态区分仍以
 // 详情层主图（实线/虚线/斜纹）为准。
 const pts=(Array.isArray(rows)?rows:[]).filter(p=>num(p.sleep_debt)!==null&&date(p.at)&&!p.estimated);
 if(pts.length<2){target.hidden=true;target.replaceChildren();return}
 target.hidden=false;
 const w=Math.max(280,Math.min(1100,target.clientWidth||640)),h=56,l=3,r=3,t=5,b=5;
 const t0=Number(date(pts[0].at)),t1=Number(date(pts.at(-1).at)),span=Math.max(60000,t1-t0);
 const x=at=>l+(Number(date(at))-t0)/span*(w-l-r),y=v=>h-b-Math.max(0,Math.min(1,v/12))*(h-t-b);
 let d='M'+x(pts[0].at).toFixed(1)+' '+y(pts[0].sleep_debt).toFixed(1);
 for(const p of pts.slice(1))d+='L'+x(p.at).toFixed(1)+' '+y(p.sleep_debt).toFixed(1);
 const g=svg('svg',{viewBox:`0 0 ${w} ${h}`,'aria-hidden':'true'});
 g.append(svg('path',{d,fill:'none',stroke:'var(--debt)','stroke-width':2,'stroke-linejoin':'round','stroke-linecap':'round'}));
 target.replaceChildren(g);
}
function renderSleep(){const s=data.state||{},sleep=data.sleep||{},cur=sleep.current;text('sleep-status',cur?(cur.asleep?(s.sleep_interrupted?'睡眠暂被打断':'正在睡眠'):'已醒来'):(s.sleep_started_at?'已记录睡眠段':'暂无进行中会话'));text('debt',fix(s.sleep_debt)+' h');text('continuous',fix(cur?.asleep?cur.continuous_minutes:(s.continuous_sleep_minutes??0),0));text('mental',pct(s.mental_state));
 const debtVal=num(s.sleep_debt),meterBox=$('debt-meter');if(meterBox){meterBox.hidden=debtVal===null;meterBox.classList.remove('sev-low','sev-mid','sev-high');if(debtVal!==null){meterBox.firstElementChild.style.width=Math.min(100,Math.max(0,debtVal/12*100))+'%';meterBox.classList.add(debtVal<=3?'sev-low':debtVal<=7?'sev-mid':'sev-high')}}
 const sparkBox=$('debt-spark');if(sparkBox)sparkDebt(sparkBox,points);
 const forecast=$('sleep-forecast');if(forecast){const wakeAt=cur?.asleep&&cur.wake_at?clock(cur.wake_at):'';forecast.hidden=!wakeAt;if(wakeAt)text('sleep-forecast','预计最早醒来 '+wakeAt)}
 chart($('debt-chart'),visiblePoints(),['sleep_debt'],12,160);text('sleep-anchors',`配置参考 ${sleep.configured_sleep||'—'} → ${sleep.configured_wake||'—'}${(sleep.dynamic?.window_start&&sleep.dynamic?.window_end)?' · 动态入睡窗口 '+clock(sleep.dynamic.window_start)+' → '+clock(sleep.dynamic.window_end):''}${cur?.asleep&&s.sleep_started_at?' · 实际入睡 '+clock(s.sleep_started_at):''}${cur?.wake_at?' · 最早醒沿 '+clock(cur.wake_at):''}${cur?.lie_in_cap?' · 赖床上限 '+clock(cur.lie_in_cap):''} · 醒睡以引擎会话为准`);const list=$('sleep-list'),rows=Array.isArray(sleep.sessions)?sleep.sessions:[];if(!rows.length){empty(list,'还没有近期睡眠会话','完整睡眠发生后会留下恢复与睡眠债记录。');return}list.replaceChildren();for(const row of rows){const box=el('div','sleeprow'),left=el('div'),right=el('div','rowend');
  // W-08：睡眠会话跨午夜（醒来分钟早于入睡分钟）时结束侧补「次日」。
  const endShown=spanEnd(row.actual_start_minute,row.actual_end_minute);
  left.append(el('strong','',`${row.schedule_date||'—'} · ${minute(row.actual_start_minute)} → ${minute(endShown)}`),el('div','note',row.actual_end_minute===null?'会话尚未结束':`醒来：${({energy_threshold:'精力达标',lie_in_cap:'赖床上限',hard_commitment:'硬约定唤醒',interrupted_awake:'被暂时打断',manual:'手动唤醒',no_sleep_mode:'免睡模式',offline_reconnect:'离线补眠估算'}[row.wake_reason]||row.wake_reason||'未记录')}`));right.append(el('strong','',num(row.energy_start)!==null&&num(row.energy_end)!==null?`精力 ${pct(row.energy_start)} → ${pct(row.energy_end)}`:row.actual_end_minute==null?'恢复待结算':'历史恢复数据缺失'),el('div','note',`睡眠债 ${fix(row.sleep_debt_before)} → ${fix(row.sleep_debt_after)}h`));box.append(left,right);list.append(box)}}
function source(v){return({spine:'日意图',random:'生活插曲',resume:'恢复活动',anchor:'生活锚点',idle:'自由活动',hard_commitment:'硬约定',rolling:'滚动安排',commitment:'硬约定'}[v]||v||'运行记录')}
function scheduleFootprint(rows, day){
 const sorted=[...rows].sort((a,b)=>(num(a.actual_start_minute)??num(a.planned_start_minute)??Infinity)-(num(b.actual_start_minute)??num(b.planned_start_minute)??Infinity));
 const result=[];
 const validStart=row=>Number.isInteger(row.actual_start_minute)&&row.actual_start_minute>=0&&row.actual_start_minute<1440;
 // 1A（3.17.8）：放宽到 <2880 让跨午夜 idle 条参与合并；合并仍要求同 schedule_date
 // （sameDay 判定），跨日续段 schedule_date=前一日，不会与今日行合并。
 const validEnd=row=>Number.isInteger(row.actual_end_minute)&&row.actual_end_minute>=row.actual_start_minute&&row.actual_end_minute<2880;
 for(const row of sorted){
  const run=result.at(-1), prev=run?.idleSegments?.at(-1)??run;
  const gap=prev&&validEnd(prev)&&validStart(row)?row.actual_start_minute-prev.actual_end_minute:null;
  const sameDay=prev&&(prev.schedule_date||day)===(row.schedule_date||day)&&(row.schedule_date||day)===day;
  if(sameDay&&prev.goal_type==='idle'&&row.goal_type==='idle'&&prev.name===row.name&&prev.source===row.source&&
     prev.status==='completed'&&validStart(prev)&&gap!==null&&gap>=0&&gap<=5&&
     (row.status==='completed'&&validEnd(row)||row.status==='active'&&row.actual_end_minute==null)){
   const segments=run.idleSegments||[run];
   segments.push(row);
   result[result.length-1]={...run,status:row.status,actual_end_minute:row.actual_end_minute,
     state_before:null,state_after:null,idleSegments:segments,idleGap:(run.idleGap||0)+gap};
  }else result.push(row);
 }
 return result;
}
function idleRunDetails(row,open){
 const segments=row.idleSegments;
 const key=[row.schedule_date,row.name,row.source,row.actual_start_minute].join('|');
 const details=el('details','idle-run'),list=el('ol','idle-segments');
 details.dataset.idleRun=key;details.open=open.has(key);
 details.append(el('summary','',segments.length+' 段待机'+(row.idleGap?' · 间隔共 '+row.idleGap+' 分钟':'')));
 for(const [i,segment] of segments.entries()){
  const gap=i?segment.actual_start_minute-segments[i-1].actual_end_minute:0;
  if(gap>0)list.append(el('li','note','间隔 '+gap+' 分钟，暂无活动记录'));
  list.append(el('li','',minute(segment.actual_start_minute)+' → '+(segment.status==='active'?'此刻':minute(segment.actual_end_minute))));
 }
 details.append(list);
 return details;
}
function openedIdleRuns(root){
 return new Set([...root.querySelectorAll('details[data-idle-run][open]')].map(e=>e.dataset.idleRun));
}
const episodeLabel=status=>({active:'进行中',completed:'已完成',paused:'暂停',interrupted:'已中断',abandoned:'已放下',skipped:'已跳过',planned:'未开始'}[status]||status||'已记录');
const schedulePages=[
 {key:'record',label:'行程记录'},
 {key:'events',label:'小事件',note:'内置与可复用事件按间隔参与；一次性事件抽中即移除。这是当前生成周期的池子，不随查看日期回退。'},
];
let schedulePage=0,schedulePageAnimating=false;
function schedulePageInfo(){return schedulePages[schedulePage]}
function renderSchedulePool(key){
 const pools=data?.schedule?.pools,items=Array.isArray(pools?.[key])?pools[key]:[];
 const target=$('schedule-'+key+'-items'),origins=$('schedule-'+key+'-sources');
 target.replaceChildren();origins?.replaceChildren();
 if(key!=='recovery')text('schedule-'+key+'-count',!Array.isArray(pools?.[key])?'未提供':
  key==='one_shot'?'剩余 '+items.length+' / '+(num(pools.one_shot_total)??items.length)+' 项':items.length+' 项');
 for(const item of items){
  const row=el('li','schedule-pool-row'),name=item.name||'未命名条目';
  const min=num(item.min_minutes),max=num(item.max_minutes);
  const period=key==='recovery'?
   [item.schedule_date,num(item.progress)===null?'':'完成 '+pct(item.progress)+'%'].filter(Boolean).join(' · '):
   min===null?'时长未记录':max===null||min===max?min+' 分钟':min+'–'+max+' 分钟';
  row.append(el('strong','',name));
  // 3.17.7 改进2：右侧时段 badge（生成事件 time_slot；内置全时段=全天；旧快照无字段不显示）
  const end=el('span','poolrowend');
  if(period)end.append(el('span','note',period));
  if(key!=='recovery'&&item.time_slot)end.append(el('span','rowbadge slotbadge',item.time_slot));
  if(end.childNodes.length)row.append(end);
  target.append(row);
  if(origins){
   const origin=el('li','schedule-pool-row');
   const label=key==='builtin'?'插件内置事件库':key==='one_shot'?'模型生成 · 一次性小目标':'模型生成 · 周期内可复用';
   const source=el('span','note',label);
   if(key!=='builtin')source.append(document.createTextNode(
    (date(pools.generated_at)?' · 生成于 '+clock(pools.generated_at,true):' · 生成时间未记录')+
    (date(pools.next_update_at)?' · 下次更新 '+clock(pools.next_update_at,true):'')));
   source.title=key==='builtin'?'随插件提供的日常小事件':
    '生成时间：'+fullClock(pools.generated_at)+'；下次更新：'+fullClock(pools.next_update_at)+
    (key==='one_shot'?'；最终抽中即消耗，启动失败或被打断也不返池。':'；遵守原有去重间隔，可在周期内再次发生。');
   origin.append(el('strong','',name),source);
   origins.append(origin);
  }
 }
 if(!items.length){
  const message=!data?.schedule?.enabled?'日程未启用':!Array.isArray(pools?.[key])?'当前快照未包含此池':
   key==='recovery'?'暂无待恢复活动':'这个池子暂无条目';
  target.append(el('li','note',message));origins?.append(el('li','note',message));
 }
}
function renderConsumedEvents(){
 const target=$('schedule-consumed-items'),rows=data?.schedule?.pools?.consumed;
 if(!target)return;
 target.replaceChildren();
 for(const item of Array.isArray(rows)?rows:[]){
  const row=el('li','schedule-pool-row');
  const status=({selected:'已抽中',started:'已开始',start_failed:'启动未成功',interrupted:'已中断',completed:'已完成'})[item.consumption_status]||'已消耗';
  const note=el('span','note',status+' · '+(date(item.consumed_at)?clock(item.consumed_at,true):'时间未记录'));
  note.title=fullClock(item.consumed_at)+'；本周期不会再抽到这条事件。';
  const end=el('span','poolrowend');end.append(note);
  if(item.time_slot)end.append(el('span','rowbadge slotbadge',item.time_slot));
  row.append(el('strong','',item.name||'未命名条目'),end);target.append(row);
 }
 if(!target.children.length)target.append(el('li','note',Array.isArray(rows)?'本周期暂无消耗记录':'当前快照未包含消耗记录'));
}
function renderSchedulePage(){
 // 3.17.10 PR-OVERVIEW-FULLWIDTH：横条置顶常驻（翻页内容之外），两页均渲染；数据源仍 dayRows()
 if(data)renderActivityOverview(dayRows());
 const page=schedulePageInfo(),record=page.key==='record';
 $('schedule-record').hidden=!record;$('schedule-pool').hidden=record;
 $('schedule-detail').hidden=!record;$('schedule-pool-detail').hidden=record;
 $('schedule-prev').disabled=record;$('schedule-next').disabled=!record;
 text('schedule-page-label',page.label);text('schedule-page-index',String(schedulePage+1)+' / '+schedulePages.length);
 const entry=$('fish-schedule');
 if(entry){entry.setAttribute('aria-label','查看'+page.label+'详情 · 麦麦');entry.title='查看'+page.label+'详情 · 麦麦'}
 if(record){
  if(data){renderSchedule();renderTimeline()}
  renderSchedulePool('recovery');
 }else{
  renderSchedulePool('builtin');renderSchedulePool('generated');renderSchedulePool('one_shot');renderConsumedEvents();
 }
 SectionFish.refresh();
}
async function scheduleFade(className){
 const content=$('schedule-page-content');
 content.classList.add(className);
 try{await content.getAnimations()[0]?.finished}
 catch(error){if(error.name!=='AbortError')throw error}
 finally{content.classList.remove(className)}
}
async function moveSchedulePage(step){
 const next=schedulePage+step;
 if(next<0||next>=schedulePages.length||schedulePageAnimating)return;
 schedulePageAnimating=true;
 const motion=!matchMedia('(prefers-reduced-motion: reduce)').matches;
 const oldControl=$(step>0?'schedule-next':'schedule-prev');
 const hadFocus=document.activeElement===oldControl;
 SectionFish.close(false);
 try{
  if(motion)await scheduleFade('schedule-fade-out');
  schedulePage=next;
  renderSchedulePage();
  if(hadFocus&&oldControl.disabled)$(step>0?'schedule-prev':'schedule-next').focus({preventScroll:true});
  if(motion)await scheduleFade('schedule-fade-in');
 }finally{schedulePageAnimating=false}
}
$('schedule-prev').onclick=()=>moveSchedulePage(-1);
$('schedule-next').onclick=()=>moveSchedulePage(1);
// ── 1B 当日活动概括横条（PR-WEBUI-TIMELINE，3.17.8）──────────────────────
// 纯展示：固定 24h（容器宽度恒定代表 1440 分钟，不按活动量缩放）；类型固定色
// （写死常量，确定性映射，不随机）；未滚动计划段=意图区间对当日 episode 区间求
// 差集后染计划灰；真实空闲如实留白不伪造；不触发任何调度/刷新副作用。
const OVERVIEW_COLORS={sleep:'#5b6ee1',rest:'#8a9df0',idle:'#9aa5b1',meal:'#e8a13c',work:'#3d8bfd',exercise:'#2fbf71',entertainment:'#e05fa0',daily_routine:'#4fb8c9',social:'#8f6fe8',free_time:'#67c98f',watch:'#c9a227',game:'#ef7d3a',leisure:'#58b89a',social_maintenance:'#b085e0',activity:'#4a90d9',transition:'#8a8f98'};
// 中文旧值归一（生产历史脏值）；未知 goal_type 走兜底固定色。
const OVERVIEW_ALIASES={'放松':'rest','休息':'rest','娱乐':'entertainment','日常吃饭':'meal'};
const OVERVIEW_FALLBACK='#7a8288';
const overviewColor=goal=>OVERVIEW_COLORS[OVERVIEW_ALIASES[goal]||goal]||OVERVIEW_FALLBACK;
// 「此刻」分钟：data.generated_at 在快照时区的当日分钟（钳制 ≤1440）；active
// episode end=None 时渲染到此刻；无法解析 → null（该段不渲染，不伪造）。
function snapshotMinute(){const d=date(data?.generated_at);if(!d)return null;try{const parts=new Intl.DateTimeFormat('en-GB',{timeZone:data?.timezone||'Asia/Shanghai',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(d);const h=Number(parts.find(p=>p.type==='hour').value),m=Number(parts.find(p=>p.type==='minute').value);return Number.isFinite(h)&&Number.isFinite(m)?Math.max(0,Math.min(1440,h*60+m)):null}catch{return null}}
// 区间差集：[a,b] 减去若干覆盖区间（覆盖区间可重叠/乱序），返回剩余子区间。
function overviewUncovered(a,b,covered){let parts=[[a,b]];for(const[ca,cb]of covered){const next=[];for(const[pa,pb]of parts){if(cb<=pa||ca>=pb){next.push([pa,pb]);continue}if(ca>pa)next.push([pa,ca]);if(cb<pb)next.push([cb,pb])}parts=next;if(!parts.length)break}return parts}
let overviewTip=null,overviewTipOwner=null;
function overviewHideTip(){if(overviewTip)overviewTip.hidden=true;overviewTipOwner=null}
// 浮窗跟随段位、不出视口（position:fixed，独立于 infoTip 实现）。
function overviewPlaceTip(node){
 const r=node.getBoundingClientRect(),width=Math.min(280,innerWidth-24);
 overviewTip.style.width=width+'px';
 const height=overviewTip.getBoundingClientRect().height;
 overviewTip.style.left=Math.max(12,Math.min(innerWidth-width-12,r.left+r.width/2-width/2))+'px';
 let top=r.bottom+8;if(top+height>innerHeight-12)top=Math.max(12,r.top-height-8);
 overviewTip.style.top=top+'px';
}
function overviewShowTip(node,lines){
 if(!overviewTip){overviewTip=el('div','overview-tooltip');overviewTip.setAttribute('role','tooltip');document.body.append(overviewTip)}
 overviewTip.replaceChildren(...lines.map(line=>el('div','',line)));
 overviewTip.hidden=false;overviewTipOwner=node;
 overviewPlaceTip(node);
}
function overviewTipLines(seg){
 const row=seg.row;
 if(seg.kind==='planned')return[row.name||'未命名意图',minute(seg.a)+' → '+minute(seg.b),'计划 · 未滚动'];
 const start=num(row.actual_start_minute)??num(row.planned_start_minute);
 const rawEnd=num(row.actual_end_minute);
 const endText=row.status==='active'&&rawEnd===null?'此刻':minute(spanEnd(start,rawEnd??num(row.planned_end_minute)));
 const lines=[row.name||'未命名活动',minute(start)+' → '+endText,source(row.source)+' · '+episodeLabel(row.status)];
 if(row.cross_day_continuation)lines.push('跨日续段 · 原属 '+(row.schedule_date||'前一日'));
 else if(row.crosses_midnight)lines.push('跨日');
 return lines;
}
function renderActivityOverview(sch){
 const box=$('activity-overview');if(!box)return;
 overviewHideTip();
 const day=selectedDay||sch.date||dayText(data?.generated_at);
 text('overview-date',day);
 const track=$('overview-track'),note=$('overview-note');
 track.replaceChildren();
 const episodes=Array.isArray(sch.episodes)?sch.episodes:[],intents=Array.isArray(sch.intents)?sch.intents:[];
 const clamp=v=>Math.max(0,Math.min(1440,v)),nowMinute=snapshotMinute();
 const segs=[];
 for(const row of episodes){
  const start=num(row.actual_start_minute)??num(row.planned_start_minute);
  if(start===null)continue;
  let end=num(row.actual_end_minute);
  if(end===null&&row.status==='active')end=nowMinute;
  if(end===null)end=num(row.planned_end_minute);
  if(end===null)continue;
  end=spanEnd(start,end)??end;
  // 跨午夜原行当日段截到 1440；续段后端已投影为 0 起。left/width 恒按 1440 比例。
  const a=clamp(start),b=clamp(end);
  if(b>a)segs.push({a,b,row,kind:'episode'});
 }
 const covered=segs.map(seg=>[seg.a,seg.b]);
 for(const row of intents){
  const start=num(row.start_minute),rawEnd=num(row.end_minute);
  if(start===null||rawEnd===null)continue;
  const end=spanEnd(start,rawEnd)??rawEnd,a=clamp(start),b=clamp(end);
  if(b<=a)continue;
  for(const[pa,pb]of overviewUncovered(a,b,covered))segs.push({a:pa,b:pb,row,kind:'planned'});
 }
 // 计划灰在底层、实际色段覆盖其上；同层按 start 排序（重叠续段与今日行正常并列，不去重伪造）。
 segs.sort((x,y)=>(x.kind==='planned'?0:1)-(y.kind==='planned'?0:1)||x.a-y.a);
 for(const seg of segs){
  const node=el('button','overview-seg '+seg.kind);node.type='button';
  node.style.left=(seg.a/1440*100)+'%';
  node.style.width=((seg.b-seg.a)/1440*100)+'%';
  node.style.background=seg.kind==='planned'?'var(--overview-planned)':overviewColor(seg.row.goal_type);
  const lines=overviewTipLines(seg);
  node.setAttribute('aria-label',lines.join(' · '));
  node.onmouseenter=()=>overviewShowTip(node,lines);
  node.onmouseleave=overviewHideTip;
  node.onfocus=()=>overviewShowTip(node,lines);
  node.onblur=overviewHideTip;
  // 移动端点按切换；桌面点击同样可固定/收起浮窗。沿用 infoTip 的 pointerdown 快照
  // 口径：触屏 tap 会先派发兼容 mouseenter（已把浮窗弹出），若 click 再按「当前是否
  // 打开」翻转会把第一次点按立刻收掉——故以 pointerdown 时刻的开关状态为准。
  let pointerWasOpen=false;
  node.onpointerdown=()=>{pointerWasOpen=Boolean(overviewTip&&!overviewTip.hidden&&overviewTipOwner===node)};
  node.onclick=e=>{e.stopPropagation();pointerWasOpen?overviewHideTip():overviewShowTip(node,lines);pointerWasOpen=false};
  node.onkeydown=e=>{if(e.key==='Escape'){overviewHideTip();node.blur()}};
  track.append(node);
 }
 track.classList.toggle('overview-empty',!segs.length);
 text('overview-note',segs.length?'':'暂无活动记录');
}
document.addEventListener('click',e=>{if(overviewTipOwner&&!overviewTipOwner.contains(e.target))overviewHideTip()});
// 滚动时浮窗跟随段位重新定位（而非直接隐藏——hover 触发的伴随滚动会把刚弹出的
// 浮窗立刻收掉）；段位已随重渲染移除则收起。
window.addEventListener('scroll',()=>{if(overviewTipOwner&&overviewTip&&!overviewTip.hidden){overviewTipOwner.isConnected?overviewPlaceTip(overviewTipOwner):overviewHideTip()}},true);
window.addEventListener('resize',overviewHideTip);
function renderSchedule(){
 const sch=dayRows(),episodes=Array.isArray(sch.episodes)?sch.episodes:[],intents=Array.isArray(sch.intents)?sch.intents:[],summary=sch.summary||{};
 const day=selectedDay||sch.date||dayText(data?.generated_at);
 const counts=[['completed_count','已完成'],['interrupted_count','已中断']].map(([k,l])=>l+' '+(summary[k]??'—'));
 text('episode-count',episodes.length+' 项记录');text('intent-count',intents.length+' 项意图');
 $('day-summary').replaceChildren(...counts.map(label=>el('span','',label)));
 text('schedule-detail-summary',day+' · '+episodes.length+' 项活动记录 · '+counts.join(' · '));
 function rows(target,list,actual){
  if(!list.length){
   const label=actual?(selectedDay?'这一天没有活动足迹':'今天还没有活动足迹'):(dayView()?.intent?.plans_available===false?'计划已超出保留期':selectedDay?'这一天没有计划记录':'今天的意图尚未生成');
   empty(target,sch.enabled?label:'日程未启用',actual?'实际发生后再留下记录。':'计划与实际活动分别展示。');return;
  }
  const open=openedIdleRuns(target);target.replaceChildren();
  for(const row of list){
   const active=actual&&row.status==='active';
   const start=actual?(row.actual_start_minute??row.planned_start_minute):row.start_minute;
   const end=actual?(row.actual_end_minute??(active?null:row.planned_end_minute)):row.end_minute;
   const box=el('div','activityrow'+(active?' active':'')),center=el('div');
   const separator=row.idleGap?' 至 ':' → ';
   center.append(el('h3','',row.name||'未命名活动'),el('div','note',minute(start)+separator+(active?'此刻':minute(spanEnd(start,end)))+(actual?' · '+source(row.source):'')));
   if(row.idleSegments)center.append(idleRunDetails(row,open));
   if(actual&&row.state_before&&row.state_after){
    const changes=spec.filter(([k])=>num(row.state_before[k])!==null&&num(row.state_after[k])!==null&&Math.abs(row.state_after[k]-row.state_before[k])>=.01).slice(0,3).map(([k,l])=>l+' '+pct(row.state_before[k])+'→'+pct(row.state_after[k]));
    if(changes.length)center.append(el('div','statechange',changes.join(' · ')));
   }
   // 3.17.7 改进1：行程记录右侧小号灰色抽取原因（仅实际 episode 带该字段时显示）
   const rowend=el('div','rowendcol');
   if(actual&&row.draw_reason_zh)rowend.append(el('span','drawreason',row.draw_reason_zh));
   rowend.append(el('span','rowbadge',actual?episodeLabel(row.status):(row.goal_type==='sleep'?'作息参考':row.hardness==='hard'?'硬约定':'意图')));
   // 1A（3.17.8）：跨日续段（前日溢出投影，00:00 起）/跨午夜原行（结束保留「次日」口径）标注。
   if(actual&&row.cross_day_continuation)rowend.append(el('span','rowbadge crossdaybadge','跨日续段'));
   else if(actual&&row.crosses_midnight)rowend.append(el('span','rowbadge crossdaybadge','跨日'));
   box.append(el('i','trackdot'),center,rowend);target.append(box);
  }
 }
 rows($('episodes'),scheduleFootprint(episodes,day).reverse(),true);
 rows($('intents'),intents,false);
}
function renderHealth(){const h=data.history||{},s=data.schedule||{},health=data.health||{},list=$('health-data'),pairs=[['数据时间',fullClock(data.generated_at)],['数据时区',data.timezone],['运行态',s.enabled?(s.runtime_ready?'日程启用 · 运行态就绪':'运行态暂不可用'):'日程未启用'],['状态新鲜度',num(data.state_age_seconds)===null?'未记录':Math.round(data.state_age_seconds/60)+' 分钟前'],['历史持久化',h.writable===false?'未知版本，保留原文件并暂停写入':h.persisted===false?'暂未落盘，内存观察继续':h.points?.length?'已原子落盘':'等待首次采样'],['历史保留','最多 7 天 / 2304 点；达到上限淘汰最旧点'],['历史起点',h.started_at?fullClock(h.started_at):'尚无记录'],['曲线口径','真实观察点；长缺口断线；饥饿、压力原值展示'],['陪伴预算','巡检与想念计入上限；早晚安记录单独展示'],['显示范围',data.truncated?'达到快照大小上限，部分旧记录已裁剪':'今日意图最多 80 条，实际足迹最多 120 条'],['刷新方式',live?'每 30 秒读取；页面隐藏暂停；失败自动退避':'离线快照，不自动更新']];
 // W-17（2026-11-03）：错误计数带「最近一次时间与归属」（后端只增 last_error_at/
 // last_error_scope），并指路宿主日志页——本页只给时间与范围，不展示原始日志正文。
 const pluginErr=count(health.plugin_errors),externalErr=count(health.external_errors);
 pairs.push(['插件错误（最近日志片段）',pluginErr==='—'?'—':pluginErr+' 条'+(health.last_error_at&&health.last_error_scope==='plugin'?' · 最近一次 '+clock(health.last_error_at,true):''),'只统计有界日志尾片段内的 error/critical 行；原始日志请到宿主「日志」页查看，本页不展示日志正文。'],['其他模块错误（最近日志片段）',externalErr==='—'?'—':externalErr+' 条'+(health.last_error_at&&health.last_error_scope==='external'?' · 最近一次 '+clock(health.last_error_at,true):''),'宿主或其他插件产生的错误，与本插件分开统计。']);
 // W-17：带第三位说明（口径/指路）的行在 dt 里挂 i 型说明 tooltip——与用量指标同款
 // 交互（infoTip 已适配详情面板内的定位），不把日志正文写进页面。
 list.replaceChildren();for(const[k,v,tip]of pairs){const dtCell=el('dt','',k);if(tip)dtCell.append(infoTip(tip));list.append(dtCell,el('dd','',v??'—'))}const warnings=Array.isArray(data.warnings)?data.warnings:[];text('warnings',warnings.join('；'));text('health-summary',warnings.length?`数据与运行说明 · ${warnings.length} 项需留意`:'数据与运行说明');
 // W-01/W-02：主卡一行可行动摘要（有无提示、数据新旧、下一步去哪），完整来源与警告在详情层。
 // W-17：摘要补错误计数与最近时间，让「错误 1」有下一步（去宿主日志页排查）。
 const errBits=[];
 if(num(health.plugin_errors)>0)errBits.push(`本插件错误 ${count(health.plugin_errors)}${health.last_error_scope==='plugin'&&health.last_error_at?'（最近 '+clock(health.last_error_at,true)+'）':''}`);
 if(num(health.external_errors)>0)errBits.push(`其他模块错误 ${count(health.external_errors)}`);
 text('health-brief',(warnings.length?`数据可用 · ${warnings.length} 项提示`:'数据正常')+' · 最近更新 '+(data.generated_at?clock(data.generated_at,true):'—')+(errBits.length?' · '+errBits.join(' · '):'')+' · 来源、新鲜度与警告点左下角麦麦查看')}
function validDay(value){return typeof value==='string'&&/^\d{4}-\d{2}-\d{2}$/.test(value)&&Number.isFinite(Date.parse(value))&&new Date(value).toISOString().slice(0,10)===value}
function validateSnapshot(v){const obj=x=>x&&typeof x==='object'&&!Array.isArray(x),rows=(x,max)=>Array.isArray(x)&&x.length<=max&&x.every(obj);if(!obj(v)||v.schema_version!==1||!obj(v.state)||!obj(v.schedule)||!obj(v.history)||!rows(v.history.points,2304)||!rows(v.schedule.episodes,120)||!rows(v.schedule.intents,80)||!(v.schedule.current===null||obj(v.schedule.current))||!obj(v.sleep)||!rows(v.sleep.sessions,8)||!Array.isArray(v.observations)||!v.observations.every(x=>typeof x==='string')||!Array.isArray(v.warnings)||!v.warnings.every(x=>typeof x==='string'))throw Error('文件不是受支持的生活面板快照');if(v.day?.available&&(!obj(v.day)||!validDay(v.day.date)||!date(v.day.window_start)||!date(v.day.window_end)||+date(v.day.window_end)<=+date(v.day.window_start)||!rows(v.day.footprint?.episodes??v.day.footprint?.items,120)||!rows(v.day.intent?.items,80)||!rows(v.day.state_history?.points,288)||!rows(v.day.vad_history?.points,288)||!rows(v.day.sleep?.sessions,8)||!rows(v.day.sleep?.estimated_segments,16)))throw Error('按日快照结构或记录数量不受支持');if(v.qzone?.delivery?.recent!==undefined&&!rows(v.qzone.delivery.recent,40))throw Error('发送回执快照结构不受支持');if(v.video!==undefined&&(!obj(v.video)||!rows(v.video.recent??[],10)||!rows(v.video.interests??[],8)))throw Error('视频信息快照结构不受支持');return v}
// W-15（2026-11-03）：原 renderModules 一次重建「心情与分享」+「系统」两个 tab 的
// 内容、VAD 图与用量矩阵表；拆成 renderMood/renderUsage，各自只在所属 tab 激活时
// 执行（由 renderActiveTab 调度），隐藏 tab 不再每 30 秒重建表格与 SVG。
// PR-ENTERTAINMENT（3.17.13）：情绪渲染抽为 renderEmotion（「此刻」路径调用，逻辑
// 逐字保留）；renderMood 重做为「娱乐」=最近观看+兴趣话题（只读投影 data.video，
// 缺失/旧快照降级空态）+空间与分享（qzone 原逻辑保留）。
function renderEmotion(){const emotion=data.emotion||{};text('emotion-label',emotion.available?(emotion.label||'心情平稳'):'情绪暂不可用');text('emotion-frozen',emotion.available?(emotion.frozen?'睡眠冻结中':'自然变化中'):'尚未就绪');const vad=emotion.effective||{};$('vad-values').replaceChildren(...[['valence','愉悦'],['arousal','唤醒'],['dominance','掌控']].map(([k,label])=>{const box=el('div');box.append(el('span','note',label),el('strong','',fix(vad[k],2)));return box}));
 // W-01 摘要回填：VAD 三细条（主卡微型预览，−1..1 映射到条长）；完整 VAD 曲线在详情层。
 const vadBars=$('vad-bars');if(vadBars)vadBars.replaceChildren(...[['valence','愉悦','--energy'],['arousal','唤醒','--focus'],['dominance','掌控','--stress']].map(([k,label,c])=>{const v=num(vad[k]),row=el('div','vadbar'),bar=el('div','meter'),fill=el('i');bar.setAttribute('aria-hidden','true');fill.style.background=`var(${c})`;fill.style.width=v===null?'0%':String(Math.max(0,Math.min(1,(v+1)/2))*100)+'%';bar.append(fill);row.append(el('span','note',label),bar,el('strong','',fix(v,2)));return row}));let vadPoints=Array.isArray(vadSeries().points)?vadSeries().points.filter(p=>date(p.at)):[];if(zoom)vadPoints=vadPoints.filter(p=>+date(p.at)>=zoom[0]&&+date(p.at)<=zoom[1]);chart($('vad-chart'),vadPoints,['valence','arousal','dominance'],1,230,-1);const referee=emotion.referee||{};text('referee-health',emotion.available?`睡眠裁判 ${referee.task||'—'} · 连续失败 ${referee.attempts??0} 次 · ${referee.abandoned?'本周期已放弃':referee.cached?'结果已缓存，等待醒沿':referee.error_class?'上次未获得有效结构化结果':'等待自然睡眠窗'}${referee.last_success_at?' · 最近成功 '+clock(referee.last_success_at,true):''}`:'等待裁判状态')}
// 娱乐面板（3.17.13）：最近观看=真实看完且文字记忆仍有效的视频（dashboard video.recent，
// ≤10 条；watched_at 为 epoch 秒，clock 按毫秒解析故 ×1000）；兴趣话题=兴趣文档最终权重
// top8（persona_base+watch_score 口径，只读不触发刷新）。服务缺失/旧快照无 video 键 →
// 空态降级，不造假 0；标题/BV 均为公开视频信息，不含私聊正文与路径。
function renderMood(){const v=data.video||{};
 const videoRecentBox=$('video-recent'),recent=Array.isArray(v.recent)?v.recent.slice(0,10):[];
 text('video-recent-count',v.available?recent.length+' 条':'—');
 if(videoRecentBox){if(recent.length){videoRecentBox.replaceChildren(...recent.map(row=>{const box=el('div','sleeprow videorrow'),info=el('div');info.append(el('strong','',row.title||'标题未记录'),el('div','note',row.bvid||''));box.append(info,el('span','note',num(row.watched_at)!==null?clock(row.watched_at*1000,true):'—'));return box}))}else empty(videoRecentBox,v.available?'最近没有看完的视频':'视频信息暂不可用',v.available?'看完的视频会在这里留下记录。':'视频观看模块就绪后展示。')}
 const interestBox=$('video-interests'),interests=Array.isArray(v.interests)?v.interests.slice(0,8):[];
 if(interestBox){if(interests.length){const max=Math.max(...interests.map(row=>num(row.weight)??0),.001);interestBox.replaceChildren(...interests.map(row=>{const box=el('div','interestrow'),head=el('div','interesthead'),w=num(row.weight),bar=el('div','meter'),fill=el('i');fill.style.background='var(--focus)';fill.style.width=(w===null?0:Math.max(0,Math.min(1,w/max))*100)+'%';bar.append(fill);head.append(el('strong','',row.topic||'未知话题'),el('span','note',fix(w,2)));box.append(head,bar);return box}))}else empty(interestBox,'暂无兴趣话题',v.available?'兴趣文档生成后展示。':'视频观看模块就绪后展示。')}
 const q=data.qzone||{};text('qzone-enabled',q.available?(q.enabled?'已启用':'未启用'):'未就绪');const cookie=q.cookie_health||{},cookieLabels={not_checked:'尚未检查登录状态',not_logged_in:'尚未登录空间',expired:'登录凭据过期或不完整',adapter_unavailable:'适配器暂不可用',network_error:'网络暂不可用',ready:'登录凭据已获取',cached:'使用本地缓存，尚未确认'};
 // PR-FIXED-CHROME（3.17.15）：头部只保留 enabled/posts_today 额度芯片与登录状态；
 // 发送回执/草稿预览类展示已移除，右列改显最近动态（内容摘录+时间+来源+配图数）。
 $('qzone-summary').replaceChildren(...[q.available?`今日说说 ${q.posts_today??0} / ${q.daily_max_posts??0}`:'空间状态暂不可用',cookieLabels[cookie.status]||'登录状态尚未检查'].map(s=>el('div','sleeprow',s)));
 renderRecentPosts(q)}
// 空间最近动态（3.17.15）：data.qzone.recent_posts（dashboard 只读投影，≤5 条已确认
// 发送）。纯文本 el() 构造，来源 badge（日程说说/视频分享）；旧快照无 recent_posts →
// 空态降级，不造假。created_at 为 epoch 秒（clock 按毫秒解析故 ×1000）。
function renderRecentPosts(q){const box=$('qzone-recent-posts');if(!box)return;const rows=Array.isArray(q.recent_posts)?q.recent_posts.slice(0,5):[];
 if(!rows.length){empty(box,q.available?'最近没有已确认的说说':'空间状态暂不可用',q.available?'发送成功的日程说说与视频分享会在这里留下记录。':'空间模块就绪后展示。');return}
 box.replaceChildren(...rows.map(row=>{const item=el('div','sleeprow qzone-post'),main=el('div');main.append(el('strong','',row.content||'（空内容）'),el('div','note',(num(row.created_at)!==null?clock(row.created_at*1000,true):'时间未记录')+(num(row.image_count)!==null&&row.image_count>0?' · 配图 '+row.image_count+' 张':'')));item.append(main,el('span','rowbadge postsource',row.source==='视频分享'?'视频分享':'日程说说'));return item}))}
const usageCount=value=>group(count(value));
const usageModuleLabels=new Map(Object.entries({planning:'日程',emotion:'情绪',companion:'陪伴',qzone:'空间',video:'视频',unknown:'未标注模块'}));
const usageModuleLabel=module=>usageModuleLabels.get(module)||String(module);
function usageWindow(usage){
 const hours=num(usage?.window_hours);
 return ({24:'最近 24 小时',168:'最近 7 天',720:'最近 30 天'})[hours]||
  (hours>0?'最近 '+hours+' 小时':'统计范围未记录');
}
function reportedTokens(value,reported){
 return num(reported)===0||count(value)==='—'?'未回传':usageCount(value);
}
function usageMetric(tag,label,value,term,tipLeft){
 const item=el('div','usage-metric'),caption=el(tag,'note',label);
 if(term)caption.append(infoTip(term,tipLeft));
 item.append(caption,el(tag==='dt'?'dd':'strong','usage-value',value));
 return item;
}
function renderUsageOverview(usage){
 const terms=usageTerms();
 const metrics=[
  ['调用次数',usageCount(usage.calls),terms.calls],
  ['宿主回传 Token',usageCount(usage.tokens),terms.tokens],
  ['输入 Token',reportedTokens(usage.prompt_tokens,usage.prompt_reported_calls),terms.prompt_tokens],
  ['输出 Token',reportedTokens(usage.completion_tokens,usage.completion_reported_calls),terms.completion_tokens],
  ['缓存命中',reportedTokens(usage.cache_hit_tokens,usage.cache_reported_calls),terms.cache_hit_tokens],
  ['缓存未命中',reportedTokens(usage.cache_miss_tokens,usage.cache_reported_calls),'宿主回传的未命中 Token。'],
 ];
 replaceInfoChildren($('usage-kpis'),...metrics.map(([label,value,term])=>usageMetric('dt',label,value,term)));
 const buckets=usage.buckets||{};
 replaceInfoChildren($('usage-buckets'),...[['host_reported','用量已回传'],['success_no_usage','成功 · 缺用量'],['failed','失败'],['legacy','旧记录']].map(([key,label])=>{
  const bucket=buckets[key]||{},card=usageMetric('span',label,usageCount(bucket.calls),terms[key]);
  const tokens=usageCount(bucket.tokens);card.append(el('span','note',tokens==='—'?'用量未记录':tokens+' Token'));
  return card;
 }));
 text('usage-period',usageWindow(usage));text('usage-source',imported?'离线快照':'宿主用量记录');
 const updated=$('usage-updated');
 updated.textContent=date(data.generated_at)?clock(data.generated_at,true):'更新时间未记录';
 if(date(data.generated_at))updated.dateTime=data.generated_at;else updated.removeAttribute('datetime');
 updated.title=fullClock(data.generated_at);
}
function renderUsageModules(usage){
 const container=$('usage-models'),matrix=usage.matrix||{};
 const open=new Set([...container.querySelectorAll('details[open]')].map(node=>node.dataset.module));
 const sorted=Object.keys(matrix).sort((a,b)=>(usage.modules?.[b]||0)-(usage.modules?.[a]||0)||a.localeCompare(b));
 const models=entries=>{
  const list=el('ul','usage-model-list');
  for(const [model,cell] of entries.sort(([,a],[,b])=>(b?.tokens||0)-(a?.tokens||0))){
   const item=el('li'),name=el('span','usage-name',model),stats=el('span','usage-amounts');
   stats.append(el('span','note',usageCount(cell?.calls)+' 次'),el('strong','',usageCount(cell?.tokens)+' Token'));
   item.append(name,stats);list.append(item);
  }
  return list;
 };
 container.replaceChildren();
 for(const module of sorted){
  const group=el('details','usage-module'),heading=el('summary','usage-module-head'),totals=el('span','usage-amounts');
  group.dataset.module=module;group.open=open.has(module);
  totals.append(el('span','note',usageCount(usage.module_calls?.[module])+' 次'),el('strong','',usageCount(usage.modules?.[module])+' Token'));
  heading.append(el('span','usage-name',usageModuleLabel(module)),totals);
  const records=el('button','usage-module-records','查看该模块的记录');
  records.type='button';records.disabled=imported||!live;records.onclick=()=>showUsageRecords(module);
  group.append(heading,models(Object.entries(matrix[module]||{})),records);
  container.append(group);
 }
 const fallback=Object.entries(usage.models||{});
 if(!sorted.length&&fallback.length){
  container.append(models(fallback.map(([model,tokens])=>[model,{tokens,calls:usage.model_calls?.[model]}])));
 }
 if(!container.hasChildNodes())container.append(el('p','usage-empty note','这个统计范围内暂无模型用量。'));
 text('usage-models-hint',sorted.length?'展开模块查看各模型的调用与 Token。':fallback.length?'这份记录按模型汇总。':'');
 renderUsageFilter();
}
function renderUsage(){
 const usage=data.usage||{},health=data.health||{};
 text('usage-window',usageWindow(usage)+(usage.truncated?' · 有界日志片段':''));
 const metrics=[
  ['调用次数',usageCount(usage.calls),'calls'],['宿主回传 Token',usageCount(usage.tokens),'tokens'],
  ['输入 Token',reportedTokens(usage.prompt_tokens,usage.prompt_reported_calls),'prompt_tokens'],
  ['输出 Token',reportedTokens(usage.completion_tokens,usage.completion_reported_calls),'completion_tokens'],
  ['缓存命中',reportedTokens(usage.cache_hit_tokens,usage.cache_reported_calls),'cache_hit_tokens'],
 ];
 replaceInfoChildren($('usage-metrics'),...metrics.map(([label,value,key])=>{
  // 3.17.7 改进3：仅「缓存命中」（主页最右 chip）详细描述改左侧防出框；其余 chip 保持右侧
  const box=usageMetric('span',label,value,usageTerms()[key],key==='cache_hit_tokens');return box;
 }));
 renderUsageOverview(usage);renderUsageModules(usage);syncUsageRange();
 if(imported||!live)renderUsageRows();
 // W-01 摘要回填：主卡补 Top3 模块与可拆分口径（纯前端按已有 prompt/completion 字段估算，
 // 宿主未回传拆分时显示 —，不造假 0；P1 的 W-09 落后端 split_coverage 字段后再换权威口径）。
 const topBox=$('usage-top');if(topBox){const top=Object.entries(usage.modules||{}).filter(([,t])=>num(t)!==null&&t>0).sort((a,b)=>b[1]-a[1]);topBox.replaceChildren(...top.map(([m,t])=>el('span','',`${usageModuleLabel(m)} ${count(t)} Token`)));topBox.hidden=!top.length}
 // W-09 权威口径（2026-11-03）：后端已回传 split_coverage 时直接使用；旧离线快照
 // 没有该字段才退回纯前端按 prompt/completion 估算（不造假 0）。
 const splitBox=$('usage-split');if(splitBox){const total=num(usage.tokens),pt=num(usage.prompt_tokens),ct=num(usage.completion_tokens),noSplit=(num(usage.prompt_reported_calls)===0&&num(usage.completion_reported_calls)===0);
  if(total===null)text('usage-split','');
  else if(noSplit)text('usage-split','可拆分（输入+输出）未回传（宿主未回传拆分字段）');
  else if(num(usage.split_coverage)!==null){text('usage-split',`可拆分（输入+输出）${count(usage.split_tokens)} / ${count(total)} Token · 覆盖率 ${Math.round(usage.split_coverage*1000)/10}%，其余为宿主未回传拆分的调用`)}
  else if(pt!==null&&ct!==null&&total>0){text('usage-split',`可拆分（输入+输出）${count(pt+ct)} / ${count(total)} Token · 约 ${Math.round((pt+ct)/total*1000)/10}%，其余为宿主未回传拆分的调用`)}
  else text('usage-split',`Token 总量 ${count(total)} · 输入/输出拆分宿主未回传，显示为 —`)}
 const coverage=num(usage.split_coverage);
 text('usage-detail-split',coverage===null?$('usage-split').textContent:
  '输入与输出共回传 '+usageCount(usage.split_tokens)+' Token · 覆盖总量 '+Math.round(coverage*1000)/10+'%');
 const missing=usageCount(usage.missing_usage_source_calls);
 text('usage-note','调用次数包含失败响应。Token 仅统计宿主回传的用量。'+
  (missing==='—'?'':' 缺少完整用量或旧标记的调用 '+missing+' 次，可能与失败重叠。'));
 // W-17（2026-11-03）：错误计数带最近一次时间与归属（后端只增 last_error_at/scope），
 // 并指路宿主日志页；原始日志正文不上页面。
 const errTime=health.last_error_at?` · 最近一次错误 ${clock(health.last_error_at,true)}（${health.last_error_scope==='plugin'?'本插件':health.last_error_scope==='external'?'其他模块':'来源未标记'}）`:'';
 text('module-health',`最近日志片段（有界）：本插件错误 ${health.plugin_errors??'—'} · 其他模块错误 ${health.external_errors??'—'}${health.last_reload?' · 最近热重载 '+fullClock(health.last_reload):''}${errTime}。不同来源分开统计；原始日志请到宿主「日志」页查看。`);
}
function deadline(promise,ms=8000){let handle;const source=Promise.resolve(promise);source.catch(()=>{});return Promise.race([source,new Promise((_,reject)=>{handle=setTimeout(()=>reject(Error('请求超时，请重试连接')),ms)})]).finally(()=>clearTimeout(handle))}
function updateFreshness(now=Date.now()){
 if(imported){connection('离线快照','warn');return}
 if(!live){return}
 const age=lastSuccessAt?Math.max(now-lastSuccessAt,data?.generated_at?now-Number(date(data.generated_at)):0):Infinity;
 if(lastError){connection('连接暂不可用 · 保留上次快照','warn')}
 else if(!data){connection(busy?'正在连接':'等待连接','warn')}
 else if(age>120000){connection('数据已陈旧 · 请刷新','warn')}
 else if(age>45000||data.stale){connection('快照可能过期','warn')}
 else{connection(data.warnings?.length?'已连接 · 留意数据':'实时连接',data.warnings?.length?'warn':'live')}
 $('connection').title=lastSuccessAt?`距最近成功读取 ${Math.max(0,Math.floor((now-lastSuccessAt)/1000))} 秒；数据生成 ${clock(data?.generated_at,true)}`:'尚未成功读取';
}
async function refresh(manual=false){
 if(!live||(!manual&&busy)||document.hidden)return;
 const seq=++requestSeq;busy=true;clearTimeout(timer);lastError='';$('refresh').setAttribute('aria-busy','true');updateFreshness();
 try{
  let result;
  const baseParams={hours,...(selectedDay?{day:selectedDay}:{})};
  if(usageParamSupported){
   try{result=validateSnapshot(await deadline(window.maibot.query('summary',{...baseParams,usage_hours:usageHours})))}
   catch(first){
    // finding #3（2026-11-04）：仅参数类错误（422/未知参数）才降级退回旧参数；
    // 临时错误（宿主 5xx/429/网络/快照校验失败等）保留 usageParamSupported，交外层
    // 退避重试——不再因一次临时失败就整页会话静默忽略用户所选 usage_hours 窗口。
    // 超时不重试，避免双倍等待（沿用原口径）。
    const msg=String(first?.message||first);
    if(msg.includes('超时'))throw first;
    if(!/422|usage_hours|参数|parameter|unsupported|not supported/i.test(msg))throw first;
    usageParamSupported=false;
    result=validateSnapshot(await deadline(window.maibot.query('summary',baseParams)));
   }
  }else{result=validateSnapshot(await deadline(window.maibot.query('summary',baseParams)))}
  if(seq!==requestSeq)return;
  data=result;imported=false;lastSuccessAt=Date.now();failures=0;lastError='';render();notice(data.warnings?.length?data.warnings.join('；'):'');
 }catch(error){if(seq!==requestSeq)return;failures++;lastError=String(error.message||error).slice(0,180);notice(`暂时无法更新：${lastError}。${data?'上次快照已保留。':'可稍后刷新。'}`,true)}
 finally{if(seq===requestSeq){busy=false;$('refresh').removeAttribute('aria-busy');$('refresh').disabled=false;updateFreshness();schedule()}}
}
function schedule(){clearTimeout(timer);if(live&&!imported&&!document.hidden)timer=setTimeout(()=>refresh(),Math.min(300000,30000*2**Math.min(failures,4)))}
// 宿主会把自身卡片探针的圆角采样进 :root{--maibot-radius} 与 .maibot-card{border-radius}。
// finding #1（2026-11-04）：宿主 SDK 另维护独立 themeStyle（仅 c.styles、无守卫），并在
// DOMContentLoaded 经 observeLayout 追加到 body 末尾——同特异性下后者覆盖 #host-theme
// 里的守卫，宿主主题偏方时圆角仍被抹平。插件侧加固（不改宿主源码）：
// ① 守卫升 !important + html[data-embedded] 更高特异性——级联上无论先后都胜出；
// ② 独立守卫 style 追加 body 末尾，DOMContentLoaded/每次 context 重申（注入时机防御）。
// 20px 圆角是本插件的固定设计值；颜色/字体仍跟随宿主。
const LBM_DESIGN_GUARD='\n:root{--maibot-radius:20px!important}html[data-embedded] .maibot-card{border-radius:var(--maibot-radius)!important}';
function assertDesignGuard(){if(!window.maibot||!document.body)return;let g=document.getElementById('lbm-design-guard');if(!g){g=document.createElement('style');g.id='lbm-design-guard'}g.textContent=LBM_DESIGN_GUARD;document.body.append(g)}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',assertDesignGuard);
function context(){const c=window.maibot?.context;if(!c)return;document.documentElement.dataset.dashboardStyle=c.dashboardStyle||'modern';document.documentElement.dataset.theme=c.theme==='dark'?'dark':'light';document.documentElement.dataset.embedded='true';if(typeof c.styles==='string')$('host-theme').textContent=c.styles+LBM_DESIGN_GUARD;assertDesignGuard()}
$('refresh').onclick=()=>refresh(true);$('range').onclick=e=>{const b=e.target.closest('button[data-hours]');if(!b)return;selectedDay="";$("history-day").value="";hours=Number(b.dataset.hours);zoom=null;$('zoom-reset').hidden=true;for(const button of $('range').querySelectorAll('button'))button.setAttribute('aria-pressed',String(Number(button.dataset.hours)===hours));if(live&&!imported)refresh(true);else if(data){computePoints();renderActiveTab()}};
function syncUsageRange(){
 const readonly=imported||!live||!usageParamSupported;
 const selected=readonly?data?.usage?.window_hours:usageHours;
 for(const button of document.querySelectorAll('#usage-range button, #usage-detail-range button')){
  button.setAttribute('aria-pressed',String(Number(button.dataset.usageHours)===selected));
  button.disabled=readonly;
 }
}
let usageRowsOffset=0,usageRowsTotal=0,usageRowsTruncated=false,usageRowsLoading=false,usageRowsSeq=0;
function resetUsageRows(){
 usageRowsSeq++;usageRowsLoaded=false;usageRowsLoading=false;usageRowsAll=[];
 usageRowsOffset=usageRowsTotal=0;usageRowsTruncated=false;usageFilter='';
 $('usage-rows').replaceChildren();
}
function renderUsageFilter(){
 const usage=data?.usage||{},select=$('usage-module-filter');
 const modules=new Set([...Object.keys(usage.modules||{}),...Object.keys(usage.matrix||{}),...usageRowsAll.map(row=>row.module).filter(Boolean)]);
 if(usageFilter)modules.add(usageFilter);
 select.replaceChildren(el('option','','全部模块'));
 select.firstElementChild.value='';
 for(const name of [...modules].sort((a,b)=>usageModuleLabel(a).localeCompare(usageModuleLabel(b),'zh-CN'))){
  const option=el('option','',usageModuleLabel(name));option.value=name;select.append(option);
 }
 select.value=usageFilter;
 $('usage-record-filter').hidden=imported||!live||!modules.size;
}
function showUsageRecords(module){
 usageFilter=module;
 renderUsageFilter();
 $('usage-details').open=true;
 loadUsageRows();
 $('usage-details').querySelector('summary').focus();
}
function usageRecord(row){
 const item=el('li','usage-record'),context=el('div','usage-record-context'),model=el('div','usage-record-model');
 const time=el('time','',clock(row.ts,true));
 if(date(row.ts)){time.dateTime=row.ts;time.title=fullClock(row.ts)}
 context.append(time,el('span','note',usageModuleLabel(row.module||'unknown')));
 model.append(el('strong','usage-name',row.model||'模型未记录'));
 const event=[row.event,row.task_name].filter(Boolean).join(' · ');
 if(event)model.append(el('span','note usage-name',event));
 const tokens=el('dl','usage-record-tokens');
 for(const [label,value] of [['输入',row.prompt],['输出',row.completion],['缓存命中',row.cache_hit],['缓存未命中',row.cache_miss]]){
  tokens.append(el('dt','note',label),el('dd','',usageCount(value)));
 }
 const outcome=el('div','usage-record-outcome');
 const status=row.abandoned?'已放弃':row.success===true?'成功':row.success===false?'失败':'未知';
 const badge=el('span','usage-status',status);badge.dataset.status=status;
 outcome.append(badge);
 if(count(row.duration_ms)!=='—')outcome.append(el('span','note',usageCount(row.duration_ms)+' ms'));
 item.append(context,model,tokens,outcome);return item;
}
function renderUsageRows(){
 const target=$('usage-rows');
 renderUsageFilter();
 if(imported||!live){target.replaceChildren(el('p','usage-empty note','离线快照只包含汇总用量。'));return}
 const rows=usageRowsAll.filter(row=>!usageFilter||row.module===usageFilter);
 const stats=el('p','note usage-record-count',
  (usageFilter?'匹配 '+usageCount(rows.length)+' 条 · ':'')+
  '已读取 '+usageCount(usageRowsAll.length)+' / '+usageCount(usageRowsTotal)+' 条');
 stats.setAttribute('role','status');target.replaceChildren(stats);
 if(rows.length){
  const list=el('ol','usage-records');
  list.append(...rows.map(usageRecord));target.append(list);
 }else target.append(el('p','usage-empty note',usageFilter?'已读取的记录中暂无该模块的调用。':'暂无逐笔记录。'));
 if(usageRowsTruncated){
  const more=el('button','usage-more','加载更多记录');
  more.type='button';more.onclick=()=>{more.disabled=true;loadUsageRows(true)};
  target.append(more);
 }
}
async function loadUsageRows(append=false){
 const target=$('usage-rows');
 if(imported||!live){renderUsageRows();return}
 if(usageRowsLoading)return;
 if(!append&&usageRowsLoaded){renderUsageRows();return}
 const seq=++usageRowsSeq;
 usageRowsLoading=true;
 if(!append)target.replaceChildren(el('p','usage-empty note','正在读取逐笔记录…'));
 else target.querySelector('.usage-row-error')?.remove();
 try{
  const params={hours,...(usageParamSupported?{usage_hours:usageHours}:{}),usage_rows_limit:50,usage_rows_offset:append?usageRowsOffset:0,...(selectedDay?{day:selectedDay}:{})};
  const result=await deadline(window.maibot.query('summary',params));
  if(seq!==usageRowsSeq)return;
  const page=result?.usage?.rows_page;
  if(!Array.isArray(page?.rows))throw Error('缺少逐笔记录');
  usageRowsAll=append?usageRowsAll.concat(page.rows):page.rows;
  usageRowsOffset=usageRowsAll.length;
  usageRowsTotal=num(page.total)??usageRowsOffset;
  usageRowsTruncated=Boolean(page.truncated&&page.rows.length);
  usageRowsLoaded=true;renderUsageRows();
  if(append)(target.querySelector('.usage-more')||$('usage-details').querySelector('summary')).focus({preventScroll:true});
 }catch(e){
  if(seq!==usageRowsSeq)return;
  const retry=el('button','usage-more','重试读取');
  retry.type='button';retry.onclick=()=>{retry.disabled=true;loadUsageRows(append)};
  if(append)target.querySelector('.usage-more')?.remove();else target.replaceChildren();
  target.append(el('p','note usage-row-error','逐笔明细暂不可用。'),retry);
 }finally{if(seq===usageRowsSeq)usageRowsLoading=false}
}
async function changeUsageRange(event){
 const button=event.target.closest('button[data-usage-hours]');
 if(!button||imported||!live||!usageParamSupported)return;
 const next=Number(button.dataset.usageHours);
 if(next===usageHours)return;
 usageHours=next;
 try{localStorage.setItem('lbmUsageHours',String(usageHours))}catch(err){}
 resetUsageRows();syncUsageRange();
 if($('usage-details').open)$('usage-rows').append(el('p','usage-empty note','正在切换统计范围…'));
 await refresh(true);
 if(usageHours!==next||!$('usage-details').open)return;
 if(lastError){$('usage-rows').replaceChildren(el('p','usage-empty note','统计范围暂未更新，请刷新后重试。'));return}
 loadUsageRows();
}
$('usage-range').onclick=changeUsageRange;
$('usage-detail-range').onclick=changeUsageRange;
$('usage-module-filter').onchange=e=>{usageFilter=e.target.value;loadUsageRows()};
$('usage-details').ontoggle=()=>{if($('usage-details').open)loadUsageRows()};
$('samples-toggle').onclick=()=>{const open=$('samples').hidden;$('samples').hidden=!open;$('samples-toggle').setAttribute('aria-expanded',String(open));text('samples-toggle',open?'收起采样值':'查看采样值')};
$('emotion-samples-toggle').onclick=()=>{const open=$('emotion-history-samples').hidden;$('emotion-history-samples').hidden=!open;$('emotion-samples-toggle').setAttribute('aria-expanded',String(open));text('emotion-samples-toggle',open?'收起采样值':'查看采样值')};
$('export').onclick=()=>{if(!data)return;$('snapshot-text').value=JSON.stringify(data);$('snapshot-panel').hidden=false;$('snapshot-panel').scrollIntoView({block:'start'})};$('snapshot-close').onclick=()=>$('snapshot-panel').hidden=true;$('snapshot-select').onclick=()=>{$('snapshot-text').focus();$('snapshot-text').select()};
// W-04：嵌入态「更多」菜单收纳导出快照——离线页不显示菜单（保留原导出按钮），
// 两个入口共用同一处理；Esc/点击外部关闭，aria-expanded 同步。
const moreBtn=$('more'),moreMenu=$('more-menu');
if(moreBtn&&moreMenu){
 moreBtn.hidden=!window.maibot;
 const mascotBtn=$('more-mascot');
 // W-12：菜单文案跟随本地收起状态；点击切换并立即生效（不动服务端 enabled）。
 const syncMascotLabel=()=>{if(mascotBtn&&typeof Mascot!=='undefined')mascotBtn.textContent=Mascot.isLocalHidden()?'显示麦麦':'收起麦麦'};
 const closeMore=()=>{if(!moreMenu.hidden){moreMenu.hidden=true;moreBtn.setAttribute('aria-expanded','false')}};
 moreBtn.onclick=e=>{e.stopPropagation();syncMascotLabel();const open=moreMenu.hidden;moreMenu.hidden=!open;moreBtn.setAttribute('aria-expanded',String(open));if(open)moreMenu.querySelector('button:not([disabled])')?.focus()};
 document.addEventListener('click',e=>{if(!moreBtn.contains(e.target)&&!moreMenu.contains(e.target))closeMore()});
 document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!moreMenu.hidden){closeMore();moreBtn.focus({preventScroll:true})}});
 $('more-export').onclick=()=>{closeMore();$('export').click()};
 if(mascotBtn)mascotBtn.onclick=()=>{if(typeof Mascot!=='undefined')Mascot.setLocalHidden(!Mascot.isLocalHidden());syncMascotLabel();closeMore()};
}
$('import').onchange=async e=>{
 const input=e.target,file=input.files[0];if(!file)return;const seq=++importSeq,epoch=++requestSeq;busy=false;clearTimeout(timer);$('refresh').removeAttribute('aria-busy');
 try{if(file.size>512*1024)throw Error('快照不得超过 512 KiB');const raw=await file.text();if(seq!==importSeq||epoch!==requestSeq)return;const result=validateSnapshot(JSON.parse(raw));data=result;imported=true;resetUsageRows();selectedDay=result.day?.available?result.day.date:'';$('history-day').value=selectedDay;render();notice('正在查看离线快照，不会自动更新。')}
 catch(error){if(seq===importSeq&&epoch===requestSeq)notice(String(error.message||error),true)}
 finally{if(seq===importSeq){input.value='';schedule()}}
};
$('theme').onclick=()=>{document.documentElement.dataset.theme=document.documentElement.dataset.theme==='dark'?'light':'dark';if(data)render()};document.addEventListener('visibilitychange',()=>{clearTimeout(timer);if(!document.hidden&&live)refresh()});window.addEventListener('maibot:context',()=>{context();if(data)render()});window.addEventListener('pagehide',()=>{clearTimeout(timer);clearInterval(freshnessTimer);requestSeq++});
const iconPaths={activity:'M3 12h4l3-8 4 16 3-8h4',heart:'M20 4c-3-2-6 0-8 2-2-2-5-4-8-2-6 5 4 12 8 15 4-3 14-10 8-15Z',moon:'M20 14A8 8 0 0 1 10 4a8 8 0 1 0 10 10Z',clock:'M12 8v5l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0',list:'M9 5h12M9 12h12M9 19h12M3 5h1M3 12h1M3 19h1',cloud:'M6 18h12a4 4 0 0 0 0-8 6 6 0 0 0-12-2 5 5 0 0 0 0 10',cpu:'M7 7h10v10H7ZM3 8h4M3 16h4M17 8h4M17 16h4M8 3v4M16 3v4M8 17v4M16 17v4',focus:'M8 3H3v5M16 3h5v5M3 16v5h5M21 16v5h-5M8 12h8M12 8v8',cup:'M4 4h12v9a6 6 0 0 1-12 0ZM16 6h2a3 3 0 0 1 0 6h-2M3 22h15',battery:'M2 6h18v12H2ZM20 10h2v4M6 9v6M10 9v6M14 9v6'};
function makeIcon(path){const icon=svg('svg',{viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':1.7,'stroke-linecap':'round','stroke-linejoin':'round',class:'heading-icon','aria-hidden':'true'});icon.append(svg('path',{d:iconPaths[path]||iconPaths.activity}));return icon}
for(const heading of document.querySelectorAll('.card h2')){const label=heading.textContent;heading.prepend(makeIcon(/睡眠|恢复/.test(label)?'moon':/陪伴|心情/.test(label)?'heart':/模型/.test(label)?'cpu':/分享/.test(label)?'cloud':/意图|足迹|一起/.test(label)?'list':/起伏/.test(label)?'clock':'activity'))}
for(const[key,,,code]of spec){const target=$('value-'+key).parentElement.querySelector('.metricicon');target.replaceChildren(makeIcon({energy:'battery',hunger:'cup',focus:'focus',stress:'activity',social_battery:'heart'}[key]))}
for(const[id,title]of[['history-chart','等待真实的生活记录'],['debt-chart','睡眠债记录待连接'],['sleep-list','等待睡眠会话'],['episodes','等待实际活动记录'],['intents','等待今日日意图']])empty($(id),title,'记录会从实际发生的时刻开始。');
// Calendar helpers always use the snapshot's business time zone.
function dayText(value){const d=date(value)||new Date();const parts=new Intl.DateTimeFormat('en-CA',{timeZone:data?.timezone||'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(d);return ['year','month','day'].map(k=>parts.find(p=>p.type===k).value).join('-')}
function zonedMidnight(day){const target=Date.parse(day+'T00:00:00Z');let guess=target;const fmt=new Intl.DateTimeFormat('en-GB',{timeZone:data?.timezone||'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});for(let i=0;i<3;i++){const parts=Object.fromEntries(fmt.formatToParts(new Date(guess)).map(p=>[p.type,p.value]));const local=Date.UTC(+parts.year,+parts.month-1,+parts.day,+parts.hour,+parts.minute,+parts.second);guess+=target-local}return guess}
function shiftDay(day,delta){return new Date(Date.parse(day+'T12:00:00Z')+delta*86400000).toISOString().slice(0,10)}
function setDay(day){if(imported&&day&&data.day?.date!==day){notice('这份离线快照没有所选日期，保留原有记录。');$('history-day').value=selectedDay;return}selectedDay=day;zoom=null;$('zoom-reset').hidden=true;$('history-day').value=day;if(live&&!imported)refresh(true);else if(data){render();if(day&&data.day?.date!==day)notice('这份离线快照没有所选日期，仍展示快照中的记录。')}}
function activateTab(key,focus=false){SectionFish.close();activeTab=key;$("datebar").hidden=key==="now"||key==="system";for(const b of document.querySelectorAll('[role=tab]')){const chosen=b.dataset.tab===key;b.setAttribute('aria-selected',String(chosen));b.tabIndex=chosen?0:-1;$('panel-'+b.dataset.tab).hidden=!chosen;if(chosen&&focus)b.focus()}if(data){updateDateLabel();computePoints();renderActiveTab()}}
// 3.17.11 PR-TABS-LEFT：栏目改左侧纵向后方向键语义加上下（Down=下一栏/Up=上一栏），左右保留兼容；Home/End 与切换逻辑不变。
for(const b of document.querySelectorAll('[role=tab]')){b.onclick=()=>activateTab(b.dataset.tab);b.onkeydown=e=>{const list=[...document.querySelectorAll('[role=tab]')],i=list.indexOf(b);let next=null;if(e.key==='ArrowDown'||e.key==='ArrowRight')next=list[(i+1)%list.length];if(e.key==='ArrowUp'||e.key==='ArrowLeft')next=list[(i+list.length-1)%list.length];if(e.key==='Home')next=list[0];if(e.key==='End')next=list.at(-1);if(next){e.preventDefault();activateTab(next.dataset.tab,true)}}}
$('zoom-in').onclick=()=>{if(!points.length)return;const a=zoom?.[0]??Number(date(points[0].at)),b=zoom?.[1]??Number(date(points.at(-1).at)),mid=(a+b)/2;zoom=[mid-(b-a)/4,mid+(b-a)/4];$('zoom-reset').hidden=false;drawHistory()};$('zoom-out').onclick=resetZoom;
$('history-day').onchange=e=>setDay(e.target.value);$('date-today').onclick=()=>setDay(dayText(data?.generated_at));$('date-yesterday').onclick=()=>setDay(shiftDay(dayText(data?.generated_at),-1));$('date-range').onclick=()=>setDay('');
function updateDateLabel(){
 if(!data)return;
 const today=dayText(data.generated_at);let dayLabel;
 if(activeTab==='schedule')dayLabel=(selectedDay||data.schedule?.date||today)+' · 当日行程';
 else if(selectedDay)dayLabel=selectedDay+(selectedDay===today?' · 今天':' · 历史记录');
 else dayLabel=activeTab==='mood'?'近期娱乐与空间动态':'此刻与最近时段';
 text('selected-day-label',dayLabel);
}
function renderExtras(){
 const today=dayText(data.generated_at);$('history-day').max=today;
 updateDateLabel();
 $('date-today').setAttribute('aria-pressed',String(selectedDay===today));$('date-yesterday').setAttribute('aria-pressed',String(selectedDay===shiftDay(today,-1)));$('date-range').setAttribute('aria-pressed',String(!selectedDay));
 $('hero-scene').classList.toggle('sleeping',!!data.sleep?.current?.asleep);$('emotion-card').classList.toggle('empty-mood',!data.emotion?.available);
 const dayInfo=$('history-day-summary');dayInfo.replaceChildren();const view=dayView();if(view){for(const [key,label]of [['episode_count','活动记录'],['completed_count','完成'],['interrupted_count','中断'],['sleep_session_count','睡眠会话']])dayInfo.append(el('span','',`${label} ${view.summary?.[key]??'—'}`));for(const row of view.sleep?.sessions||[])dayInfo.append(el('span','',`睡眠 ${minute(row.actual_start_minute)} → ${minute(spanEnd(row.actual_start_minute,row.actual_end_minute))}${row.wake_reason==='offline_reconnect'?' · 离线补全':''}`))}
 text('plan-note',dayView()?.intent?.note||'计划是意图，实际足迹记录发生的活动；它们可以不同。');
 // W-13：睡眠接线喂两个形象——旧鱼（萨卡班麦麦，闭眼探头）与新熊（Bear，Zzz+降饱和），
 // 日程睡眠投影同步；预设切换由 applyConfig 管，两个模块都做缺失兜底。
 const asleepNow=Boolean(data.sleep&&data.sleep.current&&data.sleep.current.asleep);
 if(typeof Mascot!=='undefined'){Mascot.applyConfig(data.maimai);Mascot.setSleep(asleepNow)}
 if(typeof Bear!=='undefined')Bear.setSleep(asleepNow);
 // W-15：时间线只属于「日程」tab，重 DOM（最多 240 行）随 renderActiveTab 的日程分支重建。
 SectionFish.refresh();
}
function renderTimeline(){
 const sch=dayRows(),day=selectedDay||sch.date||dayText(data.generated_at),root=$('dual-timeline');
 const open=openedIdleRuns(root);
 root.replaceChildren();text('timeline-date',day);
 const items=scheduleFootprint((sch.episodes||[]).slice(0,120),day).map(row=>({
  row,fixed:false,start:num(row.actual_start_minute)??num(row.planned_start_minute),
 }));
 for(const row of sch.intents||[]){
  if(row.hardness==='hard')items.push({row,fixed:true,start:num(row.start_minute)});
 }
 if(!items.length){empty(root,sch.enabled?'这一天暂无活动记录或固定意图':'日程未启用','实际活动与固定意图会在此分开标识。');return}
 items.sort((a,b)=>(a.start??Infinity)-(b.start??Infinity));
 const list=el('div','vtime');list.setAttribute('role','list');list.setAttribute('aria-label','行程记录时间线');
 for(const {row,fixed,start} of items){
  if(start===null)continue;
  const active=!fixed&&row.status==='active';
  const end=spanEnd(start,fixed?num(row.end_minute):num(row.actual_end_minute)??num(row.planned_end_minute));
  const separator=row.idleGap?' 至 ':' → ';
  const node=el('article','vtime-item '+(fixed?'planned':'actual')+(active?' active':'')+(!fixed&&row.status==='interrupted'?' interrupted':''));
  node.setAttribute('role','listitem');
  const body=el('div','vtime-body'),head=el('div','vtime-head');
  head.append(el('h3','',row.name||'未命名活动'),el('span','vtime-badge',fixed?(row.goal_type==='sleep'?'作息参考':'固定意图'):episodeLabel(row.status)));
  // 1A（3.17.8）：续段时间显示 00:00→HH:MM（投影已 0 起），来源沿用 source()；
  // 跨午夜原行结束时间保留 minute() 的「次日 HH:MM」口径，另加「跨日」badge。
  if(!fixed&&row.cross_day_continuation)head.append(el('span','vtime-badge crossdaybadge','跨日续段'));
  else if(!fixed&&row.crosses_midnight)head.append(el('span','vtime-badge crossdaybadge','跨日'));
  const origin=fixed?(row.origin==='archived'?'历史计划归档':'日计划'):source(row.source);
  body.append(head,el('p','note',minute(start)+separator+(active?'此刻':end===null?'待定':minute(end))+' · '+origin));
  if(row.idleSegments)body.append(idleRunDetails(row,open));
  if(!fixed&&row.state_before&&row.state_after){
   const changes=spec.filter(([k])=>num(row.state_before[k])!==null&&num(row.state_after[k])!==null&&Math.abs(row.state_after[k]-row.state_before[k])>=.01).slice(0,3).map(([k,l])=>l+' '+pct(row.state_before[k])+'%→'+pct(row.state_after[k])+'%');
   if(changes.length)body.append(el('p','vtime-change',changes.join(' · ')));
  }
  node.append(el('time','',minute(start)),el('i','vtime-dot'),body);
  list.append(node);
 }
 root.append(list);
}

function usageTerms(){return {calls:'收到宿主响应的请求数，包含失败；未收到响应的本地异常不计入。',tokens:'宿主明确回传的文本处理量（含只有总量的调用）。缺失时只计调用，不推算。',prompt_tokens:'宿主回传的输入 Token 总量；未回传拆分的调用不参与，因此与总量可能不等。',completion_tokens:'宿主回传的输出 Token 总量；未回传拆分的调用不参与，因此与总量可能不等。',cache_hit_tokens:'宿主回传的 prompt 缓存命中 Token；「未回传」表示没有任何一行携带该字段，此时无法区分真 0 与未报。',split_coverage:'可拆分覆盖率 =（输入+输出）/ 宿主回报总量；覆盖率低说明较多调用未回传拆分字段，不代表数据出错。',missing_usage_source_calls:'缺少完整宿主用量或新标记的调用，可与失败重叠；它本身不代表失败。',host_reported:'宿主返回成功响应及完整usage。',success_no_usage:'success=true，但宿主未回传完整usage。',failed:'宿主返回失败响应。',legacy:'升级前没有success和usage_present标记的记录。'}}
let infoSequence=0;
function replaceInfoChildren(target,...nodes){const old=[...target.querySelectorAll('.info-button')],index=old.indexOf(document.activeElement),wasOpen=index>=0&&old[index].getAttribute('aria-expanded')==='true';target.replaceChildren(...nodes);if(index>=0){const next=target.querySelectorAll('.info-button')[index];if(next){next.focus({preventScroll:true});if(!wasOpen)next.onblur()}}}
function infoTip(explanation,tipLeft){
 // tipLeft=true：仅该条 tooltip 右锚定向左展开（3.17.7 改进3，只影响带此类的项）
 const wrap=el('span','info-wrap'),button=el('button','info-button','i'),tip=el('span','info-tooltip'+(tipLeft?' info-tooltip-left':''),explanation||'暂无说明');button.type='button';button.setAttribute('aria-label','查看说明');tip.id='info-explanation-'+(++infoSequence);button.setAttribute('aria-describedby',tip.id);button.setAttribute('aria-expanded','false');tip.setAttribute('role','tooltip');tip.hidden=true;
 const show=()=>{tip.hidden=false;button.setAttribute('aria-expanded','true');if(button.closest('.section-fish-popover')){const r=button.getBoundingClientRect(),width=Math.min(340,innerWidth-24),h=tip.getBoundingClientRect().height;tip.style.left=Math.max(12,Math.min(innerWidth-width-12,r.left-12))+'px';tip.style.right='auto';tip.style.width=width+'px';tip.style.bottom='auto';tip.style.top=Math.max(12,Math.min(innerHeight-h-12,r.bottom+6))+'px'}else if(tip.classList.contains('info-tooltip-left')){
  // 3.17.7 改进3：左侧 tooltip 右锚定按钮向左展开；窄屏折行到左列时钳制不出视口左缘。
  // 仅作用于带 info-tooltip-left 的项（缓存命中），其他 chip 仍走纯 CSS 右侧定位。
  const r=button.getBoundingClientRect(),wrapR=wrap.getBoundingClientRect(),width=Math.min(280,innerWidth-24);tip.style.width=width+'px';tip.style.left=(Math.max(12,Math.min(r.right-width,innerWidth-width-12))-wrapR.left)+'px';tip.style.right='auto'}};
 const hide=()=>{tip.hidden=true;button.setAttribute('aria-expanded','false')};button.onmouseenter=show;button.onmouseleave=()=>{if(document.activeElement!==button)hide()};button.onfocus=show;button.onblur=hide;button.onkeydown=e=>{if(e.key==='Escape'&&!tip.hidden){e.stopPropagation();hide()}};let pointerWasOpen=false;button.onpointerdown=()=>{pointerWasOpen=!tip.hidden};button.onclick=e=>{e.stopPropagation();pointerWasOpen?hide():show();pointerWasOpen=false};wrap.append(button,tip);return wrap
}


/* LBM_EXTENSIONS */
freshnessTimer=setInterval(()=>updateFreshness(),1000);
(async()=>{if(window.maibot){try{live=true;await deadline(window.maibot.ready);context();await refresh()}catch(e){lastError=String(e.message||e);updateFreshness();notice(lastError,true);schedule()}}else{document.documentElement.dataset.theme=matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light';$('offline').hidden=false;$('theme').hidden=false;$('refresh').disabled=true;connection('离线查看','warn');text('updated','导入快照后查看真实记录')}})();
})();
