/* 常驻萨卡班麦麦：左下角桌宠状态机（2026-11-01 U 计划 §三；形象名为萨卡班麦麦）。
   收起探头（原图朝右，展示层 rotate(-角度) 使头朝右上）→ 弹出（先向上弹起、
   再下降落到左下角底部，带落地缓冲；落地时身体水平）→ 落地后接受捏捏（按下
   从当前形态连续压扁、松开从实际压扁程度连续回弹）→ 指针远离交互区域自动收起。
   分层：.maimai-peek-tilt 只管位移/旋转（状态机经 WAAPI 驱动），
   .maimai-peek-art 只管按压缩放（连续形变，可取消接续，不读布局）。
   参数由 life_dashboard 的 maimai 投影经 applyConfig 写入；投影缺失时沿用
   同值兜底（不另造第二套默认语义）。
   形象预设（2026-11-02，[maimai_interaction] preset）：old=萨卡班麦麦（本段
   数值参数照旧只喂它）；new=下方 Bear 模块的麦麦（小熊帽肖像，左下角常驻、
   不探头、固定参数渲染，本段数值不被消费——新形象暂不支持改参数）。
   睡眠接线（2026-11-03）：日程睡眠（panel.js 经 setSleep 投影）时闭眼探头，
   鼠标靠近不弹出；2s 内连点 3 下睁眼、约 1s 后弹出；10s 无互动回睡；
   下次进入页面由数据投影直接入睡。 */
/* 思绪气泡（2026-11-03）：与桌宠**拼体管理**（模仿 DSH 小鲸鱼挂件的泡泡从属关系）——
   气泡元素寄宿进当前形象根节点（麦麦 root / 萨卡班麦麦 button），随形象拖动、
   吸附、缩放一体移动；形象被拉到顶部时气泡自动翻到头下方（被「顶下来」），
   左右越界按视口钳制。尺寸/字号随 --maimai-mascot-size 缩放。
   文案三类轮换（日程 → 状态 → 自定义随机，空类跳过）。
   W-16（2026-11-03）公开版内容准备：不再内置「老大」称呼与「token」占位短句——
   状态/日程文案改为中性称呼（语义不变）；自定义池默认中性，可由生活引擎投影
   data.maimai.bubble_custom（字符串数组）覆盖；i18n 只留结构（文案表按语言分组、
   缺省回落 zh，宿主 context.language 预留，当前仅 zh）。 */
