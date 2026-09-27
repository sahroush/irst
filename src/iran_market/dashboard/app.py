"""Read-only MVP dashboard for registry, candles, signals and health."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from ..bootstrap import build
from ..indicators.supertrend import SupertrendIndicator
from ..monitoring.health import HealthService

HTML = r'''<!doctype html><html lang="en" dir="ltr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Iran Market Scanner</title><style>
:root{--bg:#0b0e14;--panel:#121722;--muted:#8b96a8;--text:#eef2f8;--line:#232b3a;--good:#35c48d;--bad:#ff6b6b;--accent:#6ea8fe}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 Inter,system-ui,sans-serif}.wrap{max-width:1280px;margin:auto;padding:24px}.top{display:flex;justify-content:space-between;gap:16px;align-items:center}.title{font-size:25px;font-weight:700}.sub{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:20px 0}.card,.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px}.card b{font-size:23px;display:block}.label{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.06em}.two{display:grid;grid-template-columns:1.25fr .75fr;gap:14px}.panel h2{font-size:15px;margin:0 0 12px}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px 8px;border-bottom:1px solid var(--line)}th{color:var(--muted);font-weight:500;font-size:12px}.bull{color:var(--good)}.bear{color:var(--bad)}input{background:#0d1119;color:var(--text);border:1px solid var(--line);border-radius:8px;padding:9px 11px;width:250px}.status{padding:4px 8px;border-radius:999px;background:#1a2230}.click{cursor:pointer}.click:hover{background:#171e2b}canvas{width:100%;height:240px;background:#0d1119;border-radius:8px}.detail{margin-top:14px}.hide{display:none}@media(max-width:850px){.grid{grid-template-columns:repeat(2,1fr)}.two{grid-template-columns:1fr}.top{align-items:flex-start;flex-direction:column}input{width:100%}}</style></head>
<body><div class="wrap"><div class="top"><div><div class="title">Iran Market Scanner</div><div class="sub">TSE/TSETMC · live candles · Supertrend signals</div></div><div><input id="search" placeholder="Search symbol…"><span id="healthBadge" class="status">loading</span></div></div>
<div class="grid"><div class="card"><span class="label">Active instruments</span><b id="active">-</b></div><div class="card"><span class="label">Building candles</span><b id="building">-</b></div><div class="card"><span class="label">Signals</span><b id="signalsCount">-</b></div><div class="card"><span class="label">Last snapshot</span><b id="snapshot" style="font-size:15px">-</b></div></div>
<div class="two"><section class="panel"><h2>Market universe / latest candles</h2><table><thead><tr><th>Symbol</th><th>Market</th><th>Close</th><th>Status</th><th>Updated</th></tr></thead><tbody id="inst"></tbody></table></section><section class="panel"><h2>Recent Supertrend flips</h2><table><thead><tr><th>Symbol</th><th>Direction</th><th>Time</th></tr></thead><tbody id="signals"></tbody></table></section></div>
<section id="detail" class="panel detail hide"><h2 id="detailTitle">Instrument</h2><canvas id="chart" width="1000" height="240"></canvas><div id="detailMeta" class="sub"></div></section></div>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
async function get(u){let r=await fetch(u);if(!r.ok)throw Error(await r.text());return r.json()}
function ago(iso){if(!iso)return'-';let n=(Date.now()-new Date(iso).getTime())/1000;if(n<60)return Math.max(0,Math.round(n))+'s ago';if(n<3600)return Math.round(n/60)+'m ago';return new Date(iso).toLocaleString()}
async function load(){let [h,i,s]=await Promise.all([get('/api/health'),get('/api/instruments?limit=100'),get('/api/signals?limit=30')]);active.textContent=h.active_instruments;building.textContent=h.building_candles;signalsCount.textContent=h.signals;snapshot.textContent=ago(h.last_snapshot_time);healthBadge.textContent=h.status;healthBadge.className='status '+(h.status==='HEALTHY'?'bull':h.status==='UNHEALTHY'?'bear':'');renderInst(i);renderSignals(s)}
function renderInst(rows){inst.innerHTML=rows.map(x=>`<tr class="click" onclick="detail('${x.id}')"><td>${esc(x.symbol||x.isin)}</td><td>${esc(x.market||'-')}</td><td>${esc(x.close??'-')}</td><td>${esc(x.candle_status||'-')}</td><td>${ago(x.updated_at)}</td></tr>`).join('')||'<tr><td colspan=5>No instruments yet. Run registry sync / scanner.</td></tr>'}
function renderSignals(rows){signals.innerHTML=rows.map(x=>`<tr><td>${esc(x.symbol||x.instrument_id)}</td><td class="${x.direction==='bullish'?'bull':'bear'}">${esc(x.direction)}</td><td>${ago(x.candle_time)}</td></tr>`).join('')||'<tr><td colspan=3>No signals yet.</td></tr>'}
async function detail(id){let d=await get('/api/instruments/'+id+'?timeframe=1h&limit=120');document.getElementById('detail').classList.remove('hide');detailTitle.textContent=(d.instrument.symbol||d.instrument.isin)+' · 1h';detailMeta.textContent='Supertrend: '+(d.supertrend.state||'UNKNOWN')+' · ATR: '+(d.supertrend.atr??'-');draw(d.candles,d.supertrend_series)}
function draw(cs,st){let c=document.getElementById('chart'),x=c.getContext('2d');x.clearRect(0,0,c.width,c.height);if(!cs.length)return;let vals=cs.flatMap(q=>[q.low,q.high]).concat(st.filter(v=>v!=null));let mn=Math.min(...vals),mx=Math.max(...vals),pad=(mx-mn||1)*.08;mn-=pad;mx+=pad;let X=i=>30+i*(c.width-60)/Math.max(1,cs.length-1),Y=v=>c.height-20-(v-mn)*(c.height-40)/(mx-mn);x.strokeStyle='#6ea8fe';x.lineWidth=2;x.beginPath();cs.forEach((q,i)=>i?x.lineTo(X(i),Y(q.close)):x.moveTo(X(i),Y(q.close)));x.stroke();x.strokeStyle='#35c48d';x.beginPath();let started=false;st.forEach((v,i)=>{if(v==null)return;if(!started){x.moveTo(X(i),Y(v));started=true}else x.lineTo(X(i),Y(v))});x.stroke()}
search.addEventListener('input',async e=>{let q=e.target.value.trim();renderInst(await get('/api/instruments?limit=100&q='+encodeURIComponent(q)))});load().catch(e=>console.error(e));setInterval(()=>load().catch(()=>{}),30000)
</script></body></html>'''


def create_app(container=None) -> FastAPI:
    c = container or build()
    app = FastAPI(title="Iran Market Scanner Dashboard", version="0.1.0")
    app.state.container = c

    @app.get("/", response_class=HTMLResponse)
    def root(): return HTML

    @app.get("/api/health")
    def health(): return HealthService(c).report()

    @app.get("/api/instruments")
    def instruments(limit: int = Query(100, ge=1, le=1000), q: str = ""):
        sql = """SELECT i.*, c.close, c.status candle_status, c.updated_at candle_updated
                 FROM instruments i LEFT JOIN candles c ON c.rowid=(SELECT c2.rowid FROM candles c2 WHERE c2.instrument_id=i.id AND c2.timeframe='1h' ORDER BY c2.start_time DESC LIMIT 1)
                 WHERE i.active=1"""
        params=[]
        if q:
            sql += " AND (i.symbol LIKE ? OR i.name LIKE ? OR i.isin LIKE ?)"; params += [f"%{q}%"]*3
        sql += " ORDER BY i.symbol LIMIT ?"; params.append(limit)
        rows=c.store.connect().execute(sql,params).fetchall()
        return [{"id":r["id"],"isin":r["isin"],"ins_code":r["ins_code"],"symbol":r["symbol"],"name":r["name"],"market":r["market"],"close":r["close"],"candle_status":r["candle_status"],"updated_at":r["candle_updated"] or r["updated_at"]} for r in rows]

    @app.get("/api/signals")
    def signals(limit: int = Query(50, ge=1, le=500)):
        rows=c.store.connect().execute("""SELECT s.*, i.symbol FROM signals s LEFT JOIN instruments i ON i.id=s.instrument_id ORDER BY s.candle_time DESC LIMIT ?""",(limit,)).fetchall()
        return [{"id":r["id"],"instrument_id":r["instrument_id"],"symbol":r["symbol"],"direction":r["direction"],"candle_time":r["candle_time"],"scanner_name":r["scanner_name"]} for r in rows]

    @app.get("/api/instruments/{instrument_id}")
    def instrument_detail(instrument_id: str, timeframe: str = "1h", limit: int = Query(120, ge=10, le=1000)):
        inst=c.instruments_repo.get(instrument_id)
        if inst is None: raise HTTPException(404,"instrument not found")
        cs=c.candles_repo.get_series(inst.id,timeframe)[-limit:]
        ind=SupertrendIndicator()
        result=ind.calculate(cs)
        _, st=ind._series([x for x in cs if x.status.value in ("CLOSED","RECONCILED")]) if cs else ([],[])
        return {"instrument":inst.model_dump(mode="json"),"candles":[x.model_dump(mode="json") for x in cs],"supertrend":{"state":result.state,"value":result.values.get("supertrend"),"atr":result.values.get("atr"),"valid":result.valid},"supertrend_series":st}

    return app

app = create_app()
