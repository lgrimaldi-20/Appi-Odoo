"""
Prueba de carga del CICLO DE COBRO completo: factura -> pago -> conciliacion.

A diferencia de prueba_carga.py (que solo emite facturas), aqui cada "usuario"
recorre el ciclo entero, que es lo que de verdad hace un cobro en produccion:

  Fase A (push)  N usuarios concurrentes, cada uno con M ciclos completos
                 (factura en bolivares -> pago del mismo importe -> conciliar).
                 Se inyectan descuadres de FACTURA y de PAGO por separado: los
                 pagos tambien validan importe desde que se anadio
                 campo_total_odoo, y conviene ejercitar ambos caminos.
  Fase B (pull)  Se encolan facturas y pagos en la cola del cliente y se lanza
                 el poller; despues se concilian por HTTP los pares que
                 llegaron bien (la conciliacion necesita los ids de Odoo, que
                 solo se conocen tras sincronizar).

Mide latencias por tipo de operacion y comprueba al final, contra Odoo, que
cada factura conciliada quedo en payment_state 'in_payment' y enlazada a SU
pago (no a otro).

Uso:
    python scripts/prueba_ciclo_cobro.py [USUARIOS] [CICLOS_POR_USUARIO] [CICLOS_POLLER]
"""

import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict

BASE = os.getenv("API_URL_BASE", "http://127.0.0.1:8000")
PREFIJO = time.strftime("CICLO%m%d-%H%M")


def _api_key():
    clave = os.getenv("API_KEY", "")
    if clave:
        return clave
    ruta = os.path.join(os.path.dirname(__file__), "..", ".env")
    with open(ruta, encoding="utf-8") as fh:
        for linea in fh:
            if linea.startswith("API_KEY="):
                return linea.split("=", 1)[1].strip()
    return ""


API_KEY = _api_key()
_lock = threading.Lock()
# {"factura": [(codigo, seg), ...], "pago": [...], "conciliar": [...]}
metricas = defaultdict(list)
# Pares (factura_id_odoo, pago_id_odoo) conciliados con exito, para verificar.
conciliados = []


