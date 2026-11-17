/* LBM_LAYOUT_FIX */
/* 布局修正层 · 行为脚本（2026-11-14 主题对比批并入基线）
   来源：设计评估 shared/layout_fix.js，三版统一、对所有主题（含原版）生效。
   additive，不改既有函数；经 LBM_EXTENSIONS token 注入，排在 fish.js 之后
   （依赖 fish.js open() 派发的 lbm:detail-open 与 .fish-panel/.fish-records DOM）。
   - classifyWide：按 pre 文本量给 #core-fields-detail 的长内容字段块加
     .lbm-wide（fish.css 布局层让其跨双列）；短块留在双列卡片位。
   - adaptWidth：面板宽度随内容自适应，clamp(480, 内容档, 720) 且钳制在视口内；
     稀疏 section（core-status/core-modules/core-logs/companion/sharing）收宽，
     空态（仅一句 note）收缩到 480。写 inline width，覆盖 placePanel 的固定 720。
   触发：lbm:detail-open（placePanel 之后）→ 同步生效，随后 fish.js 的 rAF
   snapPanel 按新尺寸贴附；resize 与 records 内容变更补算。 */
(function(){
  'use strict';
  var OV='section-fish-popover';
  var SPARSE={'core-status':1,'core-modules':1,'core-logs':1,'companion':1,'sharing':1};
  var WIDE_THRESHOLD=180; /* 字符数：emotion≈357 跨列，其余≤139 留双列（快照实测） */

  function overlay(){return document.getElementById(OV);}

  function classifyWide(){
    var fd=document.getElementById('core-fields-detail');
    if(!fd)return;
    var blocks=fd.children;
    for(var i=0;i<blocks.length;i++){
      var b=blocks[i];
      if(!b.classList||!b.classList.contains('core-raw-field'))continue;
      var pres=b.querySelectorAll('pre'),n=0;
      for(var j=0;j<pres.length;j++)n+=(pres[j].textContent||'').length;
      var hasChart=!!b.querySelector('svg,.chart');
      b.classList.toggle('lbm-wide',hasChart||n>WIDE_THRESHOLD);
    }
  }

  function adaptWidth(){
    var ov=overlay();
    if(!ov||ov.hidden||ov.classList.contains('is-closing'))return;
    var panel=ov.querySelector('.fish-panel');if(!panel)return;
    var key=ov.dataset.section||'';
    var vw=window.innerWidth||document.documentElement.clientWidth||720;
    var cap=Math.min(720,vw-24);
    var w=cap;
    if(SPARSE[key]){
      var lo=Math.min(480,cap);
      var rec=ov.querySelector('.fish-records');
      var heavy=rec&&rec.querySelector('pre,svg,.chart,table,.core-raw-field,.list,.bucket-grid,ul');
      var len=rec?(rec.textContent||'').replace(/\s+/g,'').length:0;
      if(!heavy&&len<80)w=lo;               /* 空态 → 480 */
      else w=Math.min(cap,Math.max(lo,560)); /* 稀疏但有内容 → 560 */
    }
    w=Math.max(Math.min(480,cap),Math.min(720,w)); /* clamp 480–720（视口感知） */
    panel.style.width=Math.round(w)+'px';
  }

  function apply(){try{classifyWide();adaptWidth();}catch(e){}}

  function watchRecords(){
    var ov=overlay();if(!ov)return;
    var rec=ov.querySelector('.fish-records');
    if(rec&&!rec.__lbmFixWatch){
      rec.__lbmFixWatch=true;
      /* 只观察 childList/characterData：本脚本改的是 panel.style 与块 class，
         不在观察范围内，不会自激。 */
      new MutationObserver(function(){apply();}).observe(rec,{childList:true,subtree:true,characterData:true});
    }
  }

  document.addEventListener('lbm:detail-open',function(){apply();watchRecords();});
  window.addEventListener('resize',apply);
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',function(){setTimeout(apply,0);});
  else setTimeout(apply,0);
})();