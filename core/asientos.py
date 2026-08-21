"""
Asientos contables (account.move de tipo 'entry') - crear y eliminar.

A diferencia de facturas/pagos, un asiento contable no es una traduccion 1:1 de
un registro de origen: es un conjunto de apuntes (debe/haber) sobre cuentas
contables que DEBEN cuadrar. Por eso este modulo tiene su propio flujo (esquema
fijo, como inventario.py) en vez de usar mappings.yaml + el sincronizador generico.

Flujo de creacion (crear_asiento):
  1. Idempotencia por asiento_id (state store, entidad "asiento").
  2. Resuelve el diario (por codigo) y cada cuenta de las lineas (por codigo).
  3. Valida que el asiento cuadre (suma debe == suma haber) ANTES de tocar Odoo.
  4. create del account.move (move_type='entry') con sus line_ids.
  5. Postea (action_post) salvo que se pida dejarlo en borrador (postear=False).

Eliminacion (eliminar_asiento):
  Un asiento POSTEADO no se puede borrar en Odoo. Se lleva primero a borrador
  (button_draft) y luego se elimina (unlink). El state store se marca ELIMINADO.

Idempotente por el campo de origen "asiento_id".
"""

import logging
import time
from decimal import Decimal

from core import mapper, state_store
from core.models_db import EstadoSync
from odoo_universal import OdooExecutionError, OdooUniversalAPI

logger = logging.getLogger("api-odoo")

ENTIDAD = "asiento"
MODEL = "account.move"

# Tolerancia de cuadre debe/haber: 1 centimo (absorbe ruido de coma flotante).
TOLERANCIA = Decimal("0.01")


class AsientoError(Exception):
    """Fallo al crear o eliminar un asiento contable (datos, cuadre u Odoo)."""
    pass


def _resolver_diario(odoo: OdooUniversalAPI, codigo: str) -> int:
    """Localiza el account.journal por su codigo. Obligatorio para el asiento."""
    if not codigo:
        raise AsientoError("Falta 'diario_codigo' (codigo del diario contable).")
    encontrados = odoo.execute(
        "account.journal", "search_read",
        [["code", "=", codigo]], fields=["id"], limit=1,
    )
    if not encontrados:
        raise AsientoError(f"No existe un diario (account.journal) con code='{codigo}'.")
    return encontrados[0]["id"]


def _cache_cuentas(odoo: OdooUniversalAPI, codigos: set[str]) -> dict[str, int]:
    """
    Resuelve un conjunto de cuentas contables por su codigo en un solo lote.
    Devuelve {codigo: id}. Las cuentas que no existan simplemente no aparecen.
    """
    codigos = {c for c in codigos if c}
    if not codigos:
        return {}
    # Odoo 17+ convirtio account.account.code en un campo COMPANY-DEPENDENT
    # (se almacena en code_store por compania y se calcula segun el contexto).
    # Un "in" sobre un campo company-dependent no es fiable, asi que se busca
    # cuenta a cuenta con "=" (que Odoo si sabe traducir a la compania activa) y
    # se normaliza el codigo devuelto para casar con el de origen.
    #
    # Como cuesta una llamada POR cuenta, se reutiliza la cache de datos
    # maestros del mapper: un asiento de 50 lineas sobre las mismas cuentas
    # pasa de 50 llamadas a las estrictamente nuevas.
    mapa: dict[str, int] = {}
    for codigo in codigos:
        clave = mapper._clave_cache(odoo, "account.account", "code", codigo)
        entrada = mapper._cache_fk.get(clave)
        if entrada is not None and time.monotonic() < entrada[0]:
            mapa[codigo] = entrada[1]
            continue

        encontradas = odoo.execute(
            "account.account", "search_read",
            [["code", "=", codigo]], fields=["id", "code"], limit=1,
        )
        if encontradas:
            id_cuenta = encontradas[0]["id"]
            mapa[codigo] = id_cuenta
            mapper._cache_fk[clave] = (
                time.monotonic() + mapper._CACHE_TTL_SEG, id_cuenta
            )
    return mapa


