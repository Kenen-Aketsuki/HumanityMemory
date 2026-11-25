/* 观测台各视图渲染。函数挂在 window.render_<name>，由 index.html 的调度器调用。 */
(function(){
'use strict';
const C={experience:'#6fb6ff',episode:'#b9a5ff',log:'#8898ad',digest:'#ffc46b'};
const NAMES={experience:'经验',episode:'经历',log:'日志',digest:'复盘'};

function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function num(v,d){return v==null?'—':Number(v).toFixed(d==null?2:d);}
function ts(ms){if(!ms)return '—';const d=new Date(ms);const p=n=>String(n).padStart(2,'0');
  return p(d.getMonth()+1)+'-'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes());}
function short(s,n){s=String(s||'');return s.length>n?s.slice(0,n)+'…':s;}
// 检索模式标签：与 recall 历史 / mcp_server 同语义
function modeTag(d){
  const m=d&&d.mode;
  if(m==='strict')   return '<span style="color:var(--up)" title="严格阈值命中">严格</span>';
  if(m==='fallback') return '<span style="color:var(--warn)" title="严格层没捞到，宽泛层兜底召回，精度较低">宽松兜底</span>';
  return '<span style="color:var(--dim)" title="未记录检索模式">模式未记录</span>';
}

/* ============================ 星图 ============================ */
let starGraph=null, starRAF=null;
window.render_starmap=async function(force){
  if(force||!starGraph){
    const d=await api('/api/starmap');
    if(d.ok===false){document.getElementById('v-starmap').innerHTML=
      '<div class="card note">'+esc(d.message)+'</div>';return;}
    starGraph=d.graph;
  }
  const g=starGraph, meta=g.meta||{}, nodes=g.nodes||[], edges=g.edges||[];
  const el=document.getElementById('v-starmap');
  const counts=Object.entries(meta.kind_counts||{}).map(([k,v])=>
    '<span class="kv" style="margin-right:14px"><span class="k" style="background:'+(C[k]||'#888')+'"></span>'+
    (NAMES[k]||k)+' <b>'+v+'</b></span>').join('');
  const warns=(meta.warnings||[]).length
    ? '<div class="note">'+esc((meta.warnings||[]).join(' · '))+'</div>' : '';
  el.innerHTML=
    '<div class="card"><div class="row" style="align-items:center">'+
      '<div class="kv">节点 <b>'+nodes.length+'</b>（真实 '+(meta.real_nodes||0)+'）</div>'+
      '<div class="kv">关系边 <b>'+edges.length+'</b></div>'+
      '<div class="kv">向量覆盖 <b>'+esc(meta.coverage||'—')+'</b></div>'+
      '<div class="kv">schema v<b>'+esc(meta.schema_version||'?')+'</b></div>'+
      '<div class="kv">生成 <b>'+esc(meta.generated_at||'—')+'</b></div>'+
    '</div><div class="row" style="margin-top:8px">'+counts+'</div>'+warns+'</div>'+
    '<div class="card" style="padding:0;overflow:hidden"><canvas id="star" style="display:block;width:100%"></canvas></div>';

  const canvas=document.getElementById('star'), ctx=canvas.getContext('2d');
  const W=canvas.clientWidth||900, H=520;
  const dpr=window.devicePixelRatio||1;
  canvas.width=W*dpr; canvas.height=H*dpr; canvas.style.height=H+'px'; ctx.scale(dpr,dpr);

  // 力导向布局（简化）：按等级分层给初始 y，然后若干次迭代斥力+边引力
  const lvls=(meta.levels||[]).map(x=>x.level);
  const maxL=Math.max(1,...lvls);
  const idx=new Map(nodes.map((n,i)=>[String(n.id),i]));
  const pos=nodes.map((n,i)=>{
    const a=i*2.399963; // 黄金角散布
    const layer=1-((n.forget_level||0)/maxL);
    return {x:W/2+Math.cos(a)*W*0.32*Math.sqrt((i%17+1)/18),
            y:24+layer*(H-60)+(Math.sin(a)*18),
            vx:0,vy:0,node:n};
  });
  // 后端边的字段是 s/t（不是 source/target），类型字段是 type（不是 kind）。
  // 这里兼容两种写法，避免以后字段改名又把整张图的边过滤光。
  const E=edges.map(e=>({a:idx.get(String(e.s??e.source)),b:idx.get(String(e.t??e.target)),e}))
               .filter(x=>x.a!=null&&x.b!=null);
  for(let it=0;it<120;it++){
    for(let i=0;i<pos.length;i++)for(let j=i+1;j<pos.length;j++){
      const dx=pos[j].x-pos[i].x,dy=pos[j].y-pos[i].y;
      let d2=dx*dx+dy*dy; if(d2<1)d2=1;
      const f=900/d2, d=Math.sqrt(d2), fx=dx/d*f, fy=dy/d*f;
      pos[i].vx-=fx;pos[i].vy-=fy;pos[j].vx+=fx;pos[j].vy+=fy;
    }
    for(const x of E){
      const a=pos[x.a],b=pos[x.b],dx=b.x-a.x,dy=b.y-a.y,d=Math.hypot(dx,dy)||1;
      const f=(d-70)*0.008,fx=dx/d*f,fy=dy/d*f;
      a.vx+=fx;a.vy+=fy;b.vx-=fx;b.vy-=fy;
    }
    for(const p of pos){p.x+=p.vx*0.85;p.y+=p.vy*0.85;p.vx*=0.82;p.vy*=0.82;
      p.x=Math.max(20,Math.min(W-20,p.x));p.y=Math.max(20,Math.min(H-20,p.y));}
  }
  // 画
  function draw(){
    ctx.clearRect(0,0,W,H);
    ctx.fillStyle='#0b0f16';ctx.fillRect(0,0,W,H);
    ctx.lineWidth=0.6;
    const EC={semantic:'rgba(111,182,255,.16)',invalidates:'rgba(240,138,138,.22)',
              digest:'rgba(255,196,107,.18)',tag:'rgba(136,152,173,.16)'};
    for(const x of E){
      const a=pos[x.a],b=pos[x.b];
      ctx.strokeStyle=EC[x.e.type]||'rgba(136,152,173,.16)';
      ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();
    }
    for(const p of pos){
      const n=p.node,kind=n.kind,act=Number(n.activity||0);
      const r=(meta.real_nodes&&kind!=='digest')?3.5+Math.min(6,Math.sqrt(act)*1.6):3;
      ctx.fillStyle=C[kind]||'#888';ctx.globalAlpha=n.live===false?0.35:0.9;
      ctx.beginPath();ctx.arc(p.x,p.y,r,0,Math.PI*2);ctx.fill();
      ctx.globalAlpha=1;
    }
  }
  draw();
  // hover 提示
  canvas.onmousemove=function(ev){
    const rect=canvas.getBoundingClientRect(),mx=ev.clientX-rect.left,my=ev.clientY-rect.top;
    let best=null,bd=144;
    for(const p of pos){const d=(p.x-mx)**2+(p.y-my)**2;if(d<bd){bd=d;best=p;}}
    document.getElementById('status').textContent=best
      ? (NAMES[best.node.kind]||best.node.kind)+' · '+best.node.id+' · 活跃度 '+num(best.node.activity,2)+' · L'+(best.node.forget_level??'—')+' · '+short(best.node.content,60)
      : '就绪';
  };
  document.getElementById('status').textContent='星图：悬停节点查看详情';
};

/* ============================ recall 历史 ============================ */
let recallData=null;
window.render_recall=async function(force){
  if(force||!recallData){recallData=await api('/api/recall');}
  const d=recallData, recs=(d.records||[]).slice().sort((a,b)=>(b.at||0)-(a.at||0));
  const M=d.meta||{}, ACT=M.activity||{}, levels=(M.levels||[]).slice().sort((a,b)=>a.level-b.level);
  const el=document.getElementById('v-recall');
  if(!recs.length){el.innerHTML='<div class="empty">还没有检索记录。用 memory_recall 跑几次就会出现。</div>';return;}

  // 指标
  let hitTotal=0,deltaSum=0;const kinds={};
  recs.forEach(r=>(r.hits||[]).forEach(h=>{hitTotal++;kinds[h.kind]=(kinds[h.kind]||0)+1;if(h.delta>0)deltaSum+=h.delta;}));
  const entry=levels.find(l=>l.down!=null);
  const med=deltaSum/Math.max(1,hitTotal);
  const card=(l,v,h)=>'<div class="card"><div class="kv">'+l+'</div><div style="font-size:16px;margin-top:2px">'+v+'</div><div class="kv">'+h+'</div></div>';
  const stats='<div class="row" style="margin-bottom:14px">'+
    card('检索次数',recs.length,'覆盖 '+new Set(recs.flatMap(r=>(r.hits||[]).map(h=>h.id))).size+' 条')+
    card('命中总数',hitTotal,'平均 '+(hitTotal/recs.length).toFixed(1)+' 条/次')+
    card('命中均值',num(med,3),'delta 平均')+
    card('降级线',entry?num(entry.down,1):'—',entry?('L'+entry.level):'无等级表')+
    '</div>';

  // 热度榜
  const hot=new Map();
  recs.forEach(r=>(r.hits||[]).forEach(h=>{
    const c=hot.get(h.id)||{id:h.id,kind:h.kind,n:0};c.n++;hot.set(h.id,c);
  }));
  const top=[...hot.values()].sort((a,b)=>b.n-a.n).slice(0,40);
  // 记录列表 + 详情
  const list=recs.map((r,i)=>
    '<div class="item'+(i===0?' on':'')+'" data-i="'+i+'" style="padding:8px 10px;border-bottom:1px solid #161d27;cursor:pointer">'+
      '<div style="font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(r.query||'（空查询）')+'</div>'+
      '<div class="kv" style="margin-top:3px">'+ts(r.at)+' · '+(r.hits||[]).length+' 命中'+(r.deep?' · 深度':'')+'</div>'+
    '</div>').join('');
  const hotHtml=top.map(x=>
    '<div style="padding:5px 10px;border-bottom:1px solid #161d27;font-size:12px">'+
      '<span class="k" style="background:'+(C[x.kind]||'#888')+'"></span>'+
      '<span class="id">'+esc(x.id)+'</span> <span style="float:right;color:var(--warn)">'+x.n+'</span>'+
    '</div>').join('');
  el.innerHTML=stats+
    '<div style="display:grid;grid-template-columns:300px minmax(0,1fr) 240px;gap:1px;background:var(--line);border:1px solid var(--line)">'+
      '<div style="background:var(--bg);max-height:calc(100vh - 280px);overflow:auto">'+list+'</div>'+
      '<div style="background:var(--bg);padding:14px;max-height:calc(100vh - 280px);overflow:auto" id="recDetail"></div>'+
      '<div style="background:var(--bg);max-height:calc(100vh - 280px);overflow:auto"><div class="kv" style="padding:8px 10px;border-bottom:1px solid var(--line)">条目热度（命中次数）</div>'+(hotHtml||'<div class="empty">暂无</div>')+'</div>'+
    '</div>';
  function detail(i){
    const r=recs[i];if(!r)return;
    const rows=(r.hits||[]).map(h=>
      '<tr><td>'+(h.rank||'')+'</td><td>'+(NAMES[h.kind]||h.kind)+'</td>'+
      '<td class="id">'+esc(h.id)+(h.forgotten?' 遗忘':'')+'</td><td>L'+(h.level==null?'—':h.level)+'</td>'+
      '<td>'+num(h.score,3)+'</td><td>'+num(h.vec_sim,3)+' / '+num(h.lex,2)+'</td>'+
      '<td>'+num(h.activity_before,2)+' → '+num(h.activity_after,2)+'</td>'+
      '<td class="'+(h.delta>0?'up':'')+'">'+(h.delta>0?'+'+num(h.delta,3):num(h.delta,3))+'</td></tr>').join('');
    document.getElementById('recDetail').innerHTML=
      '<pre style="white-space:pre-wrap;word-break:break-word;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px;margin:0 0 10px;font:13px/1.5 inherit">'+esc(r.query||'（空查询）')+'</pre>'+
      '<div class="kv" style="margin-bottom:10px">时间 <b>'+ts(r.at)+'</b> · 命中 <b>'+(r.hits||[]).length+'</b> · 耗时 <b>'+num(r.elapsed_ms,1)+' ms</b></div>'+
      ((r.hits||[]).length?'<table><thead><tr><th>#</th><th>类型</th><th>id</th><th>等级</th><th>分数</th><th>向量/词面</th><th>活跃度</th><th>增量</th></tr></thead><tbody>'+rows+'</tbody></table>':'<div class="empty">这次没有命中。</div>');
  }
  el.querySelectorAll('.item').forEach(n=>n.onclick=()=>{
    el.querySelectorAll('.item').forEach(x=>x.classList.remove('on'));n.classList.add('on');
    detail(Number(n.dataset.i));
  });
  if(recs.length)detail(0);
};

/* ============================ 复盘记录 ============================ */
let reviewData=null, reviewSel=0;
window.render_review=async function(force){
  if(force||!reviewData){reviewData=await api('/api/review');}
  const days=reviewData.days||[];
  const el=document.getElementById('v-review');
  if(!days.length){el.innerHTML='<div class="empty">还没有复盘记录（reports/judge 下没有报告）。<br>跑一次 lifecycle judge 后就会出现。</div>';return;}

  const timeline=days.map((d,i)=>
    '<div class="item'+(i===reviewSel?' on':'')+'" data-i="'+i+'" style="padding:10px 12px;border-bottom:1px solid #161d27;cursor:pointer">'+
      '<div style="font-size:13px">'+esc(d.date)+(d.dry?' <span class="warn" style="font-size:11px">dry-run</span>':'')+'</div>'+
      '<div class="kv" style="margin-top:3px">变动 '+d.changes.length+' · 新增 '+d.added.length+' · 顶替 '+d.replaced.length+' · 合并 '+d.deduped.length+'</div>'+
    '</div>').join('');

  el.innerHTML='<div style="display:grid;grid-template-columns:210px minmax(0,1fr);gap:1px;background:var(--line);border:1px solid var(--line)">'+
    '<div style="background:var(--bg);max-height:calc(100vh - 140px);overflow:auto">'+timeline+'</div>'+
    '<div style="background:var(--bg);padding:14px;max-height:calc(100vh - 140px);overflow:auto" id="reviewDetail"></div>'+
  '</div>';

  function sec(title,sub,body){
    return '<div class="card"><div class="row" style="align-items:baseline;margin-bottom:8px">'+
      '<strong style="font-size:13px">'+title+'</strong><span class="kv">'+sub+'</span></div>'+body+'</div>';
  }
  function table(cols,rows){
    if(!rows.length)return '<div class="empty" style="padding:12px">（无）</div>';
    return '<div class="scroll"><table><thead><tr>'+cols.map(c=>'<th>'+c+'</th>').join('')+'</tr></thead><tbody>'+rows.join('')+'</tbody></table></div>';
  }
  function detail(i){
    reviewSel=i;const d=days[i];
    const s=d.summary||{};
    let html='<div class="row" style="margin-bottom:12px">'+
      '<span class="kv">到期 <b>'+(s.due||0)+'</b></span><span class="kv">已裁定 <b>'+(s.settled||0)+'</b></span>'+
      '<span class="kv">节点 <b>'+(s.total_nodes||0)+'</b></span><span class="kv">快照文件 <b>'+esc(d.file)+'</b></span></div>';

    // 1 等级变动
    const chg=d.changes.map(x=>
      '<tr><td class="id">'+esc(x.id)+'</td><td>'+(NAMES[x.kind]||x.kind||'—')+'</td>'+
      '<td>L'+(x.from==null?'—':x.from)+' → L'+(x.to==null?'—':x.to)+' '+(
        x.action==='up'?'<span class="up">↑</span>':x.action==='down'?'<span class="down">↓</span>':'=')+'</td>'+
      '<td class="'+(Number(x.activity)>0?'up':'')+'">'+num(x.activity,2)+'</td>'+
      '<td>'+esc(x.reason||'')+'</td></tr>');
    html+=sec('① 遗忘等级变动','裁定那一刻的活跃度',
      d.changes.length?table(['id','类型','等级变动','变动时活跃度','依据'],chg)
        :'<div class="empty" style="padding:12px">当天没有到期条目 / 无等级变动</div>');

    // 2 活跃度变动
    const dl=d.activity_deltas.map(x=>
      '<tr><td class="id">'+esc(x.id)+'</td><td>'+(NAMES[x.kind]||x.kind||'—')+'</td>'+
      '<td>'+num(x.prev,2)+'</td><td>'+num(x.current,2)+'</td>'+
      '<td class="'+(x.delta>0?'up':x.delta<0?'down':'')+'">'+(x.delta>0?'+':'')+num(x.delta,2)+'</td></tr>');
    html+=sec('② 活跃度变动','相邻天快照 nodes[] 按 id 算差（裁定后 vs 裁定后，不是检索增量）',
      table(['id','类型','前一天','当天','变动'],dl)+
      '<div class="note" style="margin-top:8px;font-size:11px">提示：裁定后活跃度会清零，这里的差是「裁定完 vs 裁定完」。检索命中增量请看 recall 历史页。</div>');

    // 3 新增 / 顶替 / 合并
    const add=d.added.map(x=>'<tr><td class="id">'+esc(x.id)+'</td><td>'+(NAMES[x.kind]||x.kind)+'</td><td>L'+(x.level==null?'—':x.level)+'</td><td>'+num(x.activity,2)+'</td><td>'+esc(x.summary)+'</td></tr>');
    const rep=d.replaced.map(x=>'<tr><td class="id">'+esc(x.old_id)+'</td><td class="id">'+esc(x.new_id)+'</td><td>'+esc(x.summary)+'</td><td>'+ts(x.at)+'</td></tr>');
    const ded=d.deduped.map(x=>'<tr><td class="id">'+esc(x.item_id)+'</td><td class="up">+'+num(x.delta,2)+'</td><td>'+esc(x.summary)+'</td><td>'+ts(x.at)+'</td></tr>');
    let third='';
    const empty3=(d.added.length+d.replaced.length+d.deduped.length)===0;
    if(empty3){third='<div class="empty" style="padding:12px">当天无新增 / 顶替 / 合并</div>';}
    else{
      if(d.added.length)third+='<div class="kv" style="margin:6px 0">新增记忆（'+d.added.length+'）</div>'+table(['id','类型','等级','初始活跃度','摘要'],add);
      if(d.replaced.length)third+='<div class="kv warn" style="margin:12px 0 6px">顶替老记忆（'+d.replaced.length+'）— 旧条目被新结论取代</div>'+table(['旧 id','顶替它的新 id','旧内容摘要','时间'],rep);
      if(d.deduped.length)third+='<div class="kv" style="margin:12px 0 6px">去重合并（'+d.deduped.length+'）— 原条目保留，只加活跃度（与顶替语义相反）</div>'+table(['原条目 id','活跃度增量','新内容摘要','时间'],ded);
    }
    html+=sec('③ 新增与顶替','库实时数据',third);

    // 4 md 原文
    html+=sec('④ 报告原文','Markdown 快照',
      '<div class="kv">快照 JSON：'+esc(d.file)+'（完整原文见工作区 reports/judge/ 下同名的 .md）</div>');
    document.getElementById('reviewDetail').innerHTML=html;
  }
  el.querySelectorAll('.item').forEach(n=>n.onclick=()=>{
    el.querySelectorAll('.item').forEach(x=>x.classList.remove('on'));n.classList.add('on');
    detail(Number(n.dataset.i));
  });
  detail(reviewSel<days.length?reviewSel:0);
};

/* ============================ 记忆现状 ============================ */
let memData=null, memSort='id', memDir=1, memFilter='all', memQuery='';
function fmtRemain(ms){
  if(ms==null)return '—';
  if(ms<=0)return '已到期';
  const days=ms/86400000;
  if(days>=1)return days.toFixed(1)+' 天';
  const hrs=ms/3600000;
  return hrs.toFixed(1)+' 小时';
}
window.render_memory=async function(force){
  if(force||!memData){memData=await api('/api/memory');}
  const nodes=memData.nodes||[], el=document.getElementById('v-memory');
  if(!nodes.length){el.innerHTML='<div class="empty">当前库没有记忆条目。</div>';return;}
  const counts={};
  nodes.forEach(n=>counts[n.due_code]=(counts[n.due_code]||0)+1);

  const bar='<div class="card"><div class="row" style="align-items:center">'+
    '<span class="kv">共 <b>'+nodes.length+'</b> 条</span>'+
    '<span class="kv up">待裁定 <b>'+(counts.pending||0)+'</b></span>'+
    '<span class="kv down">已到期 <b>'+(counts.due||0)+'</b></span>'+
    '<span class="kv">遗忘层 <b>'+(counts.forgotten||0)+'</b></span>'+
    '<span class="kv">未计时 <b>'+(counts.unstarted||0)+'</b></span>'+
    '<input id="memQuery" type="text" placeholder="按 ID 检索…" value="'+esc(memQuery)+'" style="width:200px;margin-left:auto" autocomplete="off">'+
    '<select id="memFilter">'+
      ['all|全部','pending|待裁定','due|已到期','forgotten|遗忘层'].map(x=>{
        const [v,l]=x.split('|');return '<option value="'+v+'"'+(memFilter===v?' selected':'')+'>'+l+'</option>';
      }).join('')+
    '</select></div></div>';
  el.innerHTML=bar+'<div class="card" style="padding:0;overflow:hidden"><div class="scroll" id="memTable"></div></div>';

  // 可排序列：id / 类型 / 等级 / 活跃度 / 距下次裁定。点同列翻转升降，点新列回到升序。
  const COLS=[['id','id'],['kind','类型'],['level','等级'],['activity','活跃度'],
    [null,'摘要'],[null,'裁定状态'],[null,'预计裁定'],[null,'距下次裁定'],[null,'升/降线']];
  const SORTABLE={id:1,kind:1,level:1,activity:1,remain:1};
  const KIND_ORD={experience:0,episode:1,log:2,digest:3};

  function draw(){
    let rows=nodes.filter(n=>
      (memFilter==='all'||n.due_code===memFilter)&&
      (!memQuery||String(n.id).toLowerCase().indexOf(memQuery)>=0));
    rows=rows.slice().sort((a,b)=>{
      let va,vb;
      switch(memSort){
        case 'kind':     va=KIND_ORD[a.kind]!=null?KIND_ORD[a.kind]:9;
                         vb=KIND_ORD[b.kind]!=null?KIND_ORD[b.kind]:9; break;
        case 'level':    va=(a.level==null?-1:a.level); vb=(b.level==null?-1:b.level); break;
        case 'activity': va=(Number(a.activity)||0); vb=(Number(b.activity)||0); break;
        case 'remain':   // 已到期排最前，未计时的排最后，与旧排序语义一致
                         va=a.due_code==='due'?-1:(a.remain_ms==null?Infinity:a.remain_ms);
                         vb=b.due_code==='due'?-1:(b.remain_ms==null?Infinity:b.remain_ms); break;
        default:         va=String(a.id); vb=String(b.id);
      }
      const c=(typeof va==='string')?va.localeCompare(vb):va-vb;
      return c*memDir;
    });
    const tr=rows.map(n=>{
      const dueCls=n.due_code==='due'?'down':n.due_code==='pending'?'up':'';
      const p=n.predict||{};
      const pCls=p.action==='up'?'up':p.action==='down'?'down':'kv';
      return '<tr><td class="id">'+esc(n.id)+'</td><td>'+(NAMES[n.kind]||n.kind)+'</td>'+
        '<td>L'+(n.level==null?'—':n.level)+'</td><td>'+num(n.activity,2)+'</td>'+
        '<td>'+esc(n.content)+'</td>'+
        '<td class="'+dueCls+'">'+esc(n.due_label)+'</td>'+
        '<td class="'+pCls+'"'+(p.reason?' title="裁定依据：'+esc(p.reason)+'"':'')+'>'+esc(p.label||'—')+'</td>'+
        '<td class="'+dueCls+'">'+fmtRemain(n.remain_ms)+'</td>'+
        '<td>'+(n.up!=null?num(n.up,1):'—')+' / '+(n.down!=null?num(n.down,1):'—')+'</td></tr>';
    }).join('');
    const th=COLS.map(([key,label])=>{
      if(!key)return '<th>'+label+'</th>';
      const on=memSort===key;
      const arrow=on?(memDir===1?' ▲':' ▼'):'';
      return '<th data-s="'+key+'" style="cursor:pointer;'+(on?'color:var(--exp)':'')+'"'+
        ' title="点击排序，再点翻转升降">'+label+arrow+'</th>';
    }).join('');
    document.getElementById('memTable').innerHTML=
      '<table><thead><tr>'+th+'</tr></thead><tbody>'+tr+'</tbody></table>';
    document.querySelectorAll('#memTable th[data-s]').forEach(th=>th.onclick=()=>{
      if(memSort===th.dataset.s){memDir=-memDir;}
      else{memSort=th.dataset.s;memDir=1;}
      draw();
    });
  }
  document.getElementById('memFilter').onchange=e=>{memFilter=e.target.value;draw();};
  document.getElementById('memQuery').oninput=e=>{memQuery=e.target.value.trim().toLowerCase();draw();};
  draw();
};

/* ============================ 检索测试 ============================ */
// 悬浮窗：检索结果 ID 列悬停显示条目内容（跟随鼠标，不挡点击）
let hitTipEl=null;
function showHitTip(ev,text){
  if(!hitTipEl){
    hitTipEl=document.createElement('div');
    hitTipEl.style.cssText='position:fixed;z-index:999;display:none;max-width:460px;max-height:300px;'+
      'overflow:auto;background:rgba(13,20,34,.97);border:1px solid var(--line);border-radius:8px;'+
      'padding:10px 12px;font:12px/1.6 inherit;color:var(--text);white-space:pre-wrap;word-break:break-word;'+
      'box-shadow:0 6px 18px rgba(0,0,0,.55);pointer-events:none';
    document.body.appendChild(hitTipEl);
  }
  hitTipEl.textContent=text||'（无内容）';
  hitTipEl.style.display='block';
  const pad=14;let x=ev.clientX+pad,y=ev.clientY+pad;
  const r=hitTipEl.getBoundingClientRect();
  if(x+r.width>window.innerWidth-8)x=ev.clientX-r.width-pad;
  if(y+r.height>window.innerHeight-8)y=ev.clientY-r.height-pad;
  hitTipEl.style.left=x+'px';hitTipEl.style.top=y+'px';
}
function hideHitTip(){if(hitTipEl)hitTipEl.style.display='none';}

window.render_search=async function(force){
  const el=document.getElementById('v-search');
  // 只在首次挂载表单，避免轮询重绘丢失输入
  if(!document.getElementById('searchForm')){
    el.innerHTML=
      '<div class="card" id="searchForm">'+
        '<div class="row" style="align-items:center">'+
          '<input id="sq" type="text" placeholder="输入查询，模拟一次 recall 检索…" style="flex:1" autocomplete="off">'+
          '<span class="kv">条数</span><input id="sl" type="number" value="8" min="1" max="50" style="width:64px">'+
          '<select id="sk"><option value="all">全部类型</option><option value="experience">经验</option><option value="episode">经历</option></select>'+
          '<button class="act" id="sgo">检索</button>'+
        '</div>'+
        '<div class="note" style="margin-top:8px;font-size:11px">模拟 recall 的检索与打分，纯只读：不写活跃度、不写检索日志、不触发复盘闸门。深度回忆会改写遗忘层，不在模拟范围内。悬停结果里的 id 列可查看条目内容。</div>'+
      '</div>'+
      '<div class="card" id="searchResult"><div class="empty">输入查询并点「检索」</div></div>';
    document.getElementById('sgo').onclick=runSearch;
    document.getElementById('sq').addEventListener('keydown',e=>{if(e.key==='Enter')runSearch();});
  }
  async function runSearch(){
    const q=document.getElementById('sq').value.trim();
    if(!q){document.getElementById('searchResult').innerHTML='<div class="empty">请输入查询</div>';return;}
    const limit=Number(document.getElementById('sl').value)||8;
    let kinds=document.getElementById('sk').value; kinds=kinds==='all'?null:kinds;
    const btn=document.getElementById('sgo');btn.disabled=true;btn.textContent='检索中…';
    try{
      const d=await api('/api/search-test',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({query:q,limit:limit,kinds:kinds})});
      const hs=d.hits||[];
      const rows=hs.map((h,i)=>
        '<tr'+(h.needs_verify?' title="可信度偏低，标了先验证再用"':'')+'>'+
        '<td>'+h.rank+'</td><td>'+(NAMES[h.kind]||h.kind)+'</td>'+
        '<td class="id hittip" data-i="'+i+'">'+esc(h.id)+(h.fallback?' <span style="color:var(--warn)" title="宽泛层兜底召回">宽松</span>':'')+'</td><td>L'+(h.level==null?'—':h.level)+'</td>'+
        '<td>'+num(h.score,4)+'</td><td>'+num(h.vec_sim,3)+' / '+num(h.lex,2)+'</td>'+
        '<td>'+num(h.activity,2)+'</td><td class="up">+'+num(h.simulated_delta,3)+'</td>'+
        '<td>'+num(h.activity_after,2)+'</td></tr>').join('');
      document.getElementById('searchResult').innerHTML=
        '<div class="row" style="margin-bottom:10px">'+
          '<span class="kv">查询 <b>'+esc(q)+'</b></span>'+
          '<span class="kv">命中 <b>'+hs.length+'</b></span>'+
          '<span class="kv">检索模式 '+modeTag(d)+'</span>'+
          '<span class="kv">耗时 <b>'+num(d.took_ms,1)+' ms</b></span></div>'+
        (hs.length?'<table><thead><tr>'+
          ['#','类型','id','等级','分数','向量/词面','当前活跃度','模拟增量','假想活跃度'].map(x=>'<th>'+x+'</th>').join('')+
          '</tr></thead><tbody>'+rows+'</tbody></table>':'<div class="empty">这次没有命中任何记忆。</div>')+
        '<div class="note" style="margin-top:10px;font-size:11px">'+esc(d.note||'')+'</div>';
      // ID 列悬停出浮层：事件委托挂在结果容器上，重绘不用重绑
      const res=document.getElementById('searchResult');
      res.onmousemove=ev=>{
        const t=ev.target.closest&&ev.target.closest('.hittip');
        const h=t&&hs[Number(t.dataset.i)];
        if(h)showHitTip(ev,h.content);
        else hideHitTip();
      };
      res.onmouseleave=hideHitTip;
    }catch(e){
      document.getElementById('searchResult').innerHTML='<div class="card note">检索失败：'+esc(e.message)+'</div>';
    }finally{btn.disabled=false;btn.textContent='检索';}
  }
};
})();
