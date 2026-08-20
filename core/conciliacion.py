"""
Conciliacion contable factura <-> pago (Fase 4).

En Odoo, un pago y una factura no se relacionan con un simple campo: se deben
CONCILIAR sus apuntes contables (account.move.line). Este modulo cruza la linea
por cobrar de la factura con la linea equivalente del pago.

Hay DOS mecanismos, y este modulo elige en tiempo de ejecucion el que aplique
para no atarse a una version concreta de Odoo:

  A) Conciliacion por apuntes (Odoo 17/18, y 19 cuando el pago ya tiene asiento):
     1. Localiza en la factura (account.move) su linea receivable/payable no conciliada.
     2. Localiza en el pago (account.payment) su apunte equivalente (move_id).
     3. Llama a reconcile() sobre ambas account.move.line.

  B) Vinculacion directa del pago (Odoo 19 con el pago aun sin asiento):
     En 19 el ciclo del pago es draft -> in_process -> paid, y mientras esta
     "in_process" NO tiene move_id: no hay apuntes que conciliar. Se enlaza
     entonces el pago existente a la factura escribiendo invoice_ids, y la
     factura pasa a payment_state 'in_payment'. El asiento se materializa
     despues, al casar el pago con el extracto bancario.
     NO se usa el asistente account.payment.register: ese CREA un pago nuevo y
     duplicaria el importe del que ya creo /pagos.

Se intenta (A) y, si el pago no tiene asiento, se recurre a (B). El resultado
indica cual se uso en la clave "mecanismo", para que el llamante lo sepa.

Idempotencia: el cruce se registra en el state store como entidad "conciliacion"
con id_origen = "<factura_id>:<pago_id>", de modo que no se concilia dos veces.

Las cuentas por cobrar/pagar se identifican por account.account.account_type:
  - asset_receivable  (facturas de cliente / cobros)
  - liability_payable (facturas de proveedor / pagos)
"""

from core import state_store
from core.models_db import EstadoSync
from odoo_universal import OdooExecutionError, OdooUniversalAPI

ENTIDAD = "conciliacion"

# Tipos de cuenta conciliables en Odoo.
TIPOS_CONCILIABLES = ("asset_receivable", "liability_payable")


class ConciliacionError(Exception):
    """Fallo al conciliar factura y pago."""
    pass


def _lineas_conciliables(odoo: OdooUniversalAPI, move_id: int) -> list[dict]:
    """
    Devuelve las account.move.line de un asiento (factura o pago) que estan en
    una cuenta por cobrar/pagar y aun no estan conciliadas.
    """
    # parent_state='posted': en Odoo 19 reconcile() rechaza apuntes en borrador,
    # asi que se filtran aqui para dar un error claro ("verifica que esten
    # posteados") en vez de que falle el reconcile mas adelante.
    dominio = [
        ["move_id", "=", move_id],
        ["account_id.account_type", "in", list(TIPOS_CONCILIABLES)],
        ["parent_state", "=", "posted"],
        ["reconciled", "=", False],
    ]
    lineas = odoo.execute(
        "account.move.line",
        "search_read",
        dominio,
        fields=["id", "account_id", "balance"],
    )
    return lineas or []


