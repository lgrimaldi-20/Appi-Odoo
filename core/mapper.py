"""
Data Mapper (Fase 2).

Traduce un registro de la base de datos de ORIGEN al esquema de Odoo, segun la
configuracion declarativa de core/mappings.yaml:

  1. Aplica valores por defecto (defaults, p.ej. move_type=out_invoice).
  2. Renombra campos 1:1 (campos: {campo_origen: campo_odoo}).
  3. Resuelve claves foraneas (resolver): busca en Odoo el id real de un partner,
     producto o diario a partir de un valor de negocio (NIF, codigo, etc.).

La resolucion de FKs usa el conector de transporte existente (OdooUniversalAPI),
por lo que el mapper valida de paso que los datos maestros existan en Odoo
(Paso 1.1 del diseno: "validar datos maestros").
"""

import os
from functools import lru_cache
import time
from typing import Any, Optional

import yaml

from odoo_universal import OdooUniversalAPI

# Ruta al YAML de mapeo (junto a este modulo).
_MAPPINGS_PATH = os.path.join(os.path.dirname(__file__), "mappings.yaml")


# ---------------------------------------------------------------------------
# Cache de datos maestros
# ---------------------------------------------------------------------------
# Resolver una FK cuesta un search_read a Odoo (~200-800 ms). Sincronizar N
# facturas del mismo cliente repetia N veces la misma busqueda. La cache guarda
# (tenant, model, campo, valor) -> id durante un TTL corto: los datos maestros
# (clientes, diarios, monedas) cambian poco, y un TTL bajo evita servir un id
# obsoleto si el maestro se recrea en Odoo.
#
# Se cachean SOLO los aciertos: un fallo puede ser un maestro que el cliente
# esta a punto de crear, y cachearlo daria errores fantasma.
_CACHE_TTL_SEG = 300
_cache_fk: dict[tuple, tuple[float, int]] = {}


def _clave_cache(odoo, model: str, campo: str, valor) -> tuple:
    """Clave de cache. Incluye la identidad del conector: dos tenants apuntan
    a bases Odoo distintas y NO pueden compartir ids. Se usa clave_tenant
    (url+db+uid), estable, y no id(), que CPython recicla."""
    tenant = getattr(odoo, "clave_tenant", None) or id(odoo)
    return (tenant, model, campo, str(valor))


def limpiar_cache_fk() -> None:
    """Vacia la cache de datos maestros (util en tests y tras recargas)."""
    _cache_fk.clear()


class MapeoError(Exception):
    """
    Error al mapear un registro de origen a Odoo: entidad desconocida, campo
    obligatorio ausente, o dato maestro no encontrado en Odoo.
    """
    pass


@lru_cache(maxsize=1)
def cargar_config(path: str = _MAPPINGS_PATH) -> dict:
    """
    Carga y cachea la configuracion de mapeo desde el YAML.
    Se cachea porque el archivo no cambia en tiempo de ejecucion.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data


def _config_entidad(entidad: str) -> dict:
    """Devuelve la seccion de config de una entidad o lanza MapeoError."""
    config = cargar_config()
    if entidad not in config:
        disponibles = sorted(config.keys())
        raise MapeoError(
            f"Entidad '{entidad}' sin mapeo definido. Disponibles: {disponibles}"
        )
    return config[entidad]


def id_origen_de(entidad: str, registro: dict) -> str:
    """
    Extrae el identificador de origen del registro segun la config
    (campo 'id_origen'). Necesario para la idempotencia del state store.
    """
    conf = _config_entidad(entidad)
    campo = conf.get("id_origen")
    if not campo or campo not in registro:
        raise MapeoError(
            f"El registro de '{entidad}' no trae el campo id_origen '{campo}'."
        )
    return str(registro[campo])


def _resolver_fk(
    odoo: OdooUniversalAPI,
    campo_odoo: str,
    regla: dict,
    registro: dict,
) -> Optional[int]:
    """
    Resuelve una clave foranea: busca en Odoo el id del registro maestro cuyo
    'buscar_por' == valor tomado de 'desde' en el registro de origen.

    Devuelve el id (int) o None si no es obligatorio y no hay valor de busqueda.
    Lanza MapeoError si es obligatorio y no se encuentra.
    """
    campo_origen = regla["desde"]
    model = regla["model"]
    buscar_por = regla["buscar_por"]
    obligatorio = regla.get("obligatorio", False)

    valor = registro.get(campo_origen)
    if valor is None or str(valor).strip() == "":
        if obligatorio:
            raise MapeoError(
                f"Campo obligatorio '{campo_origen}' ausente para resolver "
                f"'{campo_odoo}' ({model}.{buscar_por})."
            )
        return None

    # Cache: evita repetir el mismo search_read en lotes del mismo cliente.
    clave = _clave_cache(odoo, model, buscar_por, valor)
    entrada = _cache_fk.get(clave)
    if entrada is not None:
        caducado_en, id_cacheado = entrada
        if time.monotonic() < caducado_en:
            return id_cacheado
        del _cache_fk[clave]

    # Busca en Odoo: search_read por el campo indicado, limit=1.
    resultado = odoo.execute(
        model,
        "search_read",
        [[buscar_por, "=", valor]],
        fields=["id"],
        limit=1,
    )

    if not resultado:
        if obligatorio:
            raise MapeoError(
                f"No existe en Odoo un '{model}' con {buscar_por}='{valor}' "
                f"(requerido para '{campo_odoo}')."
            )
        return None

    id_encontrado = resultado[0]["id"]
    _cache_fk[clave] = (time.monotonic() + _CACHE_TTL_SEG, id_encontrado)
    return id_encontrado


def mapear(entidad: str, registro: dict, odoo: OdooUniversalAPI) -> dict[str, Any]:
    """
    Traduce un registro de origen al dict de valores listo para Odoo.

    Parametros:
      entidad  - clave en mappings.yaml (p.ej. "factura", "pago").
      registro - dict con los datos crudos de la base de datos de origen.
      odoo     - conector para resolver las claves foraneas.

    Devuelve el dict de valores Odoo (sin el id_origen).
    Lanza MapeoError si falta un campo obligatorio o un dato maestro.
    """
    conf = _config_entidad(entidad)

    # 1. Defaults (valores fijos)
    valores: dict[str, Any] = dict(conf.get("defaults", {}))

    # 2. Renombrado directo 1:1
    for campo_origen, campo_odoo in conf.get("campos", {}).items():
        if campo_origen in registro and registro[campo_origen] is not None:
            valores[campo_odoo] = registro[campo_origen]

    # 3. Resolucion de claves foraneas
    for campo_odoo, regla in conf.get("resolver", {}).items():
        fk = _resolver_fk(odoo, campo_odoo, regla, registro)
        if fk is not None:
            valores[campo_odoo] = fk

    return valores
