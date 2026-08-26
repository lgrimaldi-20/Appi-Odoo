"""
Prueba de CARGA del middleware: varios usuarios concurrentes + modo poller.

Simula un dia de operacion comprimido:

  Fase A (push)  N usuarios en paralelo mandan facturas en bolivares por HTTP,
                 con IVA resuelto por nombre. Se incluyen ids REPETIDOS a
                 proposito para provocar colisiones de idempotencia (409).
  Fase B (pull)  Se encolan M filas en la base de origen y se lanzan pasadas
                 del poller (POLLER_LIMITE por pasada), con descuadres
                 deliberados para ejercitar la compensacion automatica.

Mide latencias (media, p50, p95, max) y reparte los codigos de respuesta, para
ver si el middleware se degrada bajo carga.

Uso:
    python scripts/prueba_carga.py [USUARIOS] [FACTURAS_POR_USUARIO] [FILAS_COLA]
"""

import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter

BASE = os.getenv("API_URL_BASE", "http://127.0.0.1:8000")
PREFIJO = time.strftime("CARGA%m%d-%H%M")


def _api_key() -> str:
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
resultados = []   # lista de (codigo_http, segundos)


def llamar(ruta, cuerpo, timeout=120):
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
    except Exception as e:                     # timeout, conexion caida...
        return 0, {"error": str(e)}, time.time() - t0


def _factura(idx, usuario, repetida=False):
    """
    Factura en bolivares. Si `repetida`, reutiliza el id del usuario anterior
    para forzar una colision de idempotencia.
    """
    dueno = usuario - 1 if repetida and usuario > 0 else usuario
    base = 250.0 * (1 + idx % 4)               # 250, 500, 750, 1000
    return {
        "registro": {
            "factura_id": PREFIJO + "-U" + str(dueno) + "-F" + format(idx, "03d"),
            "cliente_nif": "B12345678",
            "fecha": "2026-08-26",
            "referencia": PREFIJO + " carga u" + str(dueno) + " f" + str(idx),
            "moneda_iso": "VES",
            "total": round(base * 1.15, 2),    # con IVA del 15%
            "lineas": [[0, 0, {"product_id": 2, "quantity": 1 + idx % 4,
                               "price_unit": 250.0, "impuestos": ["15%"]}]],
        }
    }


def usuario_worker(usuario, n_facturas):
    """Un 'usuario' del sistema de origen mandando facturas en serie."""
    for i in range(n_facturas):
        # 1 de cada 6 reenvia el id de otro usuario -> colision esperada
        repetida = (i % 6 == 5)
        codigo, _, tardo = llamar("/facturas", _factura(i, usuario, repetida))
        with _lock:
            resultados.append((codigo, tardo))


def _resumen(titulo, muestras, segundos):
    if not muestras:
        print("\n" + titulo + ": sin datos")
        return
    tiempos = sorted(t for _, t in muestras)
    codigos = Counter(c for c, _ in muestras)
    print("\n" + titulo)
    print("   peticiones      %d en %.1fs (%.1f/s)"
          % (len(muestras), segundos, len(muestras) / segundos))
    print("   latencia  media %7.0f ms" % (statistics.mean(tiempos) * 1000))
    print("             p50   %7.0f ms" % (tiempos[len(tiempos) // 2] * 1000))
    print("             p95   %7.0f ms" % (tiempos[int(len(tiempos) * 0.95)] * 1000))
    print("             max   %7.0f ms" % (tiempos[-1] * 1000))
    etiquetas = {200: "OK", 409: "409 colision idempotencia",
                 422: "422 error de datos", 429: "429 rate limit",
                 503: "503 Odoo caido", 500: "500 ERROR INTERNO",
                 0: "sin respuesta (timeout)"}
    print("   respuestas:")
    for cod, n in sorted(codigos.items()):
        print("      %-30s %4d" % (etiquetas.get(cod, cod), n))


def fase_push(usuarios, por_usuario):
    print("\n" + "=" * 70)
    print("FASE A - PUSH: %d usuarios concurrentes x %d facturas = %d"
          % (usuarios, por_usuario, usuarios * por_usuario))
    print("=" * 70)
    hilos = [threading.Thread(target=usuario_worker, args=(u, por_usuario))
             for u in range(usuarios)]
    t0 = time.time()
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    _resumen("RESULTADO FASE A", resultados, time.time() - t0)


def fase_pull(filas):
    """Encola filas en la DB de origen y lanza pasadas del poller."""
    print("\n" + "=" * 70)
    print("FASE B - PULL: %d filas en la cola del cliente" % filas)
    print("=" * 70)
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=True)
    from core import poller_source as ps

    ps.init_source_db()
    with ps.get_source_session() as s:
        for i in range(filas):
            # 1 de cada 12 va descuadrada: el poller debe cancelarla en Odoo
            descuadre = (i % 12 == 11)
            base = 250.0 * (1 + i % 4)
            total = round(base * 1.15, 2)
            ident = PREFIJO + "-POLL-" + format(i, "03d")
            s.add(ps.ColaSincronizacion(
                entidad="factura", id_origen=ident,
                payload={
                    "factura_id": ident,
                    "cliente_nif": "B12345678", "fecha": "2026-08-26",
                    "referencia": PREFIJO + " poller " + str(i),
                    "moneda_iso": "VES",
                    "total": round(total * 0.8, 2) if descuadre else total,
                    "lineas": [[0, 0, {"product_id": 2, "quantity": 1 + i % 4,
                                       "price_unit": 250.0, "impuestos": ["15%"]}]],
                },
                estado="PENDIENTE"))
        s.commit()
    print("   %d filas encoladas" % filas)

    total = {"leidas": 0, "procesadas": 0, "con_error": 0}
    pasada = 0
    t0 = time.time()
    while True:
        pasada += 1
        t1 = time.time()
        codigo, datos, _ = llamar("/poller/ejecutar", {}, timeout=900)
        if codigo != 200:
            print("   pasada %d: HTTP %s %s" % (pasada, codigo, datos))
            break
        leidas = datos.get("leidas", 0)
        for k in total:
            total[k] += datos.get(k, 0)
        print("   pasada %d: %s  (%.1fs)" % (pasada, datos, time.time() - t1))
        if leidas == 0:
            break
        if pasada >= 12:                      # tope de seguridad
            print("   (tope de pasadas alcanzado)")
            break

    seg = time.time() - t0
    print("\nRESULTADO FASE B")
    print("   %d filas en %.1fs (%.1f filas/s) en %d pasada(s)"
          % (total["leidas"], seg, total["leidas"] / seg if seg else 0, pasada))
    print("   procesadas %d   con_error %d" % (total["procesadas"], total["con_error"]))
    print("   (los errores esperados son los descuadres deliberados,")
    print("    que ademas deben quedar CANCELADOS en Odoo)")


def main():
    usuarios = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    por_usuario = int(sys.argv[2]) if len(sys.argv) > 2 else 15
    filas = int(sys.argv[3]) if len(sys.argv) > 3 else 150

    print("PRUEBA DE CARGA  prefijo=" + PREFIJO)
    print("Objetivo: %d facturas por HTTP + %d por poller"
          % (usuarios * por_usuario, filas))
    fase_push(usuarios, por_usuario)
    fase_pull(filas)
    print("\nPrefijo de esta tanda: " + PREFIJO)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