const Bubble=(()=>{
  const root=document.getElementById('maimai-bubble');
  if(!root)return{show(){},hide(){},toggle(){},sync(){}};
  const textEl=root.querySelector('.maimai-bubble-text');
  const TTL_MS=8000;                                     // 有文案后的驻留时长
  let hideTimer=null,seq=0,turn=0,lastCustom=-1,host=null,flip=false,closing=false;
  // i18n 结构（W-16）：语言只读一次，缺省 zh；新增语言时在 TEXTS 补一组即可，不改轮换逻辑。
  const LANG=(()=>{try{return (window.maibot&&window.maibot.context&&window.maibot.context.language)||'zh'}catch(e){return 'zh'}})();
  // ① 自定义随机文案池（W-16）：公开版不再内置「老大，我想吃token了」占位。
  //    默认仅中性短句；data.maimai.bubble_custom 提供字符串数组时优先使用
  //    （可配置入口，后端暂未投影该字段，缺省回落）；池为空时该类自动跳过。
  const CUSTOM_ZH=['好麦麦......'];
  const customPool=()=>{
    try{
      const c=data&&data.maimai&&data.maimai.bubble_custom;
      if(Array.isArray(c)){const list=c.filter(x=>typeof x==='string'&&x.trim());if(list.length)return list}
    }catch(e){}
    return CUSTOM_ZH;
  };
  // ② 状态文案：精力/饥饿/专注/压力/社交电量，五项各按 ≥0.5 记「高」、否则「低」，
  //    键=五字组合（顺序固定：精力/饥饿/专注/压力/社交电量，如「高低低高高」）。
  //    全部留白待回填（2^5=32 组，见 开发文档/状态文案回填表.md）；空串自动跳过。
  const TEXTS={
    zh:{
     state:{
    // 2026-11-03 用户回填（源表：开发文档/状态文案回填表.md；改文案请回该表或直接改这里）。
    // W-16：原稿中的「老大」称呼按公开版口径改为中性文案（语气与语义不变）。
    /* 1*/ '低低低低低':'我先瘫一会儿……',
    /* 2*/ '低低低低高':'有点犯困了，陪我随便唠两句呗。',
    /* 3*/ '低低低高低':'脑袋乱糟糟的……让我缓缓。',
    /* 4*/ '低低低高高':'心里堵得慌，想找你说说。',
    /* 5*/ '低低高低低':'我想安安静静弄会儿，累了再歇。',
    /* 6*/ '低低高低高':'我这会儿还想聊，就是有点犯困。',
    /* 7*/ '低低高高低':'我还想再专心一会儿，就是有点绷。',
    /* 8*/ '低低高高高':'事情在脑子里转个不停，陪我捋捋。',
    /* 9*/ '低高低低低':'饿得我都懒得动了。',
    /*10*/ '低高低低高':'饿得没劲了，陪我聊点吃的嘛。',
    /*11*/ '低高低高低':'饿着肚子更烦了，我先缩会儿。',
    /*12*/ '低高低高高':'我饿得有点烦，想听你说说话。',
    /*13*/ '低高高低低':'脑子还在转，肚子先叫起来了。',
    /*14*/ '低高高低高':'我还能跟你琢磨一会儿，就是有点饿。',
    /*15*/ '低高高高低':'我正绷着呢，肚子还偏偏来捣乱。',
    /*16*/ '低高高高高':'我有点饿，也有点急，想找你商量商量。',
    /*17*/ '高低低低低':'这会儿只想自己溜溜神。',
    /*18*/ '高低低低高':'脑袋跑神了，嘴巴还想跟你唠。',
    /*19*/ '高低低高低':'一身劲儿没处使，心里还乱糟糟的。',
    /*20*/ '高低低高高':'心里毛毛躁躁的，想找你倒倒豆子。',
    /*21*/ '高低高低低':'脑子转得挺顺，让我安静琢磨会儿。',
    /*22*/ '高低高低高':'我现在状态正好，想跟你一起琢磨点事。',
    /*23*/ '高低高高低':'越想越绷，我得慢慢松口气。',
    /*24*/ '高低高高高':'脑子转得停不下来，想拉你一起捋一捋。',
    /*25*/ '高高低低低':'满脑子都是吃的，别的先放一边。',
    /*26*/ '高高低低高':'满脑子都是吃的，陪我唠唠嘛。',
    /*27*/ '高高低高低':'饿得心里发毛，我想自己缓缓。',
    /*28*/ '高高低高高':'我饿得坐不住了，陪我说两句嘛。',
    /*29*/ '高高高低低':'我正专心呢，肚子咕噜一声把我出卖了。',
    /*30*/ '高高高低高':'脑子正灵光呢，想边聊边嚼点什么。',
    /*31*/ '高高高高低':'脑子绷得紧，肚子还在叫，我得缓口气。',
    /*32*/ '高高高高高':'我有一肚子话，饿着又急，想一股脑讲给你。',
     }
    }
  };
  const stateTable=()=>(TEXTS[LANG]&&TEXTS[LANG].state)||TEXTS.zh.state;
  const clamp=(v,lo,hi)=>Math.min(hi,Math.max(lo,v));
  const isOpen=()=>!root.hidden;
  function stateKey(){                                   // 当前生活状态五项高低组合
    try{
      const s=data&&data.state;if(!s)return '';
      const v=k=>{const x=Number(s[k]);return Number.isFinite(x)&&x>=.5?'高':'低'};
      return v('energy')+v('hunger')+v('focus')+v('stress')+v('social_battery');
    }catch(e){return ''}
  }
  function scheduleText(){                               // 现在要干嘛 / 一会去干嘛
    try{
      const sch=(data&&data.schedule)||{};
      if(sch.current&&sch.current.name)return '我正在「'+sch.current.name+'」呢';
      const intents=Array.isArray(sch.intents)?sch.intents:[];
      const next=intents.find(r=>r&&r.name&&r.status==='planned');
      if(next)return '一会我要去「'+next.name+'」啦';
    }catch(e){}
    return '';
  }
  function customText(){                                 // 随机、不连续重复（W-16：可配置池，空池跳过）
    const pool=customPool();
    if(!pool.length)return '';
    let i=Math.floor(Math.random()*pool.length);
    if(pool.length>1&&i===lastCustom)i=(i+1)%pool.length;
    lastCustom=i;return pool[i];
  }
  function pick(){                                       // 轮换取文：日程→状态→自定义
    for(let n=0;n<3;n++){
      const k=(turn+n)%3;let t='';
      if(k===0)t=scheduleText();
      else if(k===1)t=stateTable()[stateKey()]||'';
      else t=customText();
      if(t){turn=(k+1)%3;return t}
    }
    turn=0;return customText();
  }
  function place(){                                      // 寄宿坐标：思考点对准头心；近顶翻到下方；视口钳制（镜像感知）
    if(!host)return;
    // 一律用布局坐标（offset*）：gBCR 在根翻转过渡期间会返回变形中的盒子
    const hx=host.offsetLeft,hy=host.offsetTop,hw=host.offsetWidth||144,hh=host.offsetHeight||hw;
    const w=root.offsetWidth||hw*1.53,h=root.offsetHeight||w*.875;
    const vw=Math.max(1,innerWidth);
    // 思考点恒用 44%（镜像后的换边由根镜像自动完成），但钳制盒必须按 flip 分两种：
    // 根节点镜像（scaleX(-1)）时，泡内布局坐标 L 的视口位置 = hx+hw-L-w；
    // 未镜像时视口位置 = hx+L。此前两种都按镜像盒钳制，导致老鱼（flip=false）
    // 气泡左移出屏、右吸附新熊气泡右移出屏。
    const desired=hw/2-w*.44;
    let lo,hi;
    if(flip){
     lo=hx+hw-(vw-8);hi=Math.max(lo,hx+hw-w-8);
    }else{
     lo=8-hx;hi=Math.max(lo,vw-8-hx-w);
    }
    const left=clamp(desired,lo,hi);
    root.style.left=left+'px';
    if(hy<h-hh*.84+8){                                   // 顶上放不下：翻到头下方（顶下来）
     root.classList.add('below');                        // 泡图垂直镜像：思考点朝上连着桌宠
     root.style.top=(hh*.99)+'px';root.style.bottom='auto';
    }else{
     root.classList.remove('below');
     root.style.bottom=(hh*.84)+'px';root.style.top='auto';
    }
  }
  function show(h,f,announce){
    attach(h);flip=!!f;
    measure();
    const g=++seq;
    clearTimeout(hideTimer);hideTimer=null;closing=false;
    root.hidden=false;
    // W-12 气泡可读性（2026-11-03）：打开时撤掉根节点 aria-hidden，让辅助技术能读到
    // 文案；host（形象）同步 aria-expanded。announce=true 仅用于用户主动点开的气泡
    // （避免自动弹出时打扰屏幕阅读器），此时文本层以 aria-live=polite 播报。
    root.removeAttribute('aria-hidden');
    if(host){host.setAttribute('aria-expanded','true');host.setAttribute('aria-controls','maimai-bubble')}
    if(textEl){
     if(announce)textEl.setAttribute('aria-live','polite');
     else textEl.removeAttribute('aria-live');
    }
    const t=pick();
    if(t&&textEl){
      textEl.textContent=t;textEl.hidden=false;
      // 长句缩字号：系数相对气泡宽度（--maimai-bubble-size，随形象缩放）——
      // 移动端气泡独立缩放后字号自动跟随，不再按形象基准错算（W-12）。
      const base=.082,fa=t.length>40?.059:t.length>26?.069:base;
      textEl.style.setProperty('--bub-fa',String(fa));
    }else if(textEl)textEl.hidden=true;
    root.style.transformOrigin='44% 96%';
    place();
    void root.offsetWidth;                               // 强制回流锁定基态（后台标签页 rAF 会节流）
    // 长文案自适应：若文本超出安全框高度，逐步缩字号直到放下（有下限），
    // 避免靠溢出滚动/撞边框容纳长句；短文案保持原字号居中。
    if(textEl&&!textEl.hidden){
      let fa=parseFloat(textEl.style.getPropertyValue('--bub-fa'))||.082;
      for(let i=0;i<14&&fa>.04&&textEl.scrollHeight>textEl.clientHeight+1;i++){
        fa*=.88;textEl.style.setProperty('--bub-fa',fa.toFixed(4));
      }
    }
    root.classList.add('is-open');
    hideTimer=setTimeout(()=>{if(g===seq)hide()},TTL_MS);
  }
  // 实测椭圆视觉中心与内腔宽（挂件 measureBubbleCenter 的位图版）：
  // 扫描泡图 alpha 通道，取最宽不透明行（=椭圆水平直径）的中点为中心，
  // 不再靠固定 top% 猜锚点。仅首successful 测量前反复尝试（图片未解码完时跳过）。
  let measured=false;
  function measure(){
    if(measured)return;
    try{
      const img=root.querySelector('.maimai-bubble-img');
      if(!img||!img.complete||!img.naturalWidth)return;
      const w=img.naturalWidth,h=img.naturalHeight;
      const c=document.createElement('canvas');c.width=w;c.height=h;
      const ctx=c.getContext('2d',{willReadFrequently:true});
      if(!ctx)return;
      ctx.drawImage(img,0,0);
      const d=ctx.getImageData(0,0,w,h).data;
      const opaque=(x,y)=>d[(y*w+x)*4+3]>60;
      let best={y:-1,len:0,x0:0,x1:0};
      for(let y=0;y<h;y+=2){
       let run=-1,maxlen=0,bx0=0,bx1=0;
       for(let x=0;x<=w;x++){
        if(x<w&&opaque(x,y)){if(run<0)run=x}
        else if(run>=0){const len=x-run;if(len>maxlen){maxlen=len;bx0=run;bx1=x}run=-1}
       }
       if(maxlen>best.len)best={y,len:maxlen,x0:bx0,x1:bx1};
      }
      if(best.len<w*.5)return;                           // 太窄说明图还没解码好，下次再测
      // 垂直中心/内腔高：沿水平直径中点列取「包含赤道行的连续不透明段」，
      // 遇到间隙即停（排除下方分离的思考点尾巴），避免尾巴把中心拽偏。
      const cxpx=Math.round((best.x0+best.x1)/2);
      let y0=best.y,y1=best.y;
      while(y0>0&&opaque(cxpx,y0-1))y0--;
      while(y1<h-1&&opaque(cxpx,y1+1))y1++;
      const eh=y1-y0+1;
      measured=true;
      root.style.setProperty('--bub-cx',((best.x0+best.x1)/2/w*100).toFixed(2)+'%');
      root.style.setProperty('--bub-cy',(((y0+y1)/2)/h*100).toFixed(2)+'%');
      // 椭圆内接安全框：宽取直径72%、高取内腔高62%，矩形四角留在椭圆内，长文案不撞边。
      root.style.setProperty('--bub-boxw',(best.len/w*72).toFixed(2)+'%');
      root.style.setProperty('--bub-boxh',(eh/h*62).toFixed(2)+'%');
    }catch(e){}
  }
  function attach(h){                                    // 寄宿进形象根节点（换形象时重挂）
    if(h)host=h;
    if(host&&root.parentElement!==host){
      host.appendChild(root);                            // sync 可能已更新 host 变量：以实际挂载为准
      if(!root.hidden){                                  // 换宿主时旧开启态作废：收起，避免点新形象变成关旧泡
        const g=++seq;clearTimeout(hideTimer);hideTimer=null;closing=false;
        root.hidden=true;root.classList.remove('is-open');
      }
    }
  }
  function hide(){
    const g=++seq;
    clearTimeout(hideTimer);hideTimer=null;
    if(root.hidden)return;
    closing=true;                                        // 收尾中（220ms）再点按视为「重开」而非再关
    root.classList.remove('is-open');
    // W-12：关闭即对辅助技术重新隐藏，并把 host 的气泡开关状态复位。
    root.setAttribute('aria-hidden','true');
    if(host)host.setAttribute('aria-expanded','false');
    hideTimer=setTimeout(()=>{if(g===seq){root.hidden=true;closing=false}},220);
  }
  function toggle(h,f,announce){(isOpen()&&!closing)?hide():show(h,f,announce)}
  function sync(h,f){if(h)host=h;attach(host);flip=!!f;if(isOpen())place()}
  window.addEventListener('resize',()=>{if(isOpen())place()});
  return{show,hide,toggle,sync};
})();
/* 麦麦桌宠 Bear（preset=new）：小熊帽肖像，左下角常驻、不探头。
   捏捏与拖动复刻 DSH 小鲸鱼挂件（dsh-whale-widget）的手感：
   - 捏捏：按住加 .is-pressed（scaleY(.88) scaleX(1.05)），松开去掉，由 transform
     transition .22s cubic-bezier(.34,1.56,.64,1) 过冲弹回；原点贴底中心。
   - 拖动：按住即拖（位移 <10px 算点按），拖动中 .is-dragging 关过渡；松手按
     吸附区归位（左右 10%、底 15%、顶边禁用——同挂件 v3 默认），锚点以
     「贴边+净距离」存 localStorage('maimai-bear-pos')，窗口缩放按锚点重算；
     取消/丢坐标回原位不落盘（同挂件）。
   - 镜像翻转（2026-11-03 追加，同挂件语义）：贴左必翻、贴右不翻（朝向屏幕内），
     自由摆放按中心 x 是否在左半屏判定；翻转态随锚点落盘（fl 字段）并随恢复重现。
   - 不消费 maimai_interaction 数值参数；位置存取失败（隐私模式等）退化为会话内记忆。 */