def _construir_lineas(odoo: OdooUniversalAPI, lineas: list[dict]) -> list:
    """
    Traduce las lineas de origen a comandos one2many de Odoo, resolviendo la
    cuenta por codigo y validando que el asiento cuadre.

    Cada linea de origen: {cuenta_codigo, debe, haber, concepto (opcional)}.
    Devuelve la lista de comandos [(0, 0, {...}), ...] lista para el create.
    Lanza AsientoError si falta una cuenta, si una linea es invalida o no cuadra.
    """
    if not lineas or len(lineas) < 2:
        raise AsientoError("Un asiento requiere al menos 2 lineas (debe y haber).")

    codigos = {str(l.get("cuenta_codigo", "")).strip() for l in lineas}
    mapa_cuentas = _cache_cuentas(odoo, codigos)

    comandos = []
    total_debe = Decimal("0")
    total_haber = Decimal("0")
    for i, l in enumerate(lineas, start=1):
        codigo = str(l.get("cuenta_codigo", "")).strip()
        if not codigo:
            raise AsientoError(f"Linea {i}: falta 'cuenta_codigo'.")
        if codigo not in mapa_cuentas:
            raise AsientoError(
                f"Linea {i}: no existe la cuenta contable con code='{codigo}'."
            )

        try:
            debe = Decimal(str(l.get("debe", 0) or 0))
            haber = Decimal(str(l.get("haber", 0) or 0))
        except Exception as e:
            raise AsientoError(f"Linea {i}: 'debe'/'haber' no numericos.") from e

        if debe < 0 or haber < 0:
            raise AsientoError(f"Linea {i}: 'debe'/'haber' no pueden ser negativos.")
        if debe > 0 and haber > 0:
            raise AsientoError(
                f"Linea {i}: una linea no puede tener 'debe' y 'haber' a la vez."
            )
        if debe == 0 and haber == 0:
            raise AsientoError(f"Linea {i}: la linea no tiene importe (debe/haber en 0).")

        total_debe += debe
        total_haber += haber
        comandos.append((0, 0, {
            "account_id": mapa_cuentas[codigo],
            "name": l.get("concepto") or l.get("name") or "/",
            "debit": float(debe),
            "credit": float(haber),
        }))

    if abs(total_debe - total_haber) > TOLERANCIA:
        raise AsientoError(
            f"El asiento no cuadra: debe={total_debe} vs haber={total_haber} "
            f"(diferencia={abs(total_debe - total_haber)})."
        )
    return comandos


def crear_asiento(registro: dict, odoo: OdooUniversalAPI) -> dict:
    """
    Crea (y por defecto postea) un asiento contable en Odoo.

    Campos del registro:
      asiento_id     (obligatorio) identificador unico del asiento en origen.
      diario_codigo  (obligatorio) codigo del account.journal (p.ej. "MISC").
      fecha          fecha del asiento (ISO, opcional; Odoo usa hoy si falta).
      referencia     texto libre (ref del asiento, opcional).
      lineas         (obligatorio) lista de {cuenta_codigo, debe, haber, concepto}.
      postear        si False, deja el asiento en borrador (por defecto True).

    Devuelve un dict con el resultado. Idempotente por asiento_id.
    Lanza AsientoError ante datos invalidos, descuadre o fallo de Odoo.
    """
    asiento_id = registro.get("asiento_id")
    if not asiento_id:
        raise AsientoError("Falta 'asiento_id' (identificador unico del asiento).")
    asiento_id = str(asiento_id)

    # 1. Idempotencia ATOMICA (reserva antes de tocar Odoo, sin ventana de
    # carrera entre el "compruebo" y el "creo").
    previo = state_store.reservar_estricto(
        ENTIDAD, asiento_id, model_odoo=MODEL,
        hash_payload=state_store.calcular_hash(registro),
    )
    if previo is not None:
        state_store.log(ENTIDAD, "idempotente", "OK", asiento_id, "Asiento ya creado")
        return {
            "asiento_id": asiento_id,
            "id_odoo": previo.id_odoo,
            "estado": previo.estado,
            "idempotente": True,
        }

    postear = registro.get("postear", True)

    # A partir de aqui se habla con Odoo. Un corte de conexion debe LIBERAR la
    # reserva: en PROCESANDO, reservar_estricto la rechazaria para siempre.
    with state_store.reserva_liberada_si_cae_odoo(ENTIDAD, asiento_id):
        # 2 y 3. Resolver diario/cuentas y validar cuadre (antes de tocar Odoo).
        try:
            journal_id = _resolver_diario(odoo, registro.get("diario_codigo"))
            line_ids = _construir_lineas(odoo, registro.get("lineas") or [])
        except AsientoError as e:
            state_store.marcar_estado(ENTIDAD, asiento_id, EstadoSync.ERROR, error=str(e))
            state_store.log(ENTIDAD, "validar", "ERROR", asiento_id, str(e))
            raise
        except OdooExecutionError as e:
            state_store.marcar_estado(ENTIDAD, asiento_id, EstadoSync.ERROR, error=str(e))
            state_store.log(ENTIDAD, "validar", "ERROR", asiento_id, str(e))
            raise AsientoError(f"Error de Odoo al resolver cuentas/diario: {e}") from e

        valores = {
            "move_type": "entry",
            "journal_id": journal_id,
            "ref": registro.get("referencia") or registro.get("ref") or "",
            "line_ids": line_ids,
        }
        if registro.get("fecha"):
            valores["date"] = registro["fecha"]

        # 4. CREATE.
        try:
            id_odoo = odoo.execute(MODEL, "create", valores)
            state_store.registrar_mapeo(
                ENTIDAD, asiento_id, model_odoo=MODEL, id_odoo=id_odoo,
                estado=EstadoSync.PROCESANDO,
            )
            state_store.log(ENTIDAD, "crear", "OK", asiento_id, f"id_odoo={id_odoo}")
        except OdooExecutionError as e:
            state_store.marcar_estado(ENTIDAD, asiento_id, EstadoSync.ERROR, error=str(e))
            state_store.log(ENTIDAD, "crear", "ERROR", asiento_id, str(e))
            raise AsientoError(f"Error al crear el asiento en Odoo: {e}") from e

        # 5. POST (opcional).
        if postear:
            try:
                odoo.execute(MODEL, "action_post", [id_odoo])
                state_store.log(ENTIDAD, "postear", "OK", asiento_id, f"id_odoo={id_odoo}")
            except OdooExecutionError as e:
                state_store.marcar_estado(
                    ENTIDAD, asiento_id, EstadoSync.ERROR,
                    error=f"Creado (id_odoo={id_odoo}) pero fallo action_post: {e}",
                )
                state_store.log(ENTIDAD, "postear", "ERROR", asiento_id, str(e))
                raise AsientoError(
                    f"Asiento creado en Odoo (id={id_odoo}) pero fallo al postear: {e}"
                ) from e

        state_store.marcar_estado(ENTIDAD, asiento_id, EstadoSync.PROCESADO)
        logger.info(
            "ASIENTO_OK | asiento=%s id_odoo=%s posteado=%s lineas=%d",
            asiento_id, id_odoo, postear, len(line_ids),
        )
        return {
            "asiento_id": asiento_id,
            "id_odoo": id_odoo,
            "estado": EstadoSync.PROCESADO.value,
            "posteado": bool(postear),
            "idempotente": False,
        }


