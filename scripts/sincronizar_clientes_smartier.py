"""
Sincroniza los clientes de Smartier hacia Odoo (res.partner), a mano.

La logica vive en core/maestros_smartier.py, que es la MISMA que ejecuta Celery
Beat de forma automatica cada 15 minutos. Este script solo la envuelve con una
salida legible por terminal y el modo simulacion; asi no hay dos versiones de
las reglas de deduplicacion que puedan separarse con el tiempo.

Deduplicacion (seccion 6.2 del documento de integracion):
  1. Por RIF (Documento.Contenido) cuando existe: la llave fiable. Un contacto
     que ya exista -por ejemplo como proveedor- se REUTILIZA anadiendole el rol
     de cliente, nunca se duplica.
  2. Si no hay RIF, por el Id de Smartier guardado en res.partner.ref como
     "SMARTIER-<id>". NUNCA por nombre: varia en formato y mayusculas.

Uso:
    python scripts/sincronizar_clientes_smartier.py            # simulacion
    python scripts/sincronizar_clientes_smartier.py --aplicar  # escribe en Odoo
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from core import state_store  # noqa: E402
from core.maestros_smartier import (  # noqa: E402
    ResultadoMaestros,
    RUTA_CLIENTES,
    _tiene_localizacion_ve,
    buscar_en_odoo,
    cambios_para,
    rif_de,
    sincronizar_cliente,
)
from core.smartier_client import SmartierClient  # noqa: E402
from odoo_universal import (  # noqa: E402
    OdooConnectionError,
    OdooUniversalAPI,
)

APLICAR = "--aplicar" in sys.argv


def _simular(clientes: list, odoo) -> None:
    """
    Muestra que haria cada cliente sin escribir nada en Odoo.

    Reusa buscar_en_odoo y cambios_para -las mismas funciones que la pasada
    real- para que la simulacion no pueda mentir: si dijera una cosa y luego
    se hiciera otra, no serviria de nada.
    """
    creados = actualizados = iguales = 0
    for c in clientes:
        etiqueta = f"Smartier #{str(c.get('Id')):<4} " \
                   f"{str(c.get('RazonSocial') or c.get('Nombre'))[:30]:30}"
        existente, motivo = buscar_en_odoo(odoo, c)
        if existente is None:
            print(f"  + {etiqueta} se CREARIA")
            creados += 1
            continue
        cambios = cambios_para(c, existente)
        if cambios:
            print(f"  ~ {etiqueta} se ACTUALIZARIA {sorted(cambios)} ({motivo})")
            actualizados += 1
        else:
            print(f"  = {etiqueta} sin cambios ({motivo})")
            iguales += 1
        if not rif_de(c):
            print("      sin RIF: no se le podra facturar todavia")
    print(f"\nSimulacion: {creados} nuevo(s), {actualizados} a actualizar, "
          f"{iguales} sin cambios")


def main() -> int:
    try:
        odoo = OdooUniversalAPI(
            os.getenv("ODOO_URL"), os.getenv("ODOO_DB"),
            os.getenv("ODOO_USERNAME"), os.getenv("ODOO_PASSWORD"),
        )
    except OdooConnectionError as e:
        print(f"[X] No se pudo conectar a Odoo: {e}")
        return 1
    print(f"Odoo conectado (uid={odoo.uid})")

    if _tiene_localizacion_ve(odoo):
        print("Localizacion venezolana detectada: los contactos NUEVOS se crean")
        print("  sin retencion (wh_iva_agent=False, islr_withholding_agent=False).")
        print("  Contabilidad activa la retencion que corresponda a cada uno.")
    else:
        print("Sin localizacion venezolana: no hay campos de retencion que fijar.")

    state_store.init_db()

    cli = SmartierClient()
    try:
        clientes, total = cli.listar(RUTA_CLIENTES, page_size=200)
    except Exception as e:  # noqa: BLE001 - se reporta y se sale con codigo
        print(f"[X] No se pudieron leer los clientes de Smartier: {e}")
        return 1
    finally:
        cli.close()
    print(f"Smartier: {total} cliente(s)\n")

    if not APLICAR:
        print(">>> SIMULACION (usa --aplicar para escribir en Odoo)\n")
        _simular(clientes, odoo)
        print("\nNada se ha escrito. Repite con --aplicar para hacerlo efectivo.")
        return 0

    resultado = ResultadoMaestros(leidos=len(clientes))
    for c in clientes:
        if not rif_de(c):
            resultado.sin_rif += 1
        sincronizar_cliente(c, odoo, resultado)

    for linea in resultado.detalles:
        print(f"  {linea}")
    print(f"\nResumen: {resultado.creados} creado(s), "
          f"{resultado.actualizados} actualizado(s), "
          f"{resultado.sin_cambios} sin cambios, "
          f"{resultado.errores} con error")
    if resultado.sin_rif:
        print(f"\n[!] {resultado.sin_rif} contacto(s) sin RIF: no podran")
        print("    facturarse hasta que se cargue su identificacion fiscal.")
    return 1 if resultado.errores else 0


if __name__ == "__main__":
    raise SystemExit(main())