def conciliar(
    factura_id_odoo: int,
    pago_id_odoo: int,
    odoo: OdooUniversalAPI,
    factura_id_origen: str = "",
    pago_id_origen: str = "",
) -> dict:
    """
    Concilia una factura con un pago en Odoo.

    Parametros:
      factura_id_odoo / pago_id_odoo - IDs en Odoo (account.move de cada uno).
      odoo                           - conector del tenant.
      factura_id_origen/pago_id_origen - IDs de origen, para la clave idempotente
                                         y la auditoria (opcionales).

    Devuelve un dict con las lineas conciliadas. Idempotente: si ya se concilio
    esta pareja, devuelve el resultado previo sin volver a tocar Odoo.
    Lanza ConciliacionError si no encuentra lineas conciliables o Odoo falla.
    """
    clave = f"{factura_id_origen or factura_id_odoo}:{pago_id_origen or pago_id_odoo}"

    # Idempotencia ATOMICA. Se usa reservar_estricto (y no reservar) porque la
    # conciliacion no tiene un unico id_odoo: su mapeo lo deja en None.
    previo = state_store.reservar_estricto(
        ENTIDAD, clave, model_odoo="account.move.line"
    )
    if previo is not None:
        state_store.log(ENTIDAD, "idempotente", "OK", clave, "Ya conciliado")
        return {"conciliado": True, "idempotente": True, "clave": clave}

    # 1. Se busca el asiento del pago. Si no lo tiene (Odoo 19, pago
    # "in_process"), no hay apuntes que cruzar -> mecanismo B.
    try:
        pago_move_id = _move_id_de_pago(odoo, pago_id_odoo)
    except OdooExecutionError as e:
        state_store.marcar_estado(ENTIDAD, clave, EstadoSync.ERROR, error=str(e))
        state_store.log(ENTIDAD, "leer_pago", "ERROR", clave, str(e))
        raise ConciliacionError(f"Error al leer el pago: {e}") from e

    if pago_move_id is None:
        # --- Mecanismo B: asistente nativo (Odoo 19 sin asiento de pago) ---
        try:
            factura = _vincular_pago_existente(odoo, factura_id_odoo, pago_id_odoo)
        except OdooExecutionError as e:
            state_store.marcar_estado(ENTIDAD, clave, EstadoSync.ERROR, error=str(e))
            state_store.log(ENTIDAD, "vincular_pago", "ERROR", clave, str(e))
            raise ConciliacionError(f"Error al vincular el pago con la factura: {e}") from e

        estado_pago = factura.get("payment_state")
        if estado_pago not in ("in_payment", "paid", "partial"):
            msg = (
                f"No se pudo vincular el pago {pago_id_odoo}: la factura quedo "
                f"en payment_state={estado_pago!r}."
            )
            state_store.marcar_estado(ENTIDAD, clave, EstadoSync.ERROR, error=msg)
            state_store.log(ENTIDAD, "vincular_pago", "ERROR", clave, msg)
            raise ConciliacionError(msg)

        state_store.marcar_estado(ENTIDAD, clave, EstadoSync.PROCESADO)
        state_store.log(
            ENTIDAD, "vincular_pago", "OK", clave, f"payment_state={estado_pago}"
        )
        return {
            "conciliado": True,
            "idempotente": False,
            "clave": clave,
            "mecanismo": "vinculo_pago",
            "payment_state": estado_pago,
            "residual": factura.get("amount_residual"),
        }

    # --- Mecanismo A: conciliacion por apuntes (Odoo 17/18) ---
    try:
        lineas_factura = _lineas_conciliables(odoo, factura_id_odoo)
        lineas_pago = _lineas_conciliables(odoo, pago_move_id)
    except OdooExecutionError as e:
        state_store.marcar_estado(ENTIDAD, clave, EstadoSync.ERROR, error=str(e))
        state_store.log(ENTIDAD, "buscar_lineas", "ERROR", clave, str(e))
        raise ConciliacionError(f"Error al buscar apuntes: {e}") from e

    if not lineas_factura or not lineas_pago:
        msg = (
            f"Sin apuntes conciliables (factura={len(lineas_factura)}, "
            f"pago={len(lineas_pago)}). Verifica que ambos esten posteados."
        )
        state_store.marcar_estado(ENTIDAD, clave, EstadoSync.ERROR, error=msg)
        state_store.log(ENTIDAD, "buscar_lineas", "ERROR", clave, msg)
        raise ConciliacionError(msg)

    line_ids = [l["id"] for l in lineas_factura] + [l["id"] for l in lineas_pago]

    # reconcile() sobre el conjunto de apuntes.
    try:
        odoo.execute("account.move.line", "reconcile", line_ids)
    except OdooExecutionError as e:
        state_store.marcar_estado(ENTIDAD, clave, EstadoSync.ERROR, error=str(e))
        state_store.log(ENTIDAD, "reconcile", "ERROR", clave, str(e))
        raise ConciliacionError(f"Error al conciliar: {e}") from e

    state_store.marcar_estado(ENTIDAD, clave, EstadoSync.PROCESADO)
    state_store.log(ENTIDAD, "reconcile", "OK", clave, f"apuntes={line_ids}")
    return {
        "conciliado": True,
        "idempotente": False,
        "clave": clave,
        "mecanismo": "apuntes",
        "apuntes": line_ids,
    }


def _vincular_pago_existente(
    odoo: OdooUniversalAPI, factura_id_odoo: int, pago_id_odoo: int
) -> dict:
    """
    Mecanismo B: vincula un pago YA CREADO con la factura, para el caso de
    Odoo 19 en que el pago esta "in_process" y todavia no tiene asiento.

    Importante: NO se usa el asistente account.payment.register, porque ese
    CREA un pago nuevo — si el middleware ya creo el suyo via /pagos, se
    duplicaria el importe. Aqui se escribe invoice_ids en el pago existente,
    que es el campo (editable) con que Odoo 19 relaciona pago y factura.

    Devuelve el estado en que queda la factura.
    """
    factura_previa = odoo.execute(
        "account.move", "read", [factura_id_odoo], fields=["payment_state"]
    )
    if not factura_previa:
        raise ConciliacionError(f"La factura {factura_id_odoo} no existe en Odoo.")

    # (4, id) = enlazar un registro existente al many2many, sin tocar el resto.
    odoo.execute(
        "account.payment", "write", [pago_id_odoo],
        {"invoice_ids": [(4, factura_id_odoo)]},
    )

    factura = odoo.execute(
        "account.move", "read", [factura_id_odoo],
        fields=["payment_state", "amount_residual", "matched_payment_ids"],
    )
    return factura[0] if factura else {}


def _move_id_de_pago(odoo: OdooUniversalAPI, pago_id_odoo: int) -> int | None:
    """
    Obtiene el account.move asociado a un account.payment, o None si aun no
    tiene asiento.

    En Odoo 17/18 un pago posteado siempre tiene move_id. En Odoo 19 un pago
    "in_process" tiene move_id=False (el asiento se crea al casar el pago con
    el extracto bancario), y eso NO es un error: significa que hay que usar el
    mecanismo B. Por eso aqui se devuelve None en vez de lanzar excepcion.
    """
    datos = odoo.execute(
        "account.payment", "read", [pago_id_odoo], fields=["move_id"]
    )
    if not datos:
        raise ConciliacionError(f"El pago {pago_id_odoo} no existe en Odoo.")
    move_id = datos[0].get("move_id")
    if not move_id:
        return None
    # move_id llega como [id, "nombre"] en Odoo (campo many2one).
    return move_id[0] if isinstance(move_id, (list, tuple)) else move_id