def eliminar_asiento(
    asiento_id: str | None,
    odoo: OdooUniversalAPI,
    id_odoo: int | None = None,
) -> dict:
    """
    Elimina un asiento contable de Odoo.

    Se puede identificar por asiento_id (id de origen, resuelto en el state store)
    o directamente por id_odoo. Un asiento POSTEADO se lleva primero a borrador
    (button_draft) y luego se elimina (unlink).

    Marca el mapeo como ELIMINADO en el state store (si existe) para trazabilidad.
    Lanza AsientoError si no se encuentra o si Odoo rechaza la eliminacion.
    """
    ref = None
    if id_odoo is None:
        if not asiento_id:
            raise AsientoError("Indique 'asiento_id' o 'id_odoo' del asiento a eliminar.")
        asiento_id = str(asiento_id)
        mapa = state_store.buscar_mapeo(ENTIDAD, asiento_id)
        if mapa is None or mapa.id_odoo is None:
            raise AsientoError(
                f"No hay un asiento registrado con asiento_id='{asiento_id}'."
            )
        id_odoo = mapa.id_odoo
        ref = asiento_id
    ref = ref or str(id_odoo)

    # Comprobar el estado actual en Odoo (existe / posteado).
    try:
        datos = odoo.execute(MODEL, "read", [id_odoo], fields=["state", "name"])
    except OdooExecutionError as e:
        raise AsientoError(f"Error al leer el asiento id={id_odoo}: {e}") from e
    if not datos:
        raise AsientoError(f"El asiento id_odoo={id_odoo} no existe en Odoo.")
    estado_odoo = datos[0].get("state")

    try:
        # Un asiento posteado debe volver a borrador antes de poder eliminarlo.
        if estado_odoo == "posted":
            odoo.execute(MODEL, "button_draft", [id_odoo])
        odoo.execute(MODEL, "unlink", [id_odoo])
    except OdooExecutionError as e:
        state_store.log(ENTIDAD, "eliminar", "ERROR", ref, f"id_odoo={id_odoo}: {e}")
        raise AsientoError(f"Error al eliminar el asiento id={id_odoo}: {e}") from e

    # Marca el mapeo como ELIMINADO (si existia) para dejar rastro.
    if asiento_id:
        try:
            state_store.marcar_estado(
                ENTIDAD, asiento_id, EstadoSync.ELIMINADO,
                error=f"Asiento eliminado (id_odoo={id_odoo}).",
            )
        except KeyError:
            pass
    state_store.log(ENTIDAD, "eliminar", "OK", ref, f"id_odoo={id_odoo} eliminado")
    logger.info("ASIENTO_ELIMINADO | ref=%s id_odoo=%s", ref, id_odoo)
    return {"eliminado": True, "id_odoo": id_odoo, "asiento_id": asiento_id}