def llamar(ruta, cuerpo, timeout=180):
    req = urllib.request.Request(
        BASE + ruta,
        data=json.dumps(cuerpo).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Api-Key": API_KEY},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read()), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), time.time() - t0
    except Exception as e:
        return 0, {"error": str(e)}, time.time() - t0


def _anota(op, codigo, seg):
    with _lock:
        metricas[op].append((codigo, seg))


def ciclo(usuario, idx):
    """
    Un ciclo de cobro completo. Devuelve True si llego a conciliar.

    Se descuadra la FACTURA en 1 de cada 4 ciclos y el PAGO en 1 de cada 5, para
    ejercitar las dos validaciones de importe por separado. Los divisores son
    pequenos a proposito: con modulos grandes (7, 9) y pocos ciclos por usuario
    la condicion no llega a cumplirse nunca y la prueba pasa sin haber probado
    nada — ocurrio en la primera version de este script.
    """
    ident = PREFIJO + "-U" + str(usuario) + "-" + format(idx, "02d")
    base = 250.0 * (1 + idx % 4)
    total = round(base * 1.15, 2)                 # con IVA del 15%

    fac_mala = (idx % 4 == 3)
    pago_malo = (idx % 5 == 4)

    # --- 1. Factura -------------------------------------------------------
    cod, fac, seg = llamar("/facturas", {"registro": {
        "factura_id": ident + "-FAC",
        "cliente_nif": "B12345678",
        "fecha": "2026-08-26",
        "referencia": PREFIJO + " ciclo u" + str(usuario) + " " + str(idx),
        "moneda_iso": "VES",
        # Un total que no cuadra con lo que sumaran las lineas en Odoo
        "total": round(total * 0.75, 2) if fac_mala else total,
        "lineas": [[0, 0, {"product_id": 2, "quantity": 1 + idx % 4,
                           "price_unit": 250.0, "impuestos": ["15%"]}]],
    }})
    _anota("factura", cod, seg)
    if cod != 200:
        return False

    # --- 2. Pago del mismo importe ---------------------------------------
    cod, pago, seg = llamar("/pagos", {"registro": {
        "pago_id": ident + "-PAG",
        "cliente_nif": "B12345678",
        "diario_codigo": "BNK1",
        # Importe que no coincide con el que registrara Odoo
        "monto": round(total * 0.5, 2) if pago_malo else total,
        "fecha": "2026-08-26",
        "moneda_iso": "VES",
    }})
    _anota("pago", cod, seg)
    if cod != 200:
        return False

    # --- 3. Conciliacion --------------------------------------------------
    cod, conc, seg = llamar("/conciliar", {
        "factura_id_odoo": fac.get("id_odoo"),
        "pago_id_odoo": pago.get("id_odoo"),
        "factura_id_origen": ident + "-FAC",
        "pago_id_origen": ident + "-PAG",
    })
    _anota("conciliar", cod, seg)
    if cod == 200:
        with _lock:
            conciliados.append((fac.get("id_odoo"), pago.get("id_odoo")))
        return True
    return False


def usuario_worker(usuario, ciclos):
    for i in range(ciclos):
        ciclo(usuario, i)


def _resumen_op(op, muestras):
    if not muestras:
        return
    tiempos = sorted(t for _, t in muestras)
    codigos = Counter(c for c, _ in muestras)
    reparto = "  ".join("%s:%d" % (c, n) for c, n in sorted(codigos.items()))
    print("   %-10s n=%-4d media %6.0f ms   p95 %6.0f ms   [%s]"
          % (op, len(muestras), statistics.mean(tiempos) * 1000,
             tiempos[int(len(tiempos) * 0.95)] * 1000, reparto))


def fase_push(usuarios, ciclos):
    print("\n" + "=" * 72)
    print("FASE A - PUSH: %d usuarios x %d ciclos = %d ciclos completos"
          % (usuarios, ciclos, usuarios * ciclos))
    print("          (cada ciclo = factura + pago + conciliacion)")
    print("=" * 72)
    hilos = [threading.Thread(target=usuario_worker, args=(u, ciclos))
             for u in range(usuarios)]
    t0 = time.time()
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    seg = time.time() - t0
    total_ops = sum(len(v) for v in metricas.values())
    print("\nRESULTADO FASE A")
    print("   %d operaciones en %.1fs (%.1f op/s)" % (total_ops, seg, total_ops / seg))
    for op in ("factura", "pago", "conciliar"):
        _resumen_op(op, metricas[op])
    print("   ciclos conciliados: %d de %d" % (len(conciliados), usuarios * ciclos))
    print("   (los no conciliados son los descuadres inyectados: 422)")


def fase_pull(ciclos):
    """Encola facturas y pagos, lanza el poller y concilia los pares buenos."""
    print("\n" + "=" * 72)
    print("FASE B - PULL: %d ciclos por la cola del cliente" % ciclos)
    print("=" * 72)
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=True)
    from core import poller_source as ps

    ps.init_source_db()
    with ps.get_source_session() as s:
        for i in range(ciclos):
            ident = PREFIJO + "-POLL-" + format(i, "02d")
            base = 250.0 * (1 + i % 4)
            total = round(base * 1.15, 2)
            s.add(ps.ColaSincronizacion(
                entidad="factura", id_origen=ident + "-FAC",
                payload={
                    "factura_id": ident + "-FAC", "cliente_nif": "B12345678",
                    "fecha": "2026-08-26",
                    "referencia": PREFIJO + " poller ciclo " + str(i),
                    "moneda_iso": "VES", "total": total,
                    "lineas": [[0, 0, {"product_id": 2, "quantity": 1 + i % 4,
                                       "price_unit": 250.0, "impuestos": ["15%"]}]],
                }, estado="PENDIENTE"))
            s.add(ps.ColaSincronizacion(
                entidad="pago", id_origen=ident + "-PAG",
                payload={
                    "pago_id": ident + "-PAG", "cliente_nif": "B12345678",
                    "diario_codigo": "BNK1", "monto": total,
                    "fecha": "2026-08-26", "moneda_iso": "VES",
                }, estado="PENDIENTE"))
        s.commit()
    print("   %d filas encoladas (%d facturas + %d pagos)"
          % (ciclos * 2, ciclos, ciclos))

    total = {"leidas": 0, "procesadas": 0, "con_error": 0}
    pasada = 0
    t0 = time.time()
    while pasada < 12:
        pasada += 1
        t1 = time.time()
        cod, datos, _ = llamar("/poller/ejecutar", {}, timeout=900)
        if cod != 200:
            print("   pasada %d: HTTP %s %s" % (pasada, cod, datos))
            break
        for k in total:
            total[k] += datos.get(k, 0)
        print("   pasada %d: %s  (%.1fs)" % (pasada, datos, time.time() - t1))
        if datos.get("leidas", 0) == 0:
            break
    seg = time.time() - t0
    print("\n   %d filas en %.1fs -> procesadas %d, con_error %d"
          % (total["leidas"], seg, total["procesadas"], total["con_error"]))

    # --- Conciliar los pares que se sincronizaron bien --------------------
    print("\n   conciliando los pares sincronizados...")
    from core import state_store
    ok = fallo = 0
    for i in range(ciclos):
        ident = PREFIJO + "-POLL-" + format(i, "02d")
        f = state_store.buscar_mapeo("factura", ident + "-FAC")
        p = state_store.buscar_mapeo("pago", ident + "-PAG")
        if not (f and f.id_odoo and p and p.id_odoo):
            continue
        cod, _, seg = llamar("/conciliar", {
            "factura_id_odoo": f.id_odoo, "pago_id_odoo": p.id_odoo,
            "factura_id_origen": ident + "-FAC", "pago_id_origen": ident + "-PAG",
        })
        _anota("conciliar_poller", cod, seg)
        if cod == 200:
            ok += 1
            with _lock:
                conciliados.append((f.id_odoo, p.id_odoo))
        else:
            fallo += 1
    print("   conciliados %d   fallidos %d" % (ok, fallo))
    _resumen_op("conciliar", metricas["conciliar_poller"])


