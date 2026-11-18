"""检索统计的单文件 HTML 视图。

和 `starmap.py` 同一套思路：数据读出来 → 塞进一个自包含 HTML → 浏览器打开。
没有外部依赖（不走 CDN），断网也能看，方便随手拷走。

页面分三层，对应「看数据 → 看细节 → 看结论」：

    顶部指标条  直接把结论算出来：一次检索值多少分、维持/升级要几次命中、
                有多少节点正踩在降级线下面。**不要求人自己心算。**
    左栏        检索记录列表（时间倒序）
    中栏        选中那次检索的详情：问了什么、翻出谁、每条活跃度加了多少
    右栏        条目热度榜 + 自动诊断（当前配置偏苛刻还是偏宽松）

为什么要自己算一遍：活跃度阈值是绝对分数，而「一次检索值多少分」由相似度
和曲线决定。两个数字都躺在配置里，但**它们相不相配只有算出来才知道** ——
维持一个等级需要 2 次命中还是 20 次，直接决定这套参数在你手上可不可用。
"""

from __future__ import annotations

import json
from typing import Any, Sequence


def _escape(payload: str) -> str:
    return (
        payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    )


def render_retrieval_html(
    records: Sequence[dict[str, Any]],
    *,
    meta: dict[str, Any] | None = None,
) -> str:
    data = {
        "records": list(records),
        "meta": meta or {},
    }
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return _TEMPLATE.replace("/*__DATA__*/", "const DATA = " + _escape(payload) + ";")


