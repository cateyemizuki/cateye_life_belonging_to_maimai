/* 常驻麦麦：左下角桌宠状态机（2026-11-01 U 计划 §三）。
   收起探头（原图朝右，展示层 rotate(-角度) 使头朝右上）→ 弹出（先向上弹起、
   再下降落到左下角底部，带落地缓冲；落地时身体水平）→ 落地后接受捏捏（按下
   从当前形态连续压扁、松开从实际压扁程度连续回弹）→ 指针远离交互区域自动收起。
   分层：.maimai-peek-tilt 只管位移/旋转（状态机经 WAAPI 驱动），
   .maimai-peek-art 只管按压缩放（连续形变，可取消接续，不读布局）。
   参数由 life_dashboard 的 maimai 投影经 applyConfig 写入；投影缺失时沿用
   同值兜底（不另造第二套默认语义）。 */
const Mascot=(()=>{
 const button=$('maimai-peek');
 if(!button)return {applyConfig(){},setDetailMode(){}};
 const tilt=button.querySelector('.maimai-peek-tilt');
 let art=button.querySelector('.maimai-peek-art');
 const DEFAULT_EASING='cubic-bezier(.34,1.56,.64,1)';
 // 运动常数：U 计划 §3.1 建议值（出场 450–650ms、收起延迟约 200ms），调试起点，非业务规则。
 // PRESS_MS=按下渐变压扁时长（用户 0424 目检：按下要"逐渐变扁"并保持，松开才回弹）。
 const EMERGE_MS=560,RETREAT_MS=320,RETREAT_DELAY_MS=200,PRESS_MS=260;
 // 触发圈系数（0425 目检：缩小一圈）；REARM=解除武装（重新武装阈值）。
 const TRIGGER_FACTOR=1.15,REARM_FACTOR=1.35;
 const reduceMotion=()=>matchMedia('(prefers-reduced-motion: reduce)').matches;
 const clamp=(v,low,high)=>Math.min(high,Math.max(low,v));
 const number=(block,key,fallback)=>{const v=block?block[key]:undefined;return typeof v==='number'&&Number.isFinite(v)?v:fallback};
 const easing=value=>typeof value==='string'&&value.trim()&&/^[\w\s.,%()+\-/]*$/.test(value)&&value.length<=80?value.trim():DEFAULT_EASING;
 const cfg={angle:12,retreatPx:200,size:128,squashX:1.2,squashY:.5,bounceMs:774,bounceEasing:DEFAULT_EASING};
 let state='peek';               // peek | emerging | landed | retreating
 let pressing=false,detailMode=false,pendingRetreat=false,pendingEmerge=false,hoverArmed=true;
 let retreatTimer=null,poseAnim=null,squashAnim=null,rotateAnim=null,autoPinSeq=0;
 // 自动捏捏调度令牌：任何打断（手动捏/停止/收起）使旧链失效，链路恒单条——
 // 否则激活态下每次手动点鱼都会复制一条定时器链，链路增殖导致疯狂抽搐。
 const scheduleAuto=(fn,ms)=>{const g=++autoPinSeq;setTimeout(()=>{if(g===autoPinSeq)fn()},ms)};
 let squash={sx:1,sy:1};         // 形变记账：JS 记当前值，避免每拍读布局
 let squashFrames=[{sx:1,sy:1,offset:0},{sx:1,sy:1,offset:1}];
 // 收起探头姿态：小幅 rotate(-角度) + 往角落里收（左移+下沉），尾巴上的叶子移出
 // 屏幕左缘，只露头顶+双眼；「橙/奶白颜色分界线」仍穿过视口角落点（logo 式冒头）。
 // 系数按 12° 倾角推导（peek_angle_deg 为调参项，默认 12）。
 const peekTransform=()=>`translate(${(-0.234*cfg.size).toFixed(1)}px, ${(0.234*cfg.size).toFixed(1)}px) rotate(${-cfg.angle}deg)`;
 // 自动捏捏（0425 目检新增）：落地后在尾叶上方的小按钮开关；激活后按计划书
 // 最初参数（squash 1.2/0.5、bounce 774ms overshoot）循环“压扁→回弹”，
 // 手动捏取时暂停、松开续播；收起/详情/失焦/减少动态一律停止。
 const pinBtn=document.getElementById('maimai-autopin');
 // 眼珠独立层（0425 目检新增）：桌宠改用内联 SVG，把两颗瞳孔从暗色组拆进
 // 可平移的子组——原始路径/配色不动，仅运行时分组；视线随鼠标（距离越近偏移越小）。
 let eyePupils=null,eyeLids=null,eyeRaf=0,sleepPaused=false;
 const eyeTarget={x:Math.round(innerWidth*0.7),y:Math.round(innerHeight*0.3)};
 function setupEyes(){
  try{
   const tpl=$('section-fish-art');
   if(!tpl||!tpl.content||!tpl.content.firstElementChild)return;
   const host=el('span','maimai-peek-art');
   host.setAttribute('aria-hidden','true');
   host.append(tpl.content.firstElementChild.cloneNode(true));
   let pupils=null;
   const SVG_NS='http://www.w3.org/2000/svg';
   for(const g of host.querySelectorAll('g[fill="#080909"]')){
    const ells=[...g.querySelectorAll('ellipse')];
    if(ells.length===2&&!pupils){
     // 必须用 SVG 命名空间创建分组（el() 造出的是 HTML 命名空间节点，塞进 SVG 不渲染）
     pupils=document.createElementNS(SVG_NS,'g');
     pupils.setAttribute('class','fish-pupils');pupils.setAttribute('fill','#080909');
     for(const e2 of ells)pupils.append(e2);
     g.append(pupils);
    }
   }
   // 睡眠眼睑层：两道闭眼弧线（与眼白同坐标、覆盖在眼位上），睡眠态淡入
   const whiteG=host.querySelector('g[fill="white"]');
   if(whiteG){
    whiteG.setAttribute('class','fish-eye-open');
    const lids=document.createElementNS(SVG_NS,'g');
    lids.setAttribute('class','fish-eyelids');
    lids.setAttribute('fill','none');
    lids.setAttribute('stroke','#141413');
    lids.setAttribute('stroke-width','14');
    lids.setAttribute('stroke-linecap','round');
    for(const c of [[736,484],[948,485]]){
     const lid=document.createElementNS(SVG_NS,'path');
     lid.setAttribute('d',`M ${c[0]-36} ${c[1]} Q ${c[0]} ${c[1]+30} ${c[0]+36} ${c[1]}`);
     lids.append(lid);
    }
    whiteG.parentElement.append(lids);
    eyeLids=lids;
   }
   if(!pupils)return;
   art.replaceWith(host);art=host;eyePupils=pupils;
   updateEyes();
  }catch(e){}
 }
 function updateEyes(){
  eyeRaf=0;
  if(!eyePupils)return;
  const white=art.querySelector('g[fill="white"] ellipse');
  if(!white)return;
  const wr=white.getBoundingClientRect();
  if(!wr.width)return;
  const cx=wr.left+wr.width/2,cy=wr.top+wr.height/2;
  const dx=eyeTarget.x-cx,dy=eyeTarget.y-cy;
  const d=Math.hypot(dx,dy)||1;
  const f=clamp((d-120)/480,0.15,1);                          // 近处小幅、远处明显
  const ux=dx/d*19*f,uy=dy/d*19*f;                            // 瞳孔在眼白内最大位移（用户单位）
  eyePupils.style.transform=`translate(${ux.toFixed(2)}px,${uy.toFixed(2)}px)`;
 }
 let autoPin=false,autoPinTimer=null;
 const updateAutoPinVis=()=>{if(pinBtn)pinBtn.hidden=!(state==='landed'&&!detailMode&&!reduceMotion());};
 // 持续水平旋转（激活态）：旋转轴 = 头顶叶茎根点（约图 69%W、28%H）延伸的垂直线，
 // rotateY 匀速一整圈循环（透视投影）；激活时创建一次，捏压循环绝不重建——
 // 否则旋转进度被周期性清零（用户复检指出的“转一点就复位”）。
 // 轴点用 transform-origin 相对坐标：origin(0,size) + (0.69s,-0.51s) = 叶茎根点。
 function startSpin(){
  const ax=(0.69*cfg.size).toFixed(1), ay=(-0.51*cfg.size).toFixed(1);
  const P=`perspective(700px) translate(${ax}px, ${ay}px) `;
  const S=`translate(${-parseFloat(ax)}px, ${-parseFloat(ay)}px)`;
  rotateAnim?.cancel();
  rotateAnim=tilt.animate([
   {transform:P+'rotateY(0deg) '+S,offset:0},
   {transform:P+'rotateY(360deg) '+S,offset:1}
  ],{duration:2600,easing:'linear',iterations:Infinity});
 }
 function stopSpin(){
  rotateAnim?.cancel();rotateAnim=null;                        // 停止旋转，回到水平
 }
 // 睡眠态（0425 目检新增）：闭眼动画；激活自动捏捏时暂停循环与旋转，
 // 唤醒后原样恢复。实际睡眠状态的接线由后续会话完成——本事件即预留接口。
 function setSleep(on){
  sleepPaused=!!on;
  art.classList.toggle('is-sleeping',on);
  if(on){stopAutoCycle(false);stopSpin()}
  else if(autoPin&&state==='landed'){startSpin();autoCycle()}
  updateAutoPinVis();
 }
 window.addEventListener('maimai-mascot-sleep',e=>setSleep(Boolean(e.detail&&e.detail.on)));
 function stopAutoCycle(rebound){
  autoPinSeq++;clearTimeout(autoPinTimer);autoPinTimer=null;
  stopSpin();                                                 // 停止持续旋转，回到水平
  if(rebound&&squashAnim){releaseAnim()}
  if(pinBtn)pinBtn.setAttribute('aria-pressed','false');
 }
 function autoCycle(){
  if(!autoPin||sleepPaused||state!=='landed'||reduceMotion()){stopAutoCycle(false);return}
  // 压扁脉冲（计划书原始参数）：只在 art 层，与 tilt 的持续旋转完全独立
  // （旋转在激活时由 startSpin 创建一次，捏压循环绝不重建——否则旋转进度被
  // 周期性清零，即用户复检指出的“转一点就复位”）。
  squashFrames=[{sx:1,sy:1,offset:0},{sx:cfg.squashX,sy:cfg.squashY,offset:1}];
  startSquash(squashFrames,PRESS_MS,'cubic-bezier(.33,.6,.36,1)','forwards');
  squash={sx:cfg.squashX,sy:cfg.squashY};
  scheduleAuto(()=>{releaseAnim();scheduleAuto(autoCycle,250)},PRESS_MS+60);
 }
 if(pinBtn)pinBtn.addEventListener('click',e=>{
  e.stopPropagation();
  autoPin=!autoPin;
  pinBtn.setAttribute('aria-pressed',String(autoPin));
  clearTimeout(autoPinTimer);
  clearTimeout(retreatTimer);retreatTimer=null;                // 清掉激活前已排队的收起定时器
  if(autoPin&&state==='landed'){startSpin();autoCycle()}else if(!autoPin)stopAutoCycle(true);
 });
 const setPoseClass=()=>{button.classList.toggle('is-peek',state!=='landed');updateAutoPinVis()};
 // ---- 捏捏形变：全部 WAAPI；取消/接续都从 JS 记账的当前值出发（同帧接管，无跳帧、无布局读取） ----
 const lerp=(a,b,t)=>a+(b-a)*t;
 function currentSquash(){
  if(!squashAnim)return squash;
  const dur=squashAnim.effect.getTiming().duration;
  const p=typeof dur==='number'&&dur>0?clamp((squashAnim.currentTime??0)/dur,0,1):1;
  let i=1;while(i<squashFrames.length-1&&p>squashFrames[i].offset)i++;
  const a=squashFrames[i-1],b=squashFrames[i];
  const t=(b.offset??1)>(a.offset??0)?clamp((p-(a.offset??0))/((b.offset??1)-(a.offset??0)),0,1):1;
  return {sx:lerp(a.sx,b.sx,t),sy:lerp(a.sy,b.sy,t)};
 }
 function startSquash(frames,duration,easingCurve,fill){
  squashAnim?.cancel();
  squashFrames=frames;
  squashAnim=art.animate(frames.map(f=>({transform:`scale(${f.sx}, ${f.sy})`,offset:f.offset})),
                         {duration,easing:easingCurve,fill:fill||'none'});
  // 保持句柄（fill:forwards 的按压动画结束后仍生效，须可被取消/替换）
  squashAnim.onfinish=()=>{};
 }
 function resetSquash(){squashAnim?.cancel();squashAnim=null;squash={sx:1,sy:1}}
 // ---- 状态机：角落探头 → 弹出中 → 水平落地 →（捏捏/回弹）→ 收起中 → 角落探头 ----
 function finishEmerge(){
  poseAnim=null;state='landed';setPoseClass();
  if(pendingRetreat){pendingRetreat=false;retreat();return}
  if(pendingEmerge){pendingEmerge=false;emerge()}
 }
 function emerge(){
  if(state!=='peek')return;
  clearTimeout(retreatTimer);retreatTimer=null;pendingRetreat=false;
  state='emerging';setPoseClass();
  resetSquash();
  if(reduceMotion()){state='landed';setPoseClass();return}   // 减少动态：短暂稳定的姿态切换
  poseAnim?.cancel();
  poseAnim=tilt.animate([
   {transform:`translate(0px, 0px) ${peekTransform()}`},
   {transform:`translate(8px, -74px) rotate(${-cfg.angle*.3}deg)`,offset:.34},
   {transform:'translate(2px, 8px) rotate(0deg)',offset:.62},
   {transform:'translate(0px, -9px) rotate(0deg)',offset:.8},
   {transform:'translate(0px, 0px) rotate(0deg)'}
  ],{duration:EMERGE_MS,easing:'cubic-bezier(.3,.5,.3,1)'});
  poseAnim.onfinish=finishEmerge;
 }
 function retreat(){
  if(autoPin)return;                                          // 自动捏捏激活：不自动收起
  if(state==='peek'||state==='retreating')return;
  clearTimeout(retreatTimer);retreatTimer=null;
  if(state==='emerging'){pendingRetreat=true;return}         // 出场未完：结束后再次判断收起
  state='retreating';setPoseClass();
  releasePress(true);
  if(autoPin){autoPin=false;stopAutoCycle(false);if(pinBtn)pinBtn.setAttribute('aria-pressed','false')}
  if(reduceMotion()){state='peek';setPoseClass();return}
  resetSquash();
  poseAnim?.cancel();
  poseAnim=tilt.animate([
   {transform:'translate(0px, 0px) rotate(0deg)'},
   {transform:`translate(-5px, 12px) rotate(${-cfg.angle*.45}deg)`,offset:.42},
   {transform:`translate(0px, 0px) ${peekTransform()}`}
  ],{duration:RETREAT_MS,easing:'cubic-bezier(.45,.1,.55,1)'});
  poseAnim.onfinish=()=>{poseAnim=null;state='peek';setPoseClass()};
 }
 // ---- 自动收起：以左下角稳定交互区域判断（不随动画包围盒移动），进出阈值不同防抖 ----
 const cornerDistance=(x,y)=>Math.hypot(x,y-innerHeight);
 function evaluateRetreat(){
  if(detailMode||pressing||autoPin)return;                   // 固定详情/捏着中/自动捏捏激活：不自动收起
  if(state!=='landed'){clearTimeout(retreatTimer);retreatTimer=null;return}
  const d=cornerDistance(lastPointer.x,lastPointer.y);
  if(d>cfg.retreatPx&&!retreatTimer)retreatTimer=setTimeout(retreat,RETREAT_DELAY_MS);
  else if(d<=cfg.retreatPx*.75&&retreatTimer){clearTimeout(retreatTimer);retreatTimer=null}
 }
 let lastPointer={x:-1,y:-1};
 if(matchMedia('(hover:hover) and (pointer:fine)').matches){
  // 精确指针：指向露出的麦麦（距角落小于触发半径）弹出；进入/退出用不同半径防抖。
  document.addEventListener('pointermove',e=>{
   if(e.pointerType!=='mouse'||detailMode)return;
   lastPointer={x:e.clientX,y:e.clientY};
   const d=cornerDistance(e.clientX,e.clientY);
   eyeTarget.x=e.clientX;eyeTarget.y=e.clientY;
   if(!eyeRaf)eyeRaf=requestAnimationFrame(updateEyes);
   if(d>=cfg.size*REARM_FACTOR)hoverArmed=true;
   if(state==='peek'){
    if(hoverArmed&&d<cfg.size*TRIGGER_FACTOR){hoverArmed=false;emerge()}
   }else if(state==='retreating'&&d<cfg.size*TRIGGER_FACTOR){pendingEmerge=true} // 收起中进入触发圈：收起完毕立即弹出
   else evaluateRetreat();
  },{passive:true});
 }
 // 触屏无悬停：点空白收起（弹出/捏捏由按钮自身 pointerdown 承担）。
 document.addEventListener('pointerdown',e=>{
  if(e.pointerType!=='touch'||detailMode||state!=='landed'||button.contains(e.target))return;
  retreat();
 },{passive:true});
 window.addEventListener('blur',()=>{releasePress(true);if(autoPin){autoPin=false;stopAutoCycle(false);if(pinBtn)pinBtn.setAttribute('aria-pressed','false')}});     // 窗口失焦：不永久压扁，停自动捏捏
 document.addEventListener('visibilitychange',()=>{if(document.hidden)releasePress(true)});
 // ---- 捏捏 ----
 function releaseAnim(){
  if(reduceMotion()){squash={sx:1,sy:1};return}
  const cur=currentSquash();
  squashFrames=[
   {sx:cur.sx,sy:cur.sy,offset:0},
   {sx:Math.max(.88,cur.sx*.94),sy:Math.min(1.3,cur.sy+.55*(1-cur.sy)),offset:.2},
   {sx:Math.min(1.06,cur.sx+.3*(1.06-cur.sx)),sy:Math.max(.9,cur.sy-.4*(cur.sy-.9)),offset:.48},
   {sx:1.02,sy:.98,offset:.74},
   {sx:1,sy:1,offset:1}];
  startSquash(squashFrames,cfg.bounceMs,cfg.bounceEasing);   // 从实际压扁程度连续回弹
  squash={sx:1,sy:1};
 }
 function releasePress(silent){
  if(!pressing)return;
  pressing=false;
  button.setAttribute('aria-pressed','false');
  if(silent){resetSquash();return}                           // 取消/失焦：直接回位，不回弹
  releaseAnim();
  if(autoPin)scheduleAuto(autoCycle,400);         // 自动捏捏：手动捏完续播
 }
 button.addEventListener('pointerdown',e=>{
  if(e.button===2||detailMode)return;
  if(state==='peek'){emerge();return}                        // 触屏/点按探头：先弹出落地
  if(state!=='landed')return;                                // 出场/收起中不让按压覆盖轨迹
  autoPinSeq++;                                              // 手动捏取：打断自动链，松开续播
  pressing=true;
  try{button.setPointerCapture(e.pointerId)}catch{}
  button.setAttribute('aria-pressed','true');
  const cur=currentSquash();
  squashFrames=[{sx:cur.sx,sy:cur.sy,offset:0},{sx:cfg.squashX,sy:cfg.squashY,offset:1}];
  // fill:'forwards' 让压扁形态在按住期间保持（松开时从实际压扁程度回弹）
  startSquash(squashFrames,PRESS_MS,'cubic-bezier(.33,.6,.36,1)','forwards'); // 从当前形态渐变压扁
  squash={sx:cfg.squashX,sy:cfg.squashY};
 });
 button.addEventListener('pointerup',()=>releasePress(false));
 button.addEventListener('pointercancel',()=>releasePress(true));
 button.addEventListener('lostpointercapture',()=>releasePress(true));
 // 键盘激活（Enter/Space）：探头=弹出落地，落地=一次回弹；鼠标 click 跟在 pointerup 后，用 detail 区分。
 function playBounce(){
  if(state!=='landed'||reduceMotion())return;
  squashFrames=[{sx:1,sy:1,offset:0},{sx:cfg.squashX*.92,sy:cfg.squashY,offset:.3},{sx:1,sy:1,offset:1}];
  startSquash(squashFrames,Math.min(cfg.bounceMs,620),'ease-in-out');
  squash={sx:1,sy:1};
 }
 button.addEventListener('click',e=>{if(e.detail===0&&!detailMode){state==='peek'?emerge():playBounce()}});
 // ---- 详情气泡期间：桌宠保持可见清晰，只暂停捏捏输入（不再透明消失，避免双交互争指针） ----
 function setDetailMode(on){
  detailMode=on;
  button.classList.toggle('is-above',on);
  if(on){
   releasePress(true);                                  // 仅暂停手动捏取输入
   clearTimeout(retreatTimer);retreatTimer=null;
   // 自动捏捏激活态不被详情打断：循环与旋转继续，关闭详情后原样保留
  }else evaluateRetreat();
  updateAutoPinVis();
 }
 function applyConfig(block){
  const off=block?block.enabled===false:false;
  button.hidden=off;
  if(off)return;
  cfg.angle=clamp(number(block,'peek_angle_deg',45),0,90);
  cfg.retreatPx=clamp(number(block,'hover_out_px',200),60,320);
  cfg.size=clamp(number(block,'mascot_size_px',144),48,192);
  cfg.squashX=clamp(number(block,'press_squash_x',1.2),0.8,1.6);
  cfg.squashY=clamp(number(block,'press_squash_y',0.5),0.2,1);
  cfg.bounceMs=clamp(number(block,'bounce_ms',774),200,3000);
  cfg.bounceEasing=easing(block?block.bounce_easing:undefined);
  const style=button.style;
  style.setProperty('--maimai-mascot-size',cfg.size+'px');
  style.setProperty('--maimai-peek-angle',cfg.angle+'deg');
  const root=document.documentElement.style;
  root.setProperty('--maimai-detail-blur',clamp(number(block,'detail_blur_px',3),0,40)+'px');
  root.setProperty('--maimai-detail-fade-ms',clamp(number(block,'detail_fade_ms',220),80,600)+'ms');
  root.setProperty('--maimai-detail-scale',String(clamp(number(block,'detail_scale_from',0.6),0.4,1)));
  setPoseClass();
  if(eyeRaf)cancelAnimationFrame(eyeRaf);eyeRaf=requestAnimationFrame(updateEyes);
 }
 setupEyes();
 setPoseClass();
 return {applyConfig,setDetailMode,setSleep};
})();
