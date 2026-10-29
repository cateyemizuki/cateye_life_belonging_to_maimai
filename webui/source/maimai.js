/* 常驻麦麦：页面左下角 45° 冒头的互动角色。
   两层结构：.maimai-peek-tilt 只管旋转/位移，.maimai-peek-art 只管按压缩放，
   避免 transform 互相覆盖。参数由 life_dashboard 的 maimai 投影经 applyConfig
   写入 CSS 变量；投影缺失时沿用同值兜底（不另造第二套默认语义）。 */
const Mascot=(()=>{
 const button=$('maimai-peek');
 if(!button)return {applyConfig(){},setAway(){}};
 const DEFAULT_EASING='cubic-bezier(.34,1.56,.64,1)';
 let pressing=false,away=false;
 const reduceMotion=()=>matchMedia('(prefers-reduced-motion: reduce)').matches;
 const clamp=(v,low,high)=>Math.min(high,Math.max(low,v));
 const number=(block,key,fallback)=>{const v=block?block[key]:undefined;return typeof v==='number'&&Number.isFinite(v)?v:fallback};
 const easing=value=>typeof value==='string'&&value.trim()&&/^[\w\s.,%()+\-/]*$/.test(value)&&value.length<=80?value.trim():DEFAULT_EASING;
 function applyConfig(block){
  const off=block?block.enabled===false:false;
  button.hidden=off;
  if(off)return;
  const style=button.style;
  style.setProperty('--maimai-mascot-size',clamp(number(block,'mascot_size_px',96),48,160)+'px');
  style.setProperty('--maimai-peek-angle',clamp(number(block,'peek_angle_deg',45),0,90)+'deg');
  style.setProperty('--maimai-hover-out',clamp(number(block,'hover_out_px',120),0,240)+'px');
  style.setProperty('--maimai-hover-ms',clamp(number(block,'hover_out_ms',240),80,600)+'ms');
  style.setProperty('--maimai-squash-x',clamp(number(block,'press_squash_x',1.2),0.8,1.6));
  style.setProperty('--maimai-squash-y',clamp(number(block,'press_squash_y',0.5),0.2,1));
  style.setProperty('--maimai-bounce-ms',clamp(number(block,'bounce_ms',774),200,3000)+'ms');
  style.setProperty('--maimai-bounce-easing',easing(block?block.bounce_easing:undefined));
  const root=document.documentElement.style;
  root.setProperty('--maimai-detail-blur',clamp(number(block,'detail_blur_px',16),0,40)+'px');
  root.setProperty('--maimai-detail-fade-ms',clamp(number(block,'detail_fade_ms',220),80,600)+'ms');
  root.setProperty('--maimai-detail-scale',String(clamp(number(block,'detail_scale_from',0.96),0.8,1)));
  root.setProperty('--maimai-detail-fish-size',clamp(number(block,'detail_fish_size_px',56),32,96)+'px');
 }
 function playBounce(){
  if(reduceMotion())return;
  button.classList.remove('is-bouncing');
  void button.offsetWidth;
  button.classList.add('is-bouncing');
 }
 button.addEventListener('animationend',e=>{if(e.animationName==='maimai-peek-bounce')button.classList.remove('is-bouncing')});
 button.addEventListener('pointerdown',e=>{
  if(e.button===2||away)return;
  pressing=true;
  try{button.setPointerCapture(e.pointerId)}catch{}
  button.classList.add('is-pressed');
  button.setAttribute('aria-pressed','true');
 });
 const release=()=>{
  if(!pressing)return;
  pressing=false;
  button.classList.remove('is-pressed');
  button.setAttribute('aria-pressed','false');
  playBounce();
 };
 button.addEventListener('pointerup',release);
 const abort=()=>{if(!pressing)return;pressing=false;button.classList.remove('is-pressed');button.setAttribute('aria-pressed','false')};
 button.addEventListener('pointercancel',abort);
 button.addEventListener('lostpointercapture',abort);
 // 键盘激活（Enter/Space）只触发一次回弹；鼠标 click 跟在 pointerup 后，用 detail 区分。
 button.addEventListener('click',e=>{if(e.detail===0&&!away)playBounce()});
 if(matchMedia('(hover:hover) and (pointer:fine)').matches){
  // 精确指针设备：is-out 由指针位置推导，而不是 enter/leave 事件对。
  // 「跳出」会把图移出指针下方，enter→leave 会立刻回缩并抖动；位置推导天然稳定。
  document.addEventListener('pointermove',e=>{
   if(e.pointerType==='touch'||away)return;
   const r=button.getBoundingClientRect();
   const reach=parseFloat(getComputedStyle(button).getPropertyValue('--maimai-hover-out'))||120;
   const inside=e.clientX>=r.left-40&&e.clientX<=r.right+reach
             &&e.clientY>=r.top-reach&&e.clientY<=r.bottom+40;
   button.classList.toggle('is-out',inside&&!pressing);
  },{passive:true});
 }
 function setAway(state){
  away=state;
  button.classList.toggle('is-away',state);
  if(state){
   if(pressing){pressing=false;button.classList.remove('is-pressed');button.setAttribute('aria-pressed','false')}
   button.setAttribute('aria-hidden','true');
   button.tabIndex=-1;
   button.classList.remove('is-out');
  }else{
   button.removeAttribute('aria-hidden');
   button.tabIndex=0;
  }
 }
 return {applyConfig,setAway};
})();
