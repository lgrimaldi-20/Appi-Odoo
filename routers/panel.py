"""
Panel de OBSERVABILIDAD del middleware.

Expone:
  GET /panel                     -> dashboard HTML (shell sin datos; sin auth)
  GET /panel/api/resumen         -> tarjetas agregadas (JSON, requiere API Key)
  GET /panel/api/sincronizaciones-> listado de sync_map con filtros (JSON, API Key)
  GET /panel/api/logs            -> bitacora sync_log con filtros (JSON, API Key)
  GET /panel/api/detalle/{entidad}/{id_origen} -> mapeo + logs (JSON, API Key)

Seguridad: los endpoints de DATOS estan protegidos con la misma API Key
(X-Api-Key) que el resto del middleware. La pagina /panel en si es solo el shell
HTML (no lleva datos), y su JavaScript pide la clave y la envia en cada fetch.
"""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse

from core import observabilidad
from core.seguridad import verify_api_key

# Router de datos (protegido). Prefijo /panel/api.
router = APIRouter(prefix="/panel", tags=["Panel"])

datos = APIRouter(
    prefix="/panel/api", tags=["Panel"], dependencies=[Depends(verify_api_key)]
)


@datos.get("/resumen")
def api_resumen():
    """Resumen agregado para las tarjetas del panel."""
    return observabilidad.resumen()


