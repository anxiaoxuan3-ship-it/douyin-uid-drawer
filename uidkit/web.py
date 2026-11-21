# -*- coding: utf-8 -*-
"""内置网页资源（直接内嵌成字符串，打包 exe 时不用额外带文件）。"""

INFO_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>抖音UID抽号器 · 接口面板</title>
<style>
  :root{color-scheme:dark}
  *{box-sizing:border-box}
  body{margin:0;background:#14161a;color:#e6e6e6;
       font:15px/1.7 "Microsoft YaHei UI","Microsoft YaHei",system-ui,sans-serif}
  .wrap{max-width:860px;margin:0 auto;padding:28px 22px 60px}
  h1{font-size:22px;margin:0 0 4px}
  .sub{color:#8b949e;font-size:13px;margin-bottom:22px}
  .card{background:#1c1f26;border:1px solid #2a2f38;border-radius:12px;
        padding:18px 20px;margin-bottom:16px}
  h2{font-size:15px;margin:0 0 12px;color:#7fe7ff;font-weight:600}
  code,kbd{background:#0e1116;border:1px solid #2a2f38;border-radius:6px;
           padding:1px 6px;font-family:Consolas,monospace;font-size:13px;color:#9ae6b4}
  table{width:100%;border-collapse:collapse;font-size:13.5px}
  td{padding:7px 6px;border-bottom:1px solid #23272f;vertical-align:top}
  td:first-child{width:180px;white-space:nowrap}
  tr:last-child td{border-bottom:0}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
  .stat{background:#0e1116;border:1px solid #2a2f38;border-radius:10px;padding:12px}
  .stat b{display:block;font-size:24px;color:#fff;line-height:1.3}
  .stat span{font-size:12px;color:#8b949e}
  input,textarea,button{font:inherit;border-radius:8px;border:1px solid #2a2f38;
        background:#0e1116;color:#e6e6e6;padding:9px 11px}
  input,textarea{width:100%}
  button{background:#2563eb;border-color:#2563eb;color:#fff;cursor:pointer;
         padding:9px 20px;font-weight:600}
  button:hover{background:#1d4ed8}
  button.ghost{background:#22262e;border-color:#333a45}
  .row{display:flex;gap:10px;align-items:center;margin-top:10px}
  #msg{font-size:13px;color:#9ae6b4;min-height:20px}
  .ok{color:#7CFC7C}.bad{color:#ff8080}
  a{color:#7fe7ff}
</style>
</head>
<body>
<div class="wrap">
  <h1>抖音UID抽号器 · 本地接口</h1>
  <div class="sub">这个页面由抽号器自己提供。关掉它不影响程序运行。</div>

  <div class="card">
    <h2>实时状态</h2>
    <div class="grid" id="stats"></div>
  </div>

  <div class="card">
    <h2>手动推一条弹幕试试</h2>
    <input id="t" placeholder="例如：100 591 822   （空格会被自动去掉）">
    <input id="n" placeholder="昵称（可选）" style="margin-top:8px">
    <div class="row">
      <button onclick="push()">推送</button>
      <button class="ghost" onclick="draw()">抽一次</button>
      <button class="ghost" onclick="reset()">清空参与池</button>
    </div>
    <div id="msg"></div>
  </div>

  <div class="card">
    <h2>接入口令（给第三方弹幕工具用）</h2>
    <table>
      <tr><td><code>GET /push</code></td>
          <td><code>/push?text=100 591 822&amp;nick=小明</code><br>
              最简单的推送方式，手机浏览器地址栏、书签、StreamDeck 都能用。</td></tr>
      <tr><td><code>POST /push</code></td>
          <td>body 直接放弹幕原文，或者放 JSON：<br>
              <code>{"text":"100 591 822","nick":"小明","uid":""}</code></td></tr>
      <tr><td><code>POST /push/batch</code></td>
          <td>一次推多条：<code>["100 591 822","100 591 823"]</code></td></tr>
      <tr><td><code>GET /status</code></td><td>当前状态 JSON。</td></tr>
      <tr><td><code>POST /draw?count=1</code></td><td>触发一次抽取。</td></tr>
      <tr><td><code>POST /undo?n=1</code></td><td>撤销最近的中奖记录。</td></tr>
      <tr><td><code>GET /overlay</code></td>
          <td><a id="ovlink" href="/overlay" target="_blank">浏览器透明叠加页</a> ——
              如果直播伴侣有「浏览器源」，可以直接加载这个地址。</td></tr>
    </table>
  </div>

  <div class="card">
    <h2>Python 一行接入示例</h2>
    <table>
      <tr><td>requests</td>
          <td><code>requests.post("__URL__/push", json={"text": danmaku, "nick": nick})</code></td></tr>
      <tr><td>标准库</td>
          <td><code>urllib.request.urlopen("__URL__/push?text=" + urllib.parse.quote(d))</code></td></tr>
    </table>
  </div>
</div>

<script>
const API = location.origin;
/* 本机访问不需要 token；从别的设备（手机）打开时把 token 带在网址后面，
   这里会自动加到每个请求上。 */
const TOKEN = new URLSearchParams(location.search).get('token') || '';
function api(path){
  if(!TOKEN) return path;
  return path + (path.indexOf('?')>=0 ? '&' : '?') + 'token=' + encodeURIComponent(TOKEN);
}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

async function refresh(){
  try{
    const s = await (await fetch(api('/status'))).json();
    document.getElementById('stats').innerHTML = [
      ['参与人数', s.pool_size],
      ['可抽人数', s.eligible],
      ['收到弹幕', s.danmaku_total],
      ['识别成功', s.matched],
      ['未识别', s.ignored],
      ['已中奖', s.winners_total],
      ['轮次', s.round],
    ].map(([k,v])=>`<div class="stat"><b>${v}</b><span>${k}</span></div>`).join('');
  }catch(e){}
}
function say(t,bad){const m=document.getElementById('msg');m.textContent=t;m.className=bad?'bad':'ok';}
async function push(){
  const text=document.getElementById('t').value, nick=document.getElementById('n').value;
  if(!text){return say('先写点东西',true);}
  const r=await fetch(api('/push'),{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({text,nick,src:'接口面板'})});
  const j=await r.json();
  say(j.uid?`识别到 UID ${j.uid}（${j.status}）`:`没识别到 UID：${j.reason||j.error||''}`, !j.uid);
  document.getElementById('t').value='';
  refresh();
}
async function draw(){
  const r=await fetch(api('/draw?count=1'),{method:'POST'});
  const j=await r.json();
  say(j.ok?`第 ${j.round} 轮：${j.uids.join('、')}`:(j.error||'抽取失败'), !j.ok);
  refresh();
}
async function reset(){
  if(!confirm('确定清空参与池？中奖记录会保留。'))return;
  await fetch(api('/reset'),{method:'POST'}); say('参与池已清空'); refresh();
}
document.getElementById('n').addEventListener('keydown',e=>{if(e.key==='Enter')push()});
document.getElementById('t').addEventListener('keydown',e=>{if(e.key==='Enter')push()});
const ov = document.getElementById('ovlink');
if(ov) ov.href = api('/overlay');
refresh(); setInterval(refresh, 1500);
</script>
</body>
</html>
"""


OVERLAY_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>UID抽号器 · 浏览器叠加页</title>
<style>
  html,body{margin:0;padding:0;width:100%;height:100%;overflow:hidden;
            background:transparent;}
  body{
    font-family:"Microsoft YaHei UI","Microsoft YaHei","PingFang SC",sans-serif;
    --uid-size:150px; --reel-size:110px; --title-size:40px;
    --count-size:30px; --win-size:26px;
    --uid-color:#fff; --title-color:#FFD54A; --count-color:#7FE7FF;
    --win-color:#7CFC7C; --outline:#101010; --ow:5px;
    --bg:transparent;
  }
  /* 纯色背景模式：给直播伴侣抠像用。默认透明，加了 ?bg=FF00FF 才铺色 */
  #bg{position:absolute;inset:0;background:var(--bg);z-index:0}
  #stage{position:relative;z-index:1;width:100%;height:100%;
         display:flex;flex-direction:column;align-items:center;
         justify-content:center;gap:.4em;padding:2vh 3vw;text-align:center}
  .out{
    color:var(--uid-color);
    text-shadow:
       calc(var(--ow)*-1) 0 0 var(--outline), var(--ow) 0 0 var(--outline),
       0 calc(var(--ow)*-1) 0 var(--outline), 0 var(--ow) 0 var(--outline),
       calc(var(--ow)*-.7) calc(var(--ow)*-.7) 0 var(--outline),
       calc(var(--ow)*.7) calc(var(--ow)*-.7) 0 var(--outline),
       calc(var(--ow)*-.7) calc(var(--ow)*.7) 0 var(--outline),
       calc(var(--ow)*.7) calc(var(--ow)*.7) 0 var(--outline);
  }
  #title{font-size:var(--title-size);font-weight:800;color:var(--title-color)}
  #uid{font-size:var(--uid-size);font-weight:900;letter-spacing:.04em;
       line-height:1.1;font-variant-numeric:tabular-nums;
       transition:transform .12s ease-out, color .12s}
  #uid.rolling{color:#cfd8ff}
  #uid.win{transform:scale(1.06);animation:pop .5s ease-out}
  @keyframes pop{0%{transform:scale(.85)}60%{transform:scale(1.12)}100%{transform:scale(1.06)}}
  #meta{font-size:var(--count-size);font-weight:700;color:var(--count-color)}
  #winners{font-size:var(--win-size);font-weight:700;color:var(--win-color);
           line-height:1.5;max-width:96vw}
  #winners .lbl{opacity:.65;font-weight:500;margin-right:.5em}
  #winners .w{display:inline-block;margin:0 .45em;white-space:nowrap}
  #winners .w.new{color:#FFD54A}
  #feed{position:absolute;right:2vw;top:6vh;width:26vw;text-align:right;
        font-size:20px;font-weight:600;opacity:.9;z-index:1;pointer-events:none}
  #feed div{color:#e8e8e8;text-shadow:0 0 3px #000,1px 1px 0 #000;
            white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
            margin-bottom:.25em;opacity:.85}
  .hide{display:none !important}
</style>
</head>
<body>
<div id="bg"></div>
<div id="feed" class="hide"></div>
<div id="stage">
  <div id="title" class="out">UID 抽号</div>
  <div id="uid" class="out">等待抽取…</div>
  <div id="meta" class="out"></div>
  <div id="winners" class="out"></div>
</div>

<script>
/* ------------------------------------------------------------------ *
 * 浏览器叠加页
 * 支持 URL 参数（都放在 /overlay 后面）：
 *   ?bg=FF00FF        纯色背景（给直播伴侣抠像），不填 = 真透明
 *   ?uid=150          中奖 UID 字号
 *   ?color=FFFFFF     UID 颜色（不带 #）
 *   ?title=UID抽号     标题文字，title=0 隐藏
 *   ?outline=101010   描边颜色
 *   ?ow=5             描边粗细(px)
 *   ?winners=8        中奖名单显示条数
 *   ?count=1          参与人数那行，=0 隐藏
 *   ?feed=1           右侧弹幕上墙
 * ------------------------------------------------------------------ */
const P = new URLSearchParams(location.search);
const num = (k,d)=>{const v=parseFloat(P.get(k));return isNaN(v)?d:v;};
const hex = (k,d)=>{const v=P.get(k);return v?('#'+v.replace('#','')):d;};
const show = (k,d)=>{const v=P.get(k);return v===null?d:(v!=='0'&&v!=='false');};

const root = document.documentElement;
root.style.setProperty('--uid-size', num('uid',150)+'px');
root.style.setProperty('--reel-size', num('reel',110)+'px');
root.style.setProperty('--title-size', num('titlesize',40)+'px');
root.style.setProperty('--count-size', num('countsize',30)+'px');
root.style.setProperty('--win-size', num('winsize',26)+'px');
root.style.setProperty('--uid-color', hex('color','#ffffff'));
root.style.setProperty('--title-color', hex('titlecolor','#FFD54A'));
root.style.setProperty('--count-color', hex('countcolor','#7FE7FF'));
root.style.setProperty('--win-color', hex('wincolor','#7CFC7C'));
root.style.setProperty('--outline', hex('outline','#101010'));
root.style.setProperty('--ow', num('ow',5)+'px');
if (P.get('bg')) root.style.setProperty('--bg', hex('bg','#FF00FF'));

const WIN_LIMIT = num('winners', 8);
const FEED_ON   = show('feed', false);
const COUNT_ON  = show('count', true);
const TITLE_ON  = show('title', true);

const elTitle = document.getElementById('title');
const elUid   = document.getElementById('uid');
const elMeta  = document.getElementById('meta');
const elWin   = document.getElementById('winners');
const elFeed  = document.getElementById('feed');

if (P.get('title')) elTitle.textContent = P.get('title');
if (!TITLE_ON) elTitle.classList.add('hide');
if (FEED_ON) elFeed.classList.remove('hide');

let lastRound = -1, animating = false, winners = [], feedSeen = new Set();

/* ---- 滚动动画：先快后慢，停在已知的中奖号上 ---- */
function roll(reel, finals, dur){
  animating = true;
  elUid.classList.remove('win');
  elUid.classList.add('rolling');
  const t0 = performance.now();
  const target = finals[0] || '';
  // 缓出曲线：前 70% 时间飞快滚动，最后 30% 明显减速
  function frame(now){
    const p = Math.min(1, (now - t0) / dur);
    if (p < 1){
      const eased = p < 0.72 ? p / 0.72 * 0.86 : 0.86 + Math.pow((p-0.72)/0.28, 2.2) * 0.14;
      elUid.style.setProperty('--uid-size', 'var(--reel-size)');
      const idx = Math.min(reel.length-1, Math.floor(eased * reel.length));
      elUid.textContent = reel[idx] || '……';
      requestAnimationFrame(frame);
    } else {
      elUid.style.setProperty('--uid-size', 'var(--uid-size)');
      elUid.textContent = finals.join('   ');
      elUid.classList.remove('rolling');
      elUid.classList.add('win');
      animating = false;
    }
  }
  if (!reel.length){ elUid.textContent = finals.join('   ');
                     elUid.classList.remove('rolling'); animating=false; return; }
  requestAnimationFrame(frame);
}
function duration(){ return Math.max(600, num('dur', 3600)); }

/* ---- 渲染 ---- */
function renderWinners(list, highlight){
  if (!WIN_LIMIT){ elWin.classList.add('hide'); return; }
  const rows = list.slice(-WIN_LIMIT);
  if (!rows.length){ elWin.textContent = ''; return; }
  elWin.innerHTML = '<span class="lbl">中奖</span>' + rows.map(w=>{
    const cls = (highlight && highlight.includes(w.uid)) ? 'w new' : 'w';
    return `<span class="${cls}">${w.uid}</span>`;
  }).join('');
}

function apply(s){
  if (COUNT_ON){
    elMeta.textContent = `参与 ${s.pool_size} 人 · 已抽 ${s.winners_total} 个`;
  } else elMeta.classList.add('hide');

  winners = s.winner_list || [];
  renderWinners(winners, null);

  const last = s.last;
  if (last && last.round !== lastRound){
    lastRound = last.round;
    const finals = last.uids || [];
    roll((last.reel||[]).slice(), finals, duration());
    setTimeout(()=>renderWinners(s.winner_list||[], finals), duration());
  } else if (!last && !animating){
    elUid.textContent = '等待抽取…';
  }

  if (FEED_ON){
    const fresh = (s.recent||[]).filter(r=>r.uid);
    elFeed.innerHTML = fresh.slice(-8).reverse()
      .map(r=>`<div>${r.uid}</div>`).join('');
  }
}

/* ---- 连接 ---- */
/* 本机访问不需要 token；从别的设备打开时把 ?token=xxx 带上，这里会自动透传。 */
const TOKEN = P.get('token') || '';
function api(path){
  if(!TOKEN) return path;
  return path + (path.indexOf('?')>=0 ? '&' : '?') + 'token=' + encodeURIComponent(TOKEN);
}
function connect(){
  const es = new EventSource(api('/events'));
  es.onmessage = e => { try{ apply(JSON.parse(e.data)); }catch(err){} };
  es.onerror = () => { es.close(); setTimeout(connect, 2000); };
}
fetch(api('/status')).then(r=>r.json()).then(apply).catch(()=>{});
connect();
</script>
</body>
</html>
"""
