"""
Orquestador de sincronizacion contable (Fase 3).

Conecta las tres piezas de las fases anteriores en un flujo idempotente:

    state_store (idempotencia)  ->  mapper (traduce)  ->  execute (crea + postea)

La funcion sincronizar_entidad() implementa el patron comun a facturas y pagos:

  1. IDEMPOTENCIA: si (entidad, id_origen) ya esta PROCESADO, devuelve el id_odoo
     sin volver a tocar Odoo.
  2. MARCA PROCESANDO (para trazabilidad y evitar carreras).
  3. MAPEA el registro de origen al esquema Odoo (resuelve datos maestros).
  4. CREA el registro en Odoo (create) -> obtiene id_odoo.
  5. POSTEA (action_post) para generar los asientos contables.
  6. MARCA PROCESADO y guarda la correlacion id_origen <-> id_odoo.

Cualquier fallo marca ERROR (con el mensaje) y re-lanza una excepcion tipada
para que la capa HTTP la traduzca al codigo adecuado.
"""

from dataclasses import dataclass

from core import impuestos, mapper, state_store
from core.models_db import EstadoSync
from odoo_universal import OdooExecutionError, OdooUniversalAPI


class SincronizacionError(Exception):
    """Fallo de negocio durante la sincronizacion (mapeo, create o post)."""
    pass


@dataclass
class ResultadoSync:
    """Resultado de una sincronizacion."""
    id_origen: str
    id_odoo: int
    estado: str
    idempotente: bool  # True si ya estaba procesado y no se toco Odoo


def sincronizar_entidad(
    entidad: str,
    registro: dict,
    odoo: OdooUniversalAPI,
) -> ResultadoSync:
    """
    Orquesta la sincronizacion de un registro de origen hacia Odoo.

    Parametros:
      entidad  - clave en mappings.yaml (p.ej. "factura", "pago").
      registro - datos crudos de la DB de origen.
      odoo     - conector del tenant correspondiente.

    Devuelve ResultadoSync. Lanza SincronizacionError ante cualquier fallo
    (el estado queda como ERROR en el state store).
    """
    # id de origen segun la config de mapeo (necesario para idempotencia).
    try:
        id_origen = mapper.id_origen_de(entidad, registro)
    except mapper.MapeoError as e:
        raise SincronizacionError(str(e)) from e

    # 1. IDEMPOTENCIA: si ya se proceso, no repetimos.
    id_odoo_existente = state_store.ya_procesado(entidad, id_origen)
    if id_odoo_existente is not None:
        state_store.log(
            entidad, "idempotente", "OK", id_origen,
            f"Ya procesado como id_odoo={id_odoo_existente}",
        )
        return ResultadoSync(
            id_origen=id_origen,
            id_odoo=id_odoo_existente,
            estado=EstadoSync.PROCESADO.value,
            idempotente=True,
        )

    conf = mapper.cargar_config()[entidad]
    model_odoo = conf["model"]
    hash_payload = state_store.calcular_hash(registro)

    # 2. Marca PROCESANDO (crea/actualiza el mapeo).
    state_store.registrar_mapeo(
        entidad, id_origen,
        model_odoo=model_odoo,
        estado=EstadoSync.PROCESANDO,
        hash_payload=hash_payload,
    )

    # 3. MAPEO (traduce y valida datos maestros).
    try:
        valores = mapper.mapear(entidad, registro, odoo)
    except mapper.MapeoError as e:
        state_store.marcar_estado(entidad, id_origen, EstadoSync.ERROR, error=str(e))
        state_store.log(entidad, "mapear", "ERROR", id_origen, str(e))
        raise SincronizacionError(f"Error de mapeo: {e}") from e

    # 4. CREATE en Odoo.
    try:
        id_odoo = odoo.execute(model_odoo, "create", valores)
        state_store.registrar_mapeo(
            entidad, id_origen,
            model_odoo=model_odoo, id_odoo=id_odoo,
            estado=EstadoSync.PROCESANDO, hash_payload=hash_payload,
        )
        state_store.log(entidad, "crear", "OK", id_origen, f"id_odoo={id_odoo}")
    except OdooExecutionError as e:
        state_store.marcar_estado(entidad, id_origen, EstadoSync.ERROR, error=str(e))
        state_store.log(entidad, "crear", "ERROR", id_origen, str(e))
        raise SincronizacionError(f"Error al crear en Odoo: {e}") from e

    # 5. POST (action_post) para generar los asientos contables.
    try:
        odoo.execute(model_odoo, "action_post", [id_odoo])
        state_store.log(entidad, "postear", "OK", id_origen, f"id_odoo={id_odoo}")
    except OdooExecutionError as e:
        # La factura/pago quedo creada pero en borrador. Se marca ERROR para
        # intervencion humana; el rollback automatico llegara en la Fase 5.
        state_store.marcar_estado(
            entidad, id_origen, EstadoSync.ERROR,
            error=f"Creado (id_odoo={id_odoo}) pero fallo action_post: {e}",
        )
        state_store.log(entidad, "postear", "ERROR", id_origen, str(e))
        raise SincronizacionError(
            f"Creado en Odoo (id={id_odoo}) pero fallo al postear: {e}"
        ) from e

    # 5b. VALIDACION DE TOTAL/IMPUESTOS (opcional, si la config lo pide).
    campo_total = conf.get("validar_total")
    if campo_total and registro.get(campo_total) is not None:
        try:
            impuestos.verificar_total(registro[campo_total], id_odoo, odoo)
            state_store.log(entidad, "validar_total", "OK", id_origen, f"id_odoo={id_odoo}")
        except impuestos.DescuadreError as e:
            state_store.marcar_estado(
                entidad, id_origen, EstadoSync.ERROR,
                error=f"Posteado (id_odoo={id_odoo}) pero descuadre de total: {e}",
            )
            state_store.log(entidad, "validar_total", "ERROR", id_origen, str(e))
            raise SincronizacionError(f"Descuadre de total en Odoo (id={id_odoo}): {e}") from e

    # 6. PROCESADO.
    state_store.marcar_estado(entidad, id_origen, EstadoSync.PROCESADO)
    return ResultadoSync(
        id_origen=id_origen,
        id_odoo=id_odoo,
        estado=EstadoSync.PROCESADO.value,
        idempotente=False,
    )