_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>检索分析 · HumanityMemory</title>
<style>
:root{
  --bg:#0b0f16; --panel:#121821; --line:#1f2937;
  --text:#d6e2f0; --muted:#7d8ea3; --dim:#54637a;
  --exp:#6fb6ff; --epi:#b9a5ff; --log:#8898ad; --up:#5fd6a0;
  --warn:#ffc46b; --bad:#f08a8a; --ok:#5fd6a0;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font:13px/1.55 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;}
header{display:flex;align-items:baseline;gap:14px;padding:12px 18px 8px}
h1{font-size:15px;font-weight:500;margin:0}
.sub{color:var(--muted);font-size:12px}
.stats{display:flex;gap:1px;background:var(--line);border-top:1px solid var(--line);
  border-bottom:1px solid var(--line)}
.card{flex:1;background:var(--bg);padding:9px 16px;min-width:0}
.card .k{color:var(--muted);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card .v{font-size:16px;font-weight:500;margin-top:2px;letter-spacing:.2px}
.card .h{color:var(--dim);font-size:10.5px;margin-top:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.v.warn{color:var(--warn)} .v.bad{color:var(--bad)} .v.ok{color:var(--ok)}
main{display:grid;grid-template-columns:280px minmax(0,1fr) 268px;height:calc(100vh - 128px)}
@media (max-width:1080px){main{grid-template-columns:1fr;height:auto}}
aside,section{overflow:auto;border-right:1px solid var(--line)}
aside:last-child{border-right:0;border-left:1px solid var(--line)}
h2{font-size:12px;font-weight:500;color:var(--muted);margin:0;padding:10px 14px 8px;
  letter-spacing:.5px;position:sticky;top:0;background:var(--bg);z-index:2;border-bottom:1px solid var(--line)}
.item{padding:8px 14px;border-bottom:1px solid #161d27;cursor:pointer}
.item:hover{background:var(--panel)}
.item.on{background:#17202d;border-left:2px solid var(--exp)}
.q{color:var(--text);font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.t{color:var(--dim);font-size:11px;margin-top:3px;display:flex;gap:8px}
.tag{color:var(--warn)}
.row{padding:10px 16px}
pre.q{white-space:pre-wrap;word-break:break-word;background:var(--panel);border:1px solid var(--line);
  border-radius:8px;padding:10px 12px;margin:0 0 10px;font:13px/1.5 inherit;color:#e7f0fb}
.facts{display:flex;gap:16px;flex-wrap:wrap;color:var(--muted);font-size:12px;margin-bottom:12px}
.facts b{color:var(--text);font-weight:500}
table{width:100%;border-collapse:collapse;font-size:12px}
th{color:var(--muted);font-weight:500;text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);
  position:sticky;top:0;background:var(--bg)}
td{padding:7px 8px;border-bottom:1px solid #161d27;vertical-align:top}
.k{display:inline-block;width:6px;height:6px;border-radius:50%;margin-right:6px;vertical-align:1px}
.id{font-family:ui-monospace,Consolas,monospace;color:var(--dim);font-size:11px}
.bar{position:relative;height:14px;background:#151d28;border-radius:3px;min-width:70px}
.bar>i{position:absolute;left:0;top:0;bottom:0;background:var(--up);border-radius:3px;opacity:.55}
.bar>span{position:relative;padding-left:5px;font-size:11px;color:#bfe9d6}
.zero{color:var(--dim)}
.hot{display:flex;align-items:center;gap:8px;padding:6px 14px;border-bottom:1px solid #161d27}
.hot .n{color:var(--warn);font-size:11px;min-width:20px;text-align:right}
.hot .c{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px}
.hot .lv{color:var(--dim);font-size:11px}
.diag{border-top:1px solid var(--line);margin-top:6px}
.note{padding:9px 14px;border-bottom:1px solid #161d27;font-size:12px;line-height:1.6}
.note b{font-weight:500}
.note.warn{color:#e8c98a} .note.bad{color:#e6a3a3} .note.ok{color:#9fdcc0}
.note.mute{color:var(--dim)}
.empty{padding:16px;color:var(--dim);font-size:12px}
</style>
</head>
<body>
<header>
  <h1>检索分析</h1>
  <span class="sub" id="sub">加载中…</span>
</header>
<div class="stats" id="stats"></div>
<main>
  <aside><h2>检索记录</h2><div id="list"></div></aside>
  <section><div id="detail"><div class="empty">左侧选一条记录</div></div></section>
  <aside>
    <h2>条目热度</h2><div id="hot"></div>
    <div class="diag"><h2>自动诊断</h2><div id="diag"></div></div>
  </aside>
</main>
<script>
/*__DATA__*/
const C={experience:'#6fb6ff',episode:'#b9a5ff',log:'#8898ad'};
const NAMES={experience:'经验',episode:'经历',log:'日志'};
const M=DATA.meta||{};
const recs=(DATA.records||[]).slice().sort((a,b)=>(b.at||0)-(a.at||0));
const levels=(M.levels||[]).slice().sort((a,b)=>a.level-b.level);
const nodes=M.nodes||[];

function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function ts(ms){const d=new Date(ms);const p=n=>String(n).padStart(2,'0');
  return p(d.getMonth()+1)+'-'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes());}
function num(v,d){return v==null?'—':Number(v).toFixed(d==null?2:d);}
function med(a){if(!a.length)return null;const s=a.slice().sort((x,y)=>x-y);const m=s.length>>1;
  return s.length%2?s[m]:(s[m-1]+s[m])/2;}

const deltas=[], kinds={};
let hitTotal=0;
recs.forEach(r=>{
  const hs=r.hits||[]; hitTotal+=hs.length;
  hs.forEach(h=>{kinds[h.kind]=(kinds[h.kind]||0)+1; if(h.delta>0)deltas.push(h.delta);});
});
const medDelta=med(deltas);
const logShare=hitTotal?((kinds.log||0)/hitTotal*100):0;
const entry=levels.find(l=>l.down!=null)||null;
const keepNeed=entry&&medDelta?entry.down/medDelta:null;
const riseNeed=entry&&medDelta&&entry.up!=null?entry.up/medDelta:null;

let belowLine=0;
const nodeTotal=nodes.length;
nodes.forEach(n=>{if(n.down!=null&&(n.activity||0)<n.down)belowLine++;});

function card(label,value,hint,cls){
  return '<div class="card"><div class="k">'+label+'</div><div class="v '+(cls||'')+'">'+value+'</div>'+
         '<div class="h">'+(hint||'')+'</div></div>';
}
function renderStats(){
  const avgHit=recs.length?(hitTotal/recs.length):0;
  document.getElementById('stats').innerHTML=
    card('检索次数',recs.length,'覆盖 '+new Set(recs.flatMap(r=>(r.hits||[]).map(h=>h.id))).size+' 条记忆')+
    card('平均命中',num(avgHit,1)+' 条','共 '+hitTotal+' 次命中')+
    card('单次得分',medDelta==null?'—':num(medDelta,2),'高相关命中给的分（中位）')+
    card('维持需命中',keepNeed==null?'—':num(keepNeed,1)+' 次',
         entry?('L'+entry.level+' 降级线 '+num(entry.down,1)):'缺少等级表',
         keepNeed!=null?(keepNeed>5?'warn':(keepNeed<2?'':'ok')):'')+
    card('升级需命中',riseNeed==null?'—':num(riseNeed,1)+' 次',
         entry&&entry.up!=null?('L'+entry.level+' 升级线 '+num(entry.up,1)):'','')+
    card('降级线下',nodeTotal?belowLine+' / '+nodeTotal:'—',
         nodeTotal?'活跃度低于降级线的节点':'无节点快照',
         nodeTotal?(belowLine>nodeTotal*0.8?'warn':'ok'):'');
}

function renderDiag(){
  const out=[];
  if(recs.length<10)out.push(['mute','<b>样本太少</b>：只有 '+recs.length+' 次检索，下面的判断先当参考，攒到二三十次再看。']);
  if(logShare>20)out.push(['warn','<b>日志占了 '+num(logShare,0)+'% 的检索位</b>：日志不参与等级，delta 恒为 0，位置被占了却不产生推力。考虑检索时降权或过滤。']);
  if(keepNeed!=null){
    if(keepNeed>5)out.push(['bad','<b>阈值偏苛刻</b>：维持 L'+entry.level+' 要在 '+num(entry.period,0)+' 天里被命中 <b>'+num(keepNeed,1)+' 次</b>。真实节奏达不到的话，它会一直往下掉 —— 要么调低 down_threshold，要么拉长周期。']);
    else if(keepNeed<2)out.push(['ok','<b>阈值偏宽松</b>：命中 '+num(keepNeed,1)+' 次就能保住，衰减会很慢。']);
    else out.push(['ok','<b>阈值与分值相配</b>：维持 '+num(keepNeed,1)+' 次、升级 '+num(riseNeed,1)+' 次命中，落在可执行区间。']);
  }
  if(nodeTotal){
    const pct=belowLine/nodeTotal*100;
    if(pct>80)out.push(['bad','<b>'+belowLine+'/'+nodeTotal+' 个节点在降级线下</b>：下一轮裁定它们都会掉一级。若不是预期，先调阈值再跑 judge。']);
    else if(pct<10)out.push(['ok','绝大多数节点在死区或更高处，衰减节奏正常。']);
  }
  if(!out.length)out.push(['mute','数据还不够，先跑几次 memory_recall。']);
  document.getElementById('diag').innerHTML=out.map(([c,t])=>'<div class="note '+c+'">'+t+'</div>').join('');
}

function summary(){
  const span=recs.length?ts(recs[recs.length-1].at)+' → '+ts(recs[0].at):'—';
  document.getElementById('sub').textContent=
    (M.db?M.db+' · ':'')+span+(M.generated?(' · 节点快照 '+ts(M.generated)):'');
}

function list(){
  const el=document.getElementById('list');
  if(!recs.length){el.innerHTML='<div class="empty">还没有检索记录。<br>用 memory_recall 跑几次就会出现。</div>';return;}
  el.innerHTML=recs.map((r,i)=>
    '<div class="item'+(i===0?' on':'')+'" data-i="'+i+'">'+
      '<div class="q">'+esc(r.query||'（空查询）')+'</div>'+
      '<div class="t"><span>'+ts(r.at)+'</span><span>'+(r.hit_count||(r.hits||[]).length)+' 命中</span>'+
      (r.deep?'<span class="tag">深度</span>':'')+'</div>'+
    '</div>').join('');
  el.querySelectorAll('.item').forEach(node=>{
    node.onclick=()=>{
      el.querySelectorAll('.item').forEach(n=>n.classList.remove('on'));
      node.classList.add('on');
      detail(Number(node.dataset.i));
    };
  });
}

function detail(i){
  const r=recs[i]; if(!r)return;
  const hits=r.hits||[];
  const maxDelta=Math.max(0,...hits.map(h=>h.delta||0))||1;
  const rows=hits.map(h=>{
    const d=h.delta;
    const bar=d==null||d<=0
      ? '<span class="zero">'+(d==null?'—':'0')+'</span>'
      : '<div class="bar"><i style="width:'+Math.max(4,(d/maxDelta*100)).toFixed(1)+'%"></i>'+
        '<span>+'+num(d,3)+'</span></div>';
    return '<tr>'+
      '<td>'+(h.rank||'')+'</td>'+
      '<td><span class="k" style="background:'+(C[h.kind]||'#8898ad')+'"></span>'+
        esc(NAMES[h.kind]||h.kind)+'</td>'+
      '<td class="id">'+esc(h.id)+(h.forgotten?' <span class="tag">遗忘层</span>':'')+'</td>'+
      '<td>L'+(h.level==null?'—':h.level)+'</td>'+
      '<td>'+num(h.score,3)+'</td>'+
      '<td class="id">'+num(h.vec_sim,3)+' / '+num(h.lex,2)+'</td>'+
      '<td>'+num(h.activity_before,2)+' → '+num(h.activity_after,2)+'</td>'+
      '<td>'+bar+'</td>'+
    '</tr>';
  }).join('');

  document.getElementById('detail').innerHTML=
    '<div class="row">'+
      '<pre class="q">'+esc(r.query||'（空查询）')+'</pre>'+
      '<div class="facts">'+
        '<span>时间 <b>'+ts(r.at)+'</b></span>'+
        '<span>命中 <b>'+hits.length+'</b></span>'+
        '<span>耗时 <b>'+num(r.elapsed_ms,1)+' ms</b></span>'+
        '<span>深度回忆 <b>'+(r.deep?'是':'否')+'</b></span>'+
      '</div>'+
      (hits.length?'<table><thead><tr>'+
        '<th>#</th><th>类型</th><th>id</th><th>等级</th><th>分数</th><th>向量/词面</th>'+
        '<th>活跃度</th><th>本次增量</th></tr></thead><tbody>'+rows+'</tbody></table>'
        :'<div class="empty">这次没有命中任何记忆。</div>')+
    '</div>';
}

function hot(){
  const stat=new Map();
  recs.forEach(r=>(r.hits||[]).forEach(h=>{
    const cur=stat.get(h.id)||{id:h.id,kind:h.kind,level:h.level,n:0,delta:0};
    cur.n+=1; cur.delta+=(h.delta||0);
    if(h.level!=null)cur.level=h.level;
    stat.set(h.id,cur);
  }));
  const top=[...stat.values()].sort((a,b)=>b.n-a.n||b.delta-a.delta).slice(0,40);
  const el=document.getElementById('hot');
  if(!top.length){el.innerHTML='<div class="empty">暂无</div>';return;}
  el.innerHTML=top.map(x=>
    '<div class="hot">'+
      '<span class="n">'+x.n+'</span>'+
      '<span class="k" style="background:'+(C[x.kind]||'#8898ad')+'"></span>'+
      '<span class="c id">'+esc(x.id)+'</span>'+
      '<span class="lv">L'+(x.level==null?'—':x.level)+'</span>'+
    '</div>').join('');
}

summary(); renderStats(); list(); hot(); renderDiag();
if(recs.length)detail(0);
</script>
</body>
</html>
"""