const Bear=(()=>{
  const root=document.getElementById('maimai-bear');
  // finding #4（2026-11-04）：stub 补 no-op reanchor，与真实接口口径一致（调用方虽已判空）。
  if(!root)return{setEnabled(){},setDetailMode(){},setSleep(){},reanchor(){}};
  const img=root.querySelector('.maimai-bear-img');
  const POS_KEY='maimai-bear-pos';
  const CLICK_SQ=100;                                    // 位移平方 <100（<10px）= 点按
  const SNAP={L:.20,R:.20,B:.30,T:0};                    // 吸附区比例（挂件 v3 默认 10/10/15 偏小，2026-11-03 按反馈加大）；T=0 顶边禁用
  let enabled=false,detailMode=false,drag=null,flip=false;
  let memPos=null;                                       // localStorage 不可用时的会话内兜底
  const clamp=(v,lo,hi)=>Math.min(hi,Math.max(lo,v));
  const vp=()=>({w:Math.max(1,innerWidth),h:Math.max(1,innerHeight)});
  function loadPos(){
    try{const a=JSON.parse(localStorage.getItem(POS_KEY)||'null');
      // 两轴独立校验：h/vt 允许 null（自由摆放，hd/vd 即距左/距顶距离）；fl（翻转态）可选布尔
      if(a&&a.v===1&&Number.isFinite(a.hd)&&Number.isFinite(a.vd)&&
         (a.h==='left'||a.h==='right'||a.h===null)&&
         (a.vt==='top'||a.vt==='bottom'||a.vt===null))return a;
    }catch(e){}
    return memPos;
  }
  function savePos(p){memPos=p;try{localStorage.setItem(POS_KEY,JSON.stringify(p))}catch(e){}}
  function applyFlip(){root.classList.toggle('is-flipped',flip)}
  function applyPos(p){                                  // 锚点 → left/top；越界距离夹回自愈
    const w=root.offsetWidth||144,h=root.offsetHeight||144,v=vp();
    const hd=clamp(p.hd,0,Math.max(0,v.w-w)),vd=clamp(p.vd,0,Math.max(0,v.h-h));
    // 锚点语义：贴右 = hd 为距右距离、贴底 = vd 为距底距离；自由摆放 = hd/vd 为距左/距顶距离
    root.style.left=(p.h==='right'?v.w-hd-w:hd)+'px';
    root.style.top=(p.vt==='bottom'?v.h-vd-h:vd)+'px';
    root.style.bottom='auto';
    // 翻转：贴左必翻、贴右不翻（同挂件「朝向屏幕内」）；自由位按存档，无档不翻
    flip=p.h==='left'?true:p.h==='right'?false:p.fl===true;
    applyFlip();
    Bubble.sync(root,flip);                              // 气泡开着时跟随锚点重新定位
  }
  function applyAnchors(){
    const p=loadPos();
    if(p){applyPos(p);return}
    flip=true;applyFlip();                               // 无存位的左下角默认位=贴左 → 翻转（同挂件语义）
    Bubble.sync(root,flip);
  }
  function restorePos(){                                 // 回锚点位；无锚点回 CSS 默认（取消/点按路径）
    if(loadPos())applyAnchors();
    else{root.style.left='';root.style.top='';root.style.bottom=''}
  }
  function settle(left,top){                             // 松手：夹回视口 → 吸附区判定 → 锚点落盘
    const w=root.offsetWidth||144,h=root.offsetHeight||144,v=vp();
    left=clamp(left,0,Math.max(0,v.w-w));top=clamp(top,0,Math.max(0,v.h-h));
    const cx=left+w/2,footY=top+h;
    const b={L:v.w*SNAP.L,R:v.w*(1-SNAP.R),B:v.h*(1-SNAP.B),T:v.h*SNAP.T};
    const p={v:1,
      h:cx<b.L?'left':cx>b.R?'right':null,hd:0,
      vt:footY>b.B?'bottom':SNAP.T>0&&top<b.T?'top':null,vd:0};
    if(p.h)p.hd=p.h==='left'?left:v.w-left-w;else p.hd=left;
    if(p.vt)p.vd=p.vt==='bottom'?v.h-top-h:top;else p.vd=top;
    // 翻转判定（同挂件 settle）：贴左翻、贴右正，自由位看中心 x 与半屏线
    flip=p.h==='left'?true:p.h==='right'?false:cx<v.w/2;
    p.fl=flip;
    savePos(p);applyPos(p);
  }
  function press(on){root.classList.toggle('is-pressed',on);root.setAttribute('aria-pressed',String(on))}
  function onMove(e){
    if(!drag||!drag.active)return;
    const dx=e.clientX-drag.startX,dy=e.clientY-drag.startY;
    if(dx*dx+dy*dy>=CLICK_SQ)drag.moved=true;
    // 全程钳制在视口内（禁止出界；松手 settle 会再做一次吸附判定）
    root.style.left=clamp(drag.origLeft+dx,0,Math.max(0,innerWidth-drag.w))+'px';
    root.style.top=clamp(drag.origTop+dy,0,Math.max(0,innerHeight-drag.h))+'px';
    Bubble.sync(root,flip);                              // 气泡开着时跟随拖动
  }
  function endDrag(e,cancelled){
    if(!drag||!drag.active)return;
    drag.active=false;
    document.removeEventListener('pointermove',onMove,true);
    document.removeEventListener('pointerup',onUp,true);
    document.removeEventListener('pointercancel',onCancel,true);
    press(false);
    root.classList.remove('is-dragging');
    const noCoord=!e||typeof e.clientX!=='number'||!Number.isFinite(e.clientX);
    if(cancelled||noCoord){                              // 取消/丢坐标：回原位、不落盘（同挂件）
      restorePos();
      return;
    }
    if(!drag.moved){                                     // 点按（<10px，含鼠标抖动）：回原位 + 开/关气泡
      restorePos();
      Bubble.toggle(root,flip,true);                     // 用户主动点开：允许屏幕阅读器播报（W-12）
      return;
    }
    settle(drag.origLeft+e.clientX-drag.startX,drag.origTop+e.clientY-drag.startY);
  }
  function onUp(e){endDrag(e,false)}
  function onCancel(e){endDrag(e,true)}
  // 丢 pointerup 自愈（同挂件 onDocPointerMoveCursor）：拖动中 pointermove 带 buttons===0
  // = 按钮已松开但抬起事件丢了（指针移出窗口等）→ 按最后一次坐标收尾，别让形象黏在半空。
  document.addEventListener('pointermove',e=>{if(drag&&drag.active&&!e.buttons)endDrag(e,false)},{passive:true});
  // 双保险：任何新的按下（含落点不在形象上的）都先取消遗留拖拽——拖拽只有一个指针会话，
  // 新按下 = 旧会话必死（真实输入必有 up/cancel，会先走正常路径；这里只兜异常丢失）。
  document.addEventListener('pointerdown',()=>{if(drag&&drag.active)endDrag(null,true)},true);
  // 窗口失焦/页面隐藏时拖拽不可能再收到抬起：按取消收尾（回原位、不落盘）。
  window.addEventListener('blur',()=>{if(drag&&drag.active)endDrag(null,true)});
  document.addEventListener('visibilitychange',()=>{if(document.hidden&&drag&&drag.active)endDrag(null,true)});
  img.addEventListener('pointerdown',e=>{
    if(!enabled||detailMode||(e.pointerType==='mouse'&&e.button!==0))return;
    if(drag&&drag.active)endDrag(null,true);             // 上一次拖拽未收尾（丢抬起）：先取消再开新的
    e.preventDefault();                                  // 防选中/原生拖图；touch 滚动由 touch-action:none 承担
    // 拖拽起点用布局坐标（offset*）：翻转过渡期间 gBCR 是变形中的盒子，会把起点算歪、错过吸附带
    drag={active:true,startX:e.clientX,startY:e.clientY,
          origLeft:root.offsetLeft,origTop:root.offsetTop,moved:false,
          w:root.offsetWidth,h:root.offsetHeight};
    root.classList.add('is-dragging');
    press(true);                                         // 拖动期间保持捏压（同挂件 pressDown）
    document.addEventListener('pointermove',onMove,true);
    document.addEventListener('pointerup',onUp,true);
    document.addEventListener('pointercancel',onCancel,true);
  });
  root.addEventListener('keydown',e=>{                  // 键盘捏捏：Enter/Space 一次快速压弹
    if(!enabled||detailMode||(e.key!=='Enter'&&e.key!==' '))return;
    e.preventDefault();press(true);setTimeout(()=>press(false),140);
    // W-12：root 是 role=button/tabindex=0，键盘激活应与点按一致——同时开/关气泡。
    if(!e.repeat)Bubble.toggle(root,flip,true);
  });
  function setEnabled(on){
    on=!!on;
    // 幂等：30 秒刷新/maibot:context 每次都会走 applyConfig→applyVisibility，
    // 不能重复 endDrag/press/applyAnchors，否则正在拖拽或按住时会被强制回位。
    if(on===enabled&&root.hidden===!on)return;
    enabled=on;
    root.hidden=!enabled;
    press(false);
    if(!enabled)Bubble.hide();                           // 预设切走/总开关关：气泡一并收起
    if(drag&&drag.active)endDrag(null,true);             // 预设切走时还没松手：按取消收尾
    if(enabled)applyAnchors();
  }
  function setDetailMode(on){
    detailMode=!!on;
    root.classList.toggle('is-above',on);
    if(on){press(false);Bubble.hide();if(drag&&drag.active)endDrag(null,true)}
  }
  /* W-13 新熊睡眠态（2026-11-03）：日程睡眠（panel.js setSleep 投影）时降饱和+亮度
     呼吸 + 头顶 Zzz 徽标——没有闭眼素材，不做假闭眼；捏捏/拖动/点开气泡照旧
     （睡着也能被点醒聊天），睡眠只是视觉状态。 */
  let sleepVisual=false;
  const zzz=document.createElement('span');
  zzz.className='maimai-bear-zzz';zzz.setAttribute('aria-hidden','true');zzz.textContent='Zz';
  root.append(zzz);
  function setSleep(on){
    on=!!on;
    if(on===sleepVisual)return;
    sleepVisual=on;
    root.classList.toggle('is-sleeping',on);
  }
  window.addEventListener('resize',()=>{if(enabled){applyAnchors();Bubble.sync(root,flip)}});
  // 尺寸热重载：只重算锚点，不打断拖拽/按压（setEnabled 已幂等）。
  function reanchor(){if(enabled&&!(drag&&drag.active))applyAnchors()}
  return{setEnabled,setDetailMode,setSleep,reanchor};
})();
const Mascot=(()=>{
 const button=$('maimai-peek');
 // finding #4（2026-11-04）：早退 stub 补全消费方接口（panel.js setSleep/isLocalHidden/
 // setLocalHidden、fish.js setDetailMode），DOM 缺失时不再抛 TypeError 中断渲染。
 if(!button)return {applyConfig(){},setDetailMode(){},setSleep(){},setLocalHidden(){},isLocalHidden(){return false}};
 const tilt=button.querySelector('.maimai-peek-tilt');
 let art=button.querySelector('.maimai-peek-art');
 const DEFAULT_EASING='cubic-bezier(.34,1.56,.64,1)';
 // 萨卡班麦麦固定参数（2026-11-03 撤出配置）：取小主机生产 config.toml 现值固化。
 const FIXED={angle:12,retreatPx:200,squashX:1.2,squashY:.5,bounceMs:774,easing:DEFAULT_EASING,
              detailBlur:3,detailFade:220,detailScale:.6};
 // 运动常数：U 计划 §3.1 建议值（出场 450–650ms、收起延迟约 200ms），调试起点，非业务规则。
 // PRESS_MS=按下渐变压扁时长（用户 0424 目检：按下要"逐渐变扁"并保持，松开才回弹）。
 const EMERGE_MS=560,RETREAT_MS=320,RETREAT_DELAY_MS=200,PRESS_MS=260;
 // 触发圈系数（0425 目检：缩小一圈）；REARM=解除武装（重新武装阈值）。
 const TRIGGER_FACTOR=1.15,REARM_FACTOR=1.35;
 const reduceMotion=()=>matchMedia('(prefers-reduced-motion: reduce)').matches;
 const clamp=(v,low,high)=>Math.min(high,Math.max(low,v));
 const number=(block,key,fallback)=>{const v=block?block[key]:undefined;return typeof v==='number'&&Number.isFinite(v)?v:fallback};
 const cfg={...FIXED,size:144};
 let activePreset='old';           // 形象预设：old=探头小鱼 | new=小熊帽肖像（Bear 模块）
 // W-12 本地「收起麦麦」（2026-11-03）：不动服务端 maimai_interaction.enabled，仅在
 // 本地隐藏两个形象（localStorage 尽力而为，隐私模式降级为会话内记忆）。「更多」菜单
 // 的收起/显示按钮经 setLocalHidden/isLocalHidden 驱动。
 let localHidden=false,lastOff=false;
 try{localHidden=localStorage.getItem('maimai-mascot-hidden')==='true'}catch(e){}
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
 let eyePupils=null,eyeLids=null,eyeRaf=0;
 // 睡眠接线（2026-11-03）：sleepOn=日程睡眠投影（panel.js setSleep）；
 // awake=被点醒（临时），10s 无互动回睡。睡着=sleepOn&&!awake。
 let sleepOn=false,awake=false,sleepTaps=0,sleepTapTimer=null,sleepIdleTimer=null;
 const asleepNow=()=>sleepOn&&!awake;
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
 // W-13 评审修正（2026-11-03）：睡眠中不显示自动捏捏按钮（asleepNow 检查），
 // 避免「按钮隐藏但内部 autoPin 仍激活」的显示与状态机不一致（仅影响 preset=old）。
 const updateAutoPinVis=()=>{if(pinBtn)pinBtn.hidden=!(activePreset==='old'&&state==='landed'&&!detailMode&&!reduceMotion()&&!asleepNow());};
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
 // 睡眠态（2026-11-03 正式接线）：日程睡眠时闭眼探头、鼠标靠近不弹出；
 // 2s 内连点 3 下睁眼、约 1s 后弹出；10s 无互动回睡；下次进入页面由数据投影入睡。
 function setSleep(on){
  sleepOn=!!on;
  if(!sleepOn){                                          // 日程醒了：彻底清醒（清除点醒冷却）
   awake=false;clearTimeout(sleepIdleTimer);sleepIdleTimer=null;
   art.classList.remove('is-sleeping');updateAutoPinVis();return}
  if(!awake){                                            // 睡眠中（数据刷新幂等）：闭眼、收回探头
   art.classList.add('is-sleeping');
   // W-13 评审修正（2026-11-03）：入睡时彻底关闭自动捏捏——原先 stopAutoCycle 只清
   // 定时器不复位 autoPin，retreat() 又被 autoPin 提前拦截，闭眼鱼会保持落地且
   // aria-pressed 与内部状态不一致。复位后 retreat 不受阻；醒来由 updateAutoPinVis
   // 恢复按钮，可重新开启（不动捏捏/自动捏捏本身的手感参数）。
   if(autoPin){autoPin=false;if(pinBtn)pinBtn.setAttribute('aria-pressed','false')}
   stopAutoCycle(false);
   if(state!=='peek')retreat();
  }
  updateAutoPinVis();
 }
 function goSleep(){                                    // 互动冷却结束：回去睡觉
  awake=false;sleepTaps=0;
  if(autoPin){autoPin=false;if(pinBtn)pinBtn.setAttribute('aria-pressed','false')}   // W-13 评审修正：入睡即关自动捏捏（同 setSleep）
  releasePress(true);stopAutoCycle(false);
  art.classList.add('is-sleeping');
  Bubble.hide();
  if(state!=='peek')retreat();
  updateAutoPinVis();
 }
 function wakeUp(){                                     // 点醒：睁眼，约 1s 后弹出
  awake=true;sleepTaps=0;
  art.classList.remove('is-sleeping');
  armSleepIdle();
  setTimeout(()=>{if(awake&&state==='peek')emerge()},1000);
 }
 function tapWake(){                                    // 睡觉中连点 3 下=醒（2s 内连续算「连续」）
  sleepTaps++;clearTimeout(sleepTapTimer);
  sleepTapTimer=setTimeout(()=>{sleepTaps=0},2000);
  if(sleepTaps>=3){clearTimeout(sleepTapTimer);wakeUp()}
 }
 function armSleepIdle(){                               // 醒着的互动冷却（仅日程睡眠时有效）
  clearTimeout(sleepIdleTimer);
  if(sleepOn&&awake)sleepIdleTimer=setTimeout(()=>{if(sleepOn&&awake)goSleep()},10000);
 }
 window.addEventListener('maimai-mascot-sleep',e=>setSleep(Boolean(e.detail&&e.detail.on)));
 function stopAutoCycle(rebound){
  autoPinSeq++;clearTimeout(autoPinTimer);autoPinTimer=null;
  stopSpin();                                                 // 停止持续旋转，回到水平
  if(rebound&&squashAnim){releaseAnim()}
  if(pinBtn)pinBtn.setAttribute('aria-pressed','false');
 }
 function autoCycle(){
  // W-13 修复（2026-11-03）：原条件引用了未声明变量 sleepPaused，点击自动捏捏会抛
  // ReferenceError 并中断循环。睡眠接线（2026-11-03）后，日程睡眠本就该停自动捏捏
  // （setSleep/goSleep 已断链），这里补 asleepNow() 判断兜住「睡眠中链被重新续播」
  // 的路径（如睡眠时手动捏取松开续播），语义等价于原 sleepPaused 的意图。
  if(!autoPin||asleepNow()||state!=='landed'||reduceMotion()){stopAutoCycle(false);return}
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
  Bubble.show(button,false);                           // 落地打个招呼（文案三类轮换）
  if(pendingRetreat){pendingRetreat=false;retreat();return}
  if(pendingEmerge){pendingEmerge=false;emerge()}
 }
 function emerge(){
  if(activePreset!=='old')return;                            // 新形象预设：旧小鱼整个状态机停用
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
  Bubble.hide();                                              // 收起时气泡一并收起
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
    if(!asleepNow()&&hoverArmed&&d<cfg.size*TRIGGER_FACTOR){hoverArmed=false;emerge()}   // 睡觉中不弹出
   }else if(!asleepNow()&&state==='retreating'&&d<cfg.size*TRIGGER_FACTOR){pendingEmerge=true} // 收起中进入触发圈：收起完毕立即弹出
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
  Bubble.show(button,false,true);                      // 捏一下换一句（用户主动交互，播报；W-12）
  if(autoPin)scheduleAuto(autoCycle,400);         // 自动捏捏：手动捏完续播
 }
 button.addEventListener('pointerdown',e=>{
  if(e.button===2||detailMode)return;
  if(state==='peek'){
   if(asleepNow()){tapWake();return}                         // 睡觉中：连点计数，不弹出也不捏
   emerge();return}                                          // 触屏/点按探头：先弹出落地
  armSleepIdle();                                            // 醒着互动：刷新 10s 回睡冷却
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
  Bubble.show(button,false,true);                      // 键盘捏一下同样换一句（主动交互，播报；W-12）
 }
 button.addEventListener('click',e=>{if(e.detail===0&&!detailMode){
  if(state==='peek'){asleepNow()?tapWake():emerge()}         // 键盘：睡觉中连按唤醒
  else{armSleepIdle();playBounce()}}});
 // ---- 详情气泡期间：桌宠保持可见清晰，只暂停捏捏输入（不再透明消失，避免双交互争指针） ----
 function setDetailMode(on){
  detailMode=on;
  button.classList.toggle('is-above',on);
  Bear.setDetailMode(on);                                    // 新形象：详情期间同样暂停捏捏/拖动输入
  if(on){
   releasePress(true);                                  // 仅暂停手动捏取输入
   Bubble.hide();                                       // 详情期间气泡收起
   clearTimeout(retreatTimer);retreatTimer=null;
   // 自动捏捏激活态不被详情打断：循环与旋转继续，关闭详情后原样保留
  }else evaluateRetreat();
  updateAutoPinVis();
 }
 function applyVisibility(){
  button.hidden=lastOff||activePreset!=='old'||localHidden;
  Bear.setEnabled(!lastOff&&activePreset==='new'&&!localHidden);
 }
 function setLocalHidden(on){
  localHidden=!!on;
  try{localStorage.setItem('maimai-mascot-hidden',String(localHidden))}catch(e){}
  if(localHidden)Bubble.hide();                        // 收起时气泡一并收起
  applyVisibility();
 }
 function applyConfig(block){
  lastOff=block?block.enabled===false:false;
  // 形象预设（2026-11-02）：old=萨卡班麦麦（固定参数）；new=麦麦（Bear 模块）。
  activePreset=block&&['old','fish'].includes(block.preset)?'old':'new';
  try{localStorage.setItem('maimai-preset',activePreset)}catch(e){}   // 预设缓存：下次加载即时布景，消掉形象闪现
  if(!lastOff){
   const nextSize=clamp(number(block,'mascot_size_px',144),48,192);   // 两形象共用的基准尺寸
   const sizeChanged=nextSize!==cfg.size;
   cfg.size=nextSize;
   const de=document.documentElement.style;                           // 尺寸变量挂根节点：两个形象与气泡共用
   de.setProperty('--maimai-mascot-size',cfg.size+'px');
   de.setProperty('--maimai-peek-angle',FIXED.angle+'deg');
   de.setProperty('--maimai-detail-blur',FIXED.detailBlur+'px');
   de.setProperty('--maimai-detail-fade-ms',FIXED.detailFade+'ms');
   de.setProperty('--maimai-detail-scale',String(FIXED.detailScale));
   if(sizeChanged&&typeof Bear!=='undefined'&&Bear.reanchor)Bear.reanchor();
  }
  // 先落尺寸变量再启用形象：Bear.setEnabled 首次 applyAnchors 读到的
  // offsetWidth 才是配置尺寸，而不是默认 144px。
  applyVisibility();
  if(lastOff)return;
  setPoseClass();
  if(eyeRaf)cancelAnimationFrame(eyeRaf);eyeRaf=requestAnimationFrame(updateEyes);
 }
 setupEyes();
 setPoseClass();
 return {applyConfig,setDetailMode,setSleep,setLocalHidden,isLocalHidden:()=>localHidden};
})();
/* 预设缓存（尽力而为）：有存储时页面加载即按上次预设布景——
   消掉「萨卡班麦麦闪现→切麦麦」与等数据期的空窗；存储不可用（隐私沙箱等）
   时静默跳过，两个形象都等首份数据再显形（模板里萨卡班麦麦默认 hidden）。 */
try{
  const hidden=localStorage.getItem('maimai-mascot-hidden')==='true';   // W-12：本地收起时首帧也不显形
  const cached=localStorage.getItem('maimai-preset');
  if(!hidden&&cached==='new'){document.getElementById('maimai-peek').hidden=true;Bear.setEnabled(true)}
  else if(!hidden&&cached==='old'){document.getElementById('maimai-peek').hidden=false}
}catch(e){}
