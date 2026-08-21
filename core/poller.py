"""
Poller: sincroniza desde la base de datos del CLIENTE hacia Odoo (modo pull).

Alternativa al modo push (el cliente hace POST a /facturas, /pagos...): aqui el
cliente deja registros en su propia base de datos (ver core/poller_source.py) y
el middleware los sondea periodicamente (Celery Beat) y los sincroniza.

Flujo de una pasada (procesar_lote):

  1. Lee un lote de filas PENDIENTE de la cola del cliente (FOR UPDATE SKIP
     LOCKED, para que varios workers no cojan la misma fila).
  2. Por cada fila llama a sincronizar_entidad() -> la MISMA maquinaria del modo
     push: idempotencia (sync_map), mapeo (mappings.yaml), create + post.
  3. Escribe el resultado de vuelta en la cola del cliente (PROCESADO | ERROR)
     para que el cliente lo vea sin llamar al middleware.

Doble fuente de verdad, resuelta: la cola del cliente dice QUE hay que enviar;
el sync_map del middleware dice QUE YA se envio. sincronizar_entidad consulta
sync_map antes de tocar Odoo (ya_procesado), asi que una fila reprocesada no
duplica nada en Odoo: solo se re-marca su resultado.

Politica de errores por fila (aislamiento): un fallo de DATOS en una fila la deja
en ERROR y el poller CONTINUA con las demas. Un fallo de CONEXION (Odoo caido)
aborta el lote entero y se propaga, para que Celery reintente la pasada completa
mas tarde (las filas ya procesadas son idempotentes).

Compensacion automatica del descuadre: la validacion de total corre DESPUES del
action_post, de modo que una factura descuadrada queda posteada en Odoo (asiento
contable real) aunque el middleware la marque ERROR. El poller la CANCELA
(rollback.cancelar_factura: button_draft + button_cancel) para no dejar
contabilidad huerfana. Solo se compensa el descuadre, no cualquier error: es el
unico caso en que consta que el registro esta mal. La fila queda en ERROR y NO
se reintenta sola —reintentarla recrearia y volveria a cancelar la misma factura
en bucle—; el cliente corrige el importe en origen y la reencola.
Se apaga con POLLER_CANCELAR_DESCUADRE=false.
"""

import logging
import os
from dataclasses import dataclass

from core import poller_source, state_store
from core.rollback import cancelar_factura
from core.sincronizador import SincronizacionError, sincronizar_entidad
from odoo_universal import OdooConnectionError, OdooUniversalAPI, get_tenant

logger = logging.getLogger("api-odoo")


def _cancelacion_activa() -> bool:
    """
    Si el poller debe CANCELAR en Odoo las facturas que quedaron posteadas pero
    descuadradas. Se lee en cada pasada (no al importar) para poder apagarlo en
    caliente. Por defecto activo.
    """
    return os.getenv("POLLER_CANCELAR_DESCUADRE", "true").strip().lower() not in (
        "false", "0", "no",
    )


@dataclass
class ResultadoLote:
    """Resumen de una pasada del poller."""
    leidas: int       # filas tomadas de la cola
    procesadas: int   # sincronizadas con exito (o ya idempotentes)
    con_error: int    # filas marcadas ERROR por fallo de datos


def _procesar_fila(fila: dict, odoo: OdooUniversalAPI) -> bool:
    """
    Sincroniza una fila de la cola. Devuelve True si termino en PROCESADO,
    False si quedo en ERROR por un fallo de datos.

    Re-lanza OdooConnectionError para que el llamador aborte el lote (Odoo esta
    caido: no tiene sentido seguir con el resto).
    """
    fila_id = fila["id"]
    entidad = fila["entidad"]
    id_origen = fila["id_origen"]
    payload = fila["payload"]

    try:
        resultado = sincronizar_entidad(entidad, payload, odoo)
        poller_source.marcar_resultado(fila_id, "PROCESADO")
        state_store.log(
            entidad, "poller", "OK", id_origen,
            f"cola#{fila_id} -> id_odoo={resultado.id_odoo}"
            + (" (idempotente)" if resultado.idempotente else ""),
        )
        return True
    except OdooConnectionError:
        # Fallo de conexion: no es culpa de la fila. Se deja PENDIENTE (no se
        # marca resultado) y se propaga para abortar el lote y reintentar luego.
        logger.warning(
            "Poller: Odoo no disponible al procesar cola#%s (%s/%s); "
            "se aborta el lote para reintentar.",
            fila_id, entidad, id_origen,
        )
        raise
    except SincronizacionError as e:
        # Fallo de datos (mapeo, create, post, descuadre): aisla la fila.
        detalle = str(e)

        # COMPENSACION: un descuadre de total deja la factura POSTEADA en Odoo
        # (la validacion ocurre despues del action_post). Ese asiento contable
        # es real y nadie lo dio por bueno, asi que se cancela para no dejar
        # contabilidad huerfana esperando revision manual.
        #
        # Solo se compensa el descuadre: es el unico fallo en que sabemos que
        # el registro esta objetivamente mal. Un fallo de action_post, por
        # ejemplo, deja la factura en borrador (no contabiliza nada) y puede
        # deberse a una causa transitoria.
        if getattr(e, "descuadre", False) and e.id_odoo and _cancelacion_activa():
            cancelada = cancelar_factura(
                e.id_odoo, odoo, entidad=entidad, id_origen=id_origen,
                motivo=f"Descuadre detectado por el poller (cola#{fila_id})",
            )
            detalle += (
                f" | Factura id_odoo={e.id_odoo} CANCELADA automaticamente en Odoo."
                if cancelada else
                f" | NO se pudo cancelar id_odoo={e.id_odoo}: REQUIERE INTERVENCION MANUAL."
            )

        # La fila queda en ERROR, no PENDIENTE: reintentarla sola volveria a
        # crear y cancelar la misma factura en bucle. El cliente corrige el
        # importe en su sistema y la reencola.
        poller_source.marcar_resultado(fila_id, "ERROR", error_detalle=detalle)
        state_store.log(entidad, "poller", "ERROR", id_origen, f"cola#{fila_id}: {detalle}")
        logger.info("Poller: fila cola#%s en ERROR: %s", fila_id, detalle)
        return False


def procesar_lote(tenant: str = "default", limite: int = 50) -> ResultadoLote:
    """
    Ejecuta una pasada del poller: toma un lote de la cola del cliente y lo
    sincroniza hacia Odoo. Devuelve un ResultadoLote con el resumen.

    Si el modo polling no esta configurado (sin SOURCE_DATABASE_URL), no hace
    nada y devuelve un lote vacio.
    """
    if not poller_source.polling_habilitado():
        logger.debug("Poller: SOURCE_DATABASE_URL no configurado; nada que hacer.")
        return ResultadoLote(leidas=0, procesadas=0, con_error=0)

    odoo = get_tenant(tenant)
    lote = poller_source.tomar_lote(limite)

    procesadas = 0
    con_error = 0
    for fila in lote:
        if _procesar_fila(fila, odoo):
            procesadas += 1
        else:
            con_error += 1

    if lote:
        logger.info(
            "Poller: lote de %s fila(s) -> %s ok, %s error.",
            len(lote), procesadas, con_error,
        )
    return ResultadoLote(leidas=len(lote), procesadas=procesadas, con_error=con_error)