@datos.get("/sincronizaciones")
def api_sincronizaciones(
    estado: str | None = Query(None, description="PROCESADO|ERROR|PROCESANDO|PENDIENTE"),
    entidad: str | None = Query(None, description="factura|pago|conciliacion|..."),
    id_origen: str | None = Query(None, description="Coincidencia parcial."),
    limite: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Listado de sync_map (estado de cada sincronizacion) con filtros."""
    return observabilidad.listar_sincronizaciones(
        estado=estado, entidad=entidad, id_origen=id_origen,
        limite=limite, offset=offset,
    )


@datos.get("/logs")
def api_logs(
    entidad: str | None = Query(None),
    id_origen: str | None = Query(None),
    resultado: str | None = Query(None, description="OK|ERROR"),
    limite: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Bitacora de auditoria (sync_log) con filtros."""
    return observabilidad.listar_logs(
        entidad=entidad, id_origen=id_origen, resultado=resultado,
        limite=limite, offset=offset,
    )


@datos.get("/detalle/{entidad}/{id_origen}")
def api_detalle(entidad: str, id_origen: str):
    """Detalle de un registro: su mapeo mas toda su bitacora."""
    return observabilidad.detalle_registro(entidad, id_origen)


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def panel_html():
    """Sirve el dashboard HTML (shell). Los datos se cargan via fetch con API Key."""
    return HTMLResponse(content=_PANEL_HTML)


# ---------------------------------------------------------------------------
# Dashboard HTML autocontenido (sin dependencias externas).
# ---------------------------------------------------------------------------

_PANEL_HTML = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Panel de sincronizacion | API-Odoo</title>
<style>
  :root {
    --bg:#0f172a; --panel:#1e293b; --panel2:#273449; --txt:#e2e8f0; --muted:#94a3b8;
    --border:#334155; --ok:#22c55e; --err:#ef4444; --proc:#f59e0b; --pend:#64748b;
    --accent:#38bdf8;
  }
  * { box-sizing:border-box; }
  body { margin:0; font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
    background:var(--bg); color:var(--txt); font-size:14px; }
  header { padding:16px 24px; background:var(--panel); border-bottom:1px solid var(--border);
    display:flex; align-items:center; gap:16px; flex-wrap:wrap; }
  header h1 { font-size:18px; margin:0; font-weight:600; }
  header .status { margin-left:auto; display:flex; gap:8px; align-items:center; color:var(--muted); }
  .status-pill { display:flex; gap:6px; align-items:center; }
  .dot { width:9px; height:9px; border-radius:50%; background:var(--pend); }
  .dot.ok { background:var(--ok); } .dot.err { background:var(--err); }
  main { padding:24px; max-width:1280px; margin:0 auto; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-bottom:24px; }
  .card { background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:16px; }
  .card .n { font-size:28px; font-weight:700; } .card .l { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.5px; }
  .card.ok .n{color:var(--ok);} .card.err .n{color:var(--err);} .card.proc .n{color:var(--proc);} .card.pend .n{color:var(--pend);}
  .toolbar { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:12px; align-items:center; }
  .pager { display:flex; gap:10px; align-items:center; justify-content:center;
           margin-top:14px; flex-wrap:wrap; }
  .pager button { background:#1e2a3a; color:#cfe3ff; border:1px solid #2c3e55;
                  border-radius:6px; padding:7px 14px; cursor:pointer; font-size:13px; }
  .pager button:hover:not(:disabled) { background:#27374b; }
  .pager button:disabled { opacity:.35; cursor:default; }
  .pager select { background:#1e2a3a; color:#cfe3ff; border:1px solid #2c3e55;
                  border-radius:6px; padding:6px 10px; font-size:13px; }
  input,select,button { background:var(--panel2); color:var(--txt); border:1px solid var(--border);
    border-radius:8px; padding:8px 12px; font-size:13px; }
  button { cursor:pointer; } button:hover { border-color:var(--accent); }
  button.primary { background:var(--accent); color:#04283a; border-color:var(--accent); font-weight:600; }
  .tabs { display:flex; gap:4px; margin-bottom:12px; border-bottom:1px solid var(--border); }
  .tab { padding:8px 16px; cursor:pointer; color:var(--muted); border-bottom:2px solid transparent; }
  .tab.active { color:var(--txt); border-bottom-color:var(--accent); }
  table { width:100%; border-collapse:collapse; background:var(--panel); border-radius:10px; overflow:hidden; }
  th,td { text-align:left; padding:10px 12px; border-bottom:1px solid var(--border); font-size:13px; vertical-align:top; }
  th { color:var(--muted); font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:.5px; }
  tr:hover td { background:var(--panel2); }
  .badge { padding:2px 9px; border-radius:99px; font-size:11px; font-weight:600; display:inline-block; }
  .badge.PROCESADO{background:rgba(34,197,94,.15);color:var(--ok);}
  .badge.ERROR{background:rgba(239,68,68,.15);color:var(--err);}
  .badge.PROCESANDO{background:rgba(245,158,11,.15);color:var(--proc);}
  .badge.PENDIENTE{background:rgba(100,116,139,.15);color:var(--pend);}
  .badge.OK{background:rgba(34,197,94,.15);color:var(--ok);}
  .mono { font-family:ui-monospace,Menlo,Consolas,monospace; font-size:12px; }
  .err-txt { color:#fca5a5; max-width:420px; }
  .muted { color:var(--muted); }
  .empty { text-align:center; padding:40px; color:var(--muted); }
  .gate { max-width:380px; margin:80px auto; background:var(--panel); border:1px solid var(--border);
    border-radius:12px; padding:28px; text-align:center; }
  .gate input { width:100%; margin:14px 0; }
  .hide { display:none; }
</style>
</head>
<body>
<div id="gate" class="gate">
  <h2>Panel API-Odoo</h2>
  <p class="muted">Introduce la API Key para ver el estado de las sincronizaciones.</p>
  <input id="key" type="password" placeholder="X-Api-Key" autocomplete="off">
  <button class="primary" onclick="entrar()" style="width:100%">Entrar</button>
  <p id="gerr" class="err-txt hide" style="margin-top:12px"></p>
</div>

<div id="app" class="hide">
<header>
  <h1>🔄 Panel de sincronizacion</h1>
  <div class="status">
    <div class="status-pill">
      <span id="poller-dot" class="dot"></span>
      <span id="poller-status" class="muted">Sin ejecución aún</span>
    </div>
    <span id="lastupd" class="muted"></span>
    <label class="muted" style="display:flex;gap:6px;align-items:center">
      <input type="checkbox" id="auto" style="width:auto"> auto
    </label>
    <button onclick="pollerAhora(this)">Poller ahora</button>
    <button onclick="cargarTodo()">Refrescar</button>
    <button onclick="salir()">Salir</button>
  </div>
</header>
<main>
  <div class="cards" id="cards"></div>

  <div class="tabs">
    <div class="tab active" data-tab="sync" onclick="verTab('sync')">Sincronizaciones</div>
    <div class="tab" data-tab="logs" onclick="verTab('logs')">Bitacora</div>
  </div>

  <div id="tab-sync">
    <div class="toolbar">
      <select id="f-estado" onchange="cargarSync(0)">
        <option value="">Todos los estados</option>
        <option>PROCESADO</option><option>ERROR</option>
        <option>PROCESANDO</option><option>PENDIENTE</option>
      </select>
      <input id="f-entidad" placeholder="entidad (factura, pago...)" oninput="debounce(()=>cargarSync(0))">
      <input id="f-idorigen" placeholder="id_origen (parcial)" oninput="debounce(()=>cargarSync(0))">
      <span id="sync-count" class="muted"></span>
    </div>
    <table>
      <thead><tr>
        <th>Estado</th><th>Entidad</th><th>ID origen</th><th>Modelo Odoo</th>
        <th>ID Odoo</th><th>Actualizado</th><th>Error</th>
      </tr></thead>
      <tbody id="sync-body"></tbody>
    </table>
    <div class="pager">
      <button onclick="pagSync(-1)" id="sync-prev">&laquo; Anterior</button>
      <span id="sync-pag" class="muted"></span>
      <button onclick="pagSync(1)" id="sync-next">Siguiente &raquo;</button>
      <select id="sync-tam" onchange="cargarSync(0)">
        <option value="50">50 por pagina</option>
        <option value="100" selected>100 por pagina</option>
        <option value="250">250 por pagina</option>
        <option value="500">500 por pagina</option>
      </select>
    </div>
  </div>

  <div id="tab-logs" class="hide">
    <div class="toolbar">
      <select id="l-resultado" onchange="cargarLogs(0)">
        <option value="">Todos</option><option>OK</option><option>ERROR</option>
      </select>
      
      <input id="l-entidad" placeholder="entidad" oninput="debounce(()=>cargarLogs(0))">
      <input id="l-idorigen" placeholder="id_origen (parcial)" oninput="debounce(()=>cargarLogs(0))">
      <span id="logs-count" class="muted"></span>
    </div>
    <table>
      <thead><tr>
        <th>Resultado</th><th>Entidad</th><th>ID origen</th><th>Accion</th>
        <th>Detalle</th><th>Fecha</th>
      </tr></thead>
      <tbody id="logs-body"></tbody>
    </table>
    <div class="pager">
      <button onclick="pagLogs(-1)" id="logs-prev">&laquo; Anterior</button>
      <span id="logs-pag" class="muted"></span>
      <button onclick="pagLogs(1)" id="logs-next">Siguiente &raquo;</button>
      <select id="logs-tam" onchange="cargarLogs(0)">
        <option value="50">50 por pagina</option>
        <option value="100" selected>100 por pagina</option>
        <option value="250">250 por pagina</option>
        <option value="500">500 por pagina</option>
      </select>
    </div>
  </div>
</main>
</div>

<script>
let APIKEY = sessionStorage.getItem("apikey") || "";
let tab = "sync";
let dbTimer = null, autoTimer = null;

function debounce(fn){ clearTimeout(dbTimer); dbTimer=setTimeout(fn,300); }

async function api(path, opts = {}){
  const r = await fetch(path, {
    ...opts,
    headers: {
      "X-Api-Key": APIKEY,
      ...(opts.headers || {})
    }
  });
  if(r.status===401) throw new Error("API Key invalida");
  if(!r.ok) throw new Error("Error "+r.status);
  return r.json();
}

function setPollerStatus(ok, text){
  const dot = document.getElementById("poller-dot");
  const out = document.getElementById("poller-status");
  dot.classList.toggle("ok", ok === true);
  dot.classList.toggle("err", ok === false);
  out.textContent = text;
}

async function pollerAhora(btn){
  if(btn) btn.disabled = true;
  try {
    const d = await api("/poller/ejecutar", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tenant: "default", limite: 50 })
    });
    setPollerStatus(true,
      `Poller: ${d.leidas} leidas · ${d.procesadas} procesadas · ${d.con_error} con error`);
    await cargarTodo();
  } catch (e) {
    setPollerStatus(false, e.message);
  } finally {
    if(btn) btn.disabled = false;
  }
}

function entrar(){
  APIKEY = document.getElementById("key").value.trim();
  api("/panel/api/resumen").then(()=>{
    sessionStorage.setItem("apikey", APIKEY);
    document.getElementById("gate").classList.add("hide");
    document.getElementById("app").classList.remove("hide");
    cargarTodo();
  }).catch(e=>{
    const g=document.getElementById("gerr"); g.textContent=e.message; g.classList.remove("hide");
  });
}
function salir(){ sessionStorage.removeItem("apikey"); location.reload(); }

function esc(s){ return (s===null||s===undefined)?"":String(s)
  .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }
function fecha(s){ return s? new Date(s).toLocaleString() : ""; }

async function cargarResumen(){
  const d = await api("/panel/api/resumen");
  const c = document.getElementById("cards");
  const e = d.por_estado;
  c.innerHTML = `
    <div class="card"><div class="n">${d.total}</div><div class="l">Total</div></div>
    <div class="card ok"><div class="n">${e.PROCESADO}</div><div class="l">Procesado</div></div>
    <div class="card err"><div class="n">${e.ERROR}</div><div class="l">Error</div></div>
    <div class="card proc"><div class="n">${e.PROCESANDO}</div><div class="l">Procesando</div></div>
    <div class="card pend"><div class="n">${e.PENDIENTE}</div><div class="l">Pendiente</div></div>
    <div class="card"><div class="n">${d.logs.total}</div><div class="l">Logs (${d.logs.errores} err)</div></div>`;
}

// Desplazamiento actual de cada tabla. La API pagina con limite+offset y
// devuelve el total, asi que el panel solo tiene que llevar la cuenta.
let offSync = 0, offLogs = 0;

function pagSync(dir){
  const tam = +document.getElementById("sync-tam").value;
  offSync = Math.max(0, offSync + dir*tam);
  cargarSync();
}

function pagLogs(dir){
  const tam = +document.getElementById("logs-tam").value;
  offLogs = Math.max(0, offLogs + dir*tam);
  cargarLogs();
}

// Pinta "N-M de T" y desactiva las flechas en los extremos.
function pintarPager(pre, off, tam, total){
  const desde = total ? off+1 : 0, hasta = Math.min(off+tam, total);
  document.getElementById(pre+"-pag").textContent =
    total ? `${desde}-${hasta} de ${total}` : "sin resultados";
  document.getElementById(pre+"-prev").disabled = off <= 0;
  document.getElementById(pre+"-next").disabled = hasta >= total;
}

async function cargarSync(reset){
  // Al filtrar o cambiar el tamano se vuelve a la primera pagina: mantener el
  // offset mostraria una pagina vacia si el nuevo filtro trae menos registros.
  if(reset===0) offSync = 0;
  const p = new URLSearchParams();
  const est=document.getElementById("f-estado").value;
  const ent=document.getElementById("f-entidad").value;
  const ido=document.getElementById("f-idorigen").value;
  const tam=+document.getElementById("sync-tam").value;
  if(est)p.set("estado",est); if(ent)p.set("entidad",ent); if(ido)p.set("id_origen",ido);
  p.set("limite",tam); p.set("offset",offSync);
  const d = await api("/panel/api/sincronizaciones?"+p.toString());
  // Si el offset se paso del final (p.ej. tras filtrar), retrocede a la ultima.
  if(!d.items.length && d.total && offSync >= d.total){
    offSync = Math.max(0, (Math.ceil(d.total/tam)-1)*tam);
    return cargarSync();
  }
  document.getElementById("sync-count").textContent = d.total+" registro(s)";
  pintarPager("sync", offSync, tam, d.total);
  const b=document.getElementById("sync-body");
  if(!d.items.length){ b.innerHTML=`<tr><td colspan="7" class="empty">Sin resultados</td></tr>`; return; }
  b.innerHTML = d.items.map(m=>`<tr>
    <td><span class="badge ${esc(m.estado)}">${esc(m.estado)}</span></td>
    <td>${esc(m.entidad)}</td>
    <td class="mono">${esc(m.id_origen)}</td>
    <td class="muted">${esc(m.model_odoo)}</td>
    <td class="mono">${m.id_odoo??""}</td>
    <td class="muted">${fecha(m.actualizado)}</td>
    <td class="err-txt">${esc(m.error)}</td>
  </tr>`).join("");
}

async function cargarLogs(reset){
  if(reset===0) offLogs = 0;
  const p = new URLSearchParams();
  const res=document.getElementById("l-resultado").value;
  const ent=document.getElementById("l-entidad").value;
  const ido=document.getElementById("l-idorigen").value;
  const tam=+document.getElementById("logs-tam").value;
  if(res)p.set("resultado",res); if(ent)p.set("entidad",ent); if(ido)p.set("id_origen",ido);
  p.set("limite",tam); p.set("offset",offLogs);
  const d = await api("/panel/api/logs?"+p.toString());
  if(!d.items.length && d.total && offLogs >= d.total){
    offLogs = Math.max(0, (Math.ceil(d.total/tam)-1)*tam);
    return cargarLogs();
  }
  document.getElementById("logs-count").textContent = d.total+" entrada(s)";
  pintarPager("logs", offLogs, tam, d.total);
  const b=document.getElementById("logs-body");
  if(!d.items.length){ b.innerHTML=`<tr><td colspan="6" class="empty">Sin resultados</td></tr>`; return; }
  b.innerHTML = d.items.map(l=>`<tr>
    <td><span class="badge ${esc(l.resultado)}">${esc(l.resultado)}</span></td>
    <td>${esc(l.entidad)}</td>
    <td class="mono">${esc(l.id_origen)}</td>
    <td>${esc(l.accion)}</td>
    <td class="${l.resultado==='ERROR'?'err-txt':'muted'}">${esc(l.detalle)}</td>
    <td class="muted">${fecha(l.timestamp)}</td>
  </tr>`).join("");
}

function verTab(t){
  tab=t;
  document.querySelectorAll(".tab").forEach(x=>x.classList.toggle("active",x.dataset.tab===t));
  document.getElementById("tab-sync").classList.toggle("hide",t!=="sync");
  document.getElementById("tab-logs").classList.toggle("hide",t!=="logs");
}

async function cargarTodo(){
  try{
    await cargarResumen();
    await cargarSync();
    await cargarLogs();
    document.getElementById("lastupd").textContent = "Actualizado "+new Date().toLocaleTimeString();
  }catch(e){
    if(e.message.includes("API Key")) salir();
  }
}

document.getElementById("auto").addEventListener("change", ev=>{
  if(ev.target.checked){ autoTimer=setInterval(cargarTodo,5000); }
  else{ clearInterval(autoTimer); }
});
document.getElementById("key")?.addEventListener("keydown",e=>{ if(e.key==="Enter")entrar(); });

// Autologin si ya hay clave en sesion.
if(APIKEY){ entrar(); }
</script>
</body>
</html>"""
