"""
Rollback logico / compensacion (Fase 5).

Odoo no ofrece transacciones distribuidas: si la factura se crea y postea pero
un paso posterior (pago o conciliacion) falla, hay que COMPENSAR dejando el
sistema en un estado consistente y auditable.

cancelar_factura() lleva un account.move de vuelta a un estado no contable:
  - button_draft  -> pasa de "posteado" a "borrador"
  - button_cancel -> lo marca como "cancelado"

Toda compensacion se registra en el state store (sync_log) para intervencion
humana. Un fallo al compensar NO se relanza como excepcion dura: se deja un log
de nivel ERROR muy explicito, porque el registro original ya era un error.
"""

import logging
import os

from core import state_store
from odoo_universal import OdooExecutionError, OdooUniversalAPI

logger = logging.getLogger("api-odoo")


def cancelar_pago(
    id_odoo: int,
    odoo: OdooUniversalAPI,
    entidad: str = "pago",
    id_origen: str = "",
    motivo: str = "",
) -> bool:
    """
    Compensa un pago ya creado en Odoo.

    account.payment NO tiene button_draft/button_cancel (eso es de
    account.move): comprobado contra Odoo 19, responde "the method does not
    exist". Usa action_cancel, que lo deja en 'canceled'.

    Devuelve True si se cancelo. No relanza excepciones: el registro original ya
    era un error, y un fallo al compensar debe quedar como log para revision.
    """
    ref = id_origen or str(id_odoo)
    try:
        odoo.execute("account.payment", "action_cancel", [id_odoo])
        state_store.log(
            entidad, "rollback", "OK", ref,
            f"Pago id_odoo={id_odoo} cancelado. Motivo: {motivo}",
        )
        logger.info("ROLLBACK_OK | entidad=%s ref=%s id_odoo=%s", entidad, ref, id_odoo)
        return True
    except OdooExecutionError as e:
        state_store.log(
            entidad, "rollback", "ERROR", ref,
            f"NO se pudo cancelar el pago id_odoo={id_odoo}: {e}. "
            f"INTERVENCION MANUAL. Motivo original: {motivo}",
        )
        logger.error("ROLLBACK_FALLO | entidad=%s ref=%s id_odoo=%s error=%s",
                     entidad, ref, id_odoo, e)
        return False


def cancelacion_descuadre_activa() -> bool:
    """
    Si hay que CANCELAR en Odoo los documentos que quedaron posteados pero
    descuadrados. Se lee en cada llamada (no al importar) para poder apagarlo
    en caliente. Por defecto activo.
    """
    return os.getenv("POLLER_CANCELAR_DESCUADRE", "true").strip().lower() not in (
        "false", "0", "no",
    )


def compensar_descuadre(
    error, odoo: OdooUniversalAPI, entidad: str, id_origen: str, origen: str = ""
) -> str:
    """
    Cancela en Odoo el documento de un SincronizacionError por descuadre, y
    devuelve el texto a anadir al mensaje de error (vacio si no habia nada que
    compensar).

    Por que hace falta: validar_total corre DESPUES del action_post, asi que un
    documento descuadrado queda posteado en Odoo -un asiento contable real- pese
    a que el middleware lo marque ERROR. Sin compensarlo queda contabilidad
    huerfana esperando una revision manual que nada garantiza.

    Solo se compensa el DESCUADRE, no cualquier fallo: es el unico en que consta
    que el documento esta objetivamente mal. Un fallo de action_post deja la
    factura en borrador (no contabiliza nada) y puede ser transitorio; uno de
    mapeo no llego a crear nada.

    Vive aqui, y no en cada llamador, porque los tres caminos que sincronizan
    (endpoint sincrono, tarea Celery y poller) necesitan exactamente la misma
    decision: tenerla duplicada hacia que el modo push dejara asientos sin
    cancelar mientras el poller si los cancelaba.
    """
    if not getattr(error, "descuadre", False):
        return ""
    id_odoo = getattr(error, "id_odoo", None)
    if not id_odoo or not cancelacion_descuadre_activa():
        return ""

    # Un pago no se cancela como una factura: modelos distintos, metodos
    # distintos (comprobado en Odoo 19).
    cancelar = cancelar_pago if entidad == "pago" else cancelar_factura
    cancelada = cancelar(
        id_odoo, odoo, entidad=entidad, id_origen=id_origen,
        motivo=f"Descuadre detectado{' por ' + origen if origen else ''}",
    )
    if cancelada:
        return f" | Documento id_odoo={id_odoo} CANCELADO automaticamente en Odoo."
    return (f" | NO se pudo cancelar id_odoo={id_odoo}: "
            f"REQUIERE INTERVENCION MANUAL.")


def cancelar_factura(
    id_odoo: int,
    odoo: OdooUniversalAPI,
    entidad: str = "factura",
    id_origen: str = "",
    motivo: str = "",
) -> bool:
    """
    Compensa una factura ya creada en Odoo (draft + cancel).

    Devuelve True si la compensacion se aplico, False si fallo (en cuyo caso
    queda un log ERROR para intervencion manual). No relanza excepciones.
    """
    ref = id_origen or str(id_odoo)
    try:
        # Primero a borrador (revierte los asientos), luego cancelar.
        odoo.execute("account.move", "button_draft", [id_odoo])
        odoo.execute("account.move", "button_cancel", [id_odoo])
        state_store.log(
            entidad, "rollback", "OK", ref,
            f"Factura id_odoo={id_odoo} cancelada. Motivo: {motivo}",
        )
        logger.info("ROLLBACK_OK | entidad=%s ref=%s id_odoo=%s", entidad, ref, id_odoo)
        return True
    except OdooExecutionError as e:
        # No se pudo compensar: requiere intervencion humana.
        state_store.log(
            entidad, "rollback", "ERROR", ref,
            f"NO se pudo cancelar id_odoo={id_odoo}: {e}. INTERVENCION MANUAL. "
            f"Motivo original: {motivo}",
        )
        logger.error(
            "ROLLBACK_FALLO | entidad=%s ref=%s id_odoo=%s error=%s",
            entidad, ref, id_odoo, e,
        )
        return False