def verificar():
    """Contra Odoo: cada factura conciliada debe apuntar a SU pago."""
    print("\n" + "=" * 72)
    print("VERIFICACION EN ODOO")
    print("=" * 72)
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=True)
    from odoo_universal import OdooUniversalAPI

    o = OdooUniversalAPI(os.getenv("ODOO_URL"), os.getenv("ODOO_DB"),
                         os.getenv("ODOO_USERNAME"), os.getenv("ODOO_PASSWORD"))
    if not conciliados:
        print("   (no hubo conciliaciones que verificar)")
        return

    ids_fac = [f for f, _ in conciliados]
    docs = {d["id"]: d for d in o.execute(
        "account.move", "read", ids_fac,
        fields=["name", "state", "payment_state", "matched_payment_ids"])}

    estados = Counter(d["payment_state"] for d in docs.values())
    print("   facturas conciliadas: %d" % len(conciliados))
    print("   payment_state:", dict(estados))

    # Lo importante: la factura debe estar enlazada a SU pago, no a otro.
    mal = [(f, p) for f, p in conciliados
           if p not in (docs.get(f, {}).get("matched_payment_ids") or [])]
    if mal:
        print("   *** %d factura(s) enlazadas al pago EQUIVOCADO: %s" % (len(mal), mal[:5]))
    else:
        print("   enlace factura<->pago correcto en las %d" % len(conciliados))


def main():
    usuarios = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    ciclos = int(sys.argv[2]) if len(sys.argv) > 2 else 6
    poller = int(sys.argv[3]) if len(sys.argv) > 3 else 30

    print("PRUEBA DE CICLO DE COBRO  prefijo=" + PREFIJO)
    print("Objetivo: %d ciclos por HTTP + %d por poller"
          % (usuarios * ciclos, poller))
    fase_push(usuarios, ciclos)
    fase_pull(poller)
    verificar()
    print("\nPrefijo de esta tanda: " + PREFIJO)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
