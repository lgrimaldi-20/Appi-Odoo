"""
Sincronizacion de DATOS MAESTROS de Smartier hacia Odoo (clientes).

Por que este modulo existe: la logica vivia solo dentro de
scripts/sincronizar_clientes_smartier.py, un CLI. Eso obligaba a que alguien
se acordara de ejecutarlo, y cuando llegaba una nota de entrega de un cliente
que todavia no estaba en Odoo, la nota quedaba en ERROR esperando. Al estar
aqui, la misma logica la puede invocar Celery Beat (automatico) y el script
(manual), sin duplicarla.

Orden que importa: los clientes se sincronizan ANTES que las notas de entrega.
Una nota necesita que su cliente exista en Odoo para resolver el partner_id;
al reves, la nota falla.

Que NO hace, a proposito:
  - No decide condicion fiscal. Los campos de retencion se fijan neutros al
    CREAR (el modulo venezolano los trae en True por defecto) y no se tocan
    nunca al actualizar: son competencia de contabilidad.
  - No borra. Un cliente deshabilitado en Smartier se ARCHIVA en Odoo
    (active=False), conservando su historial.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from core import state_store
from core.models_db import EstadoSync
from core.smartier_client import SmartierClient
from odoo_universal import OdooExecutionError, OdooUniversalAPI

logger = logging.getLogger(__name__)

RUTA_CLIENTES = "/external/clientes"

# Prefijo de la referencia externa en Odoo: permite localizar despues que
# contactos vinieron de Smartier y con que Id.
PREFIJO_REF = "SMARTIER-"

# Entidad del state store: con esto la sincronizacion de contactos aparece en
# el panel igual que las facturas, en vez de ser una escritura invisible.
ENTIDAD = "cliente"

# Estado de Smartier que archiva el contacto en Odoo.
ESTADO_DESHABILITADO = "Deshabilitado"

# Tipo de Smartier -> company_type de Odoo.
TIPO_COMPANY_TYPE = {"Contacto": "person", "Empresa": "company"}

NOTA_SIN_RIF = (
    "[Sincronizado desde Smartier] PENDIENTE DE VALIDACION FISCAL: "
    "este contacto no tiene RIF en Smartier. Odoo rechazara su factura "
    "hasta que contabilidad complete la identificacion fiscal."
)
NOTA_CON_RIF = "[Sincronizado desde Smartier]"

# Campos que el sincronizador escribe al ACTUALIZAR un contacto que ya existe.
#
# 'comment' esta deliberadamente FUERA: es un campo de texto libre donde
# contabilidad escribe sus propias notas, y una pasada de sincronizacion no
# debe apropiarse de el. Solo se rellena al crear, como pista inicial.
#
# Los campos de retencion tampoco estan: los fija contabilidad a la vista de
# la designacion del SENIAT, y reescribirlos revertiria esa decision.
CAMPOS_ACTUALIZABLES = ("name", "email", "active", "company_type", "vat", "ref")


class MaestrosError(Exception):
    """Fallo al sincronizar datos maestros desde Smartier."""


@dataclass
class ResultadoMaestros:
    """Recuento de una pasada, para el log y la respuesta del endpoint."""
    leidos: int = 0
    creados: int = 0
    actualizados: int = 0
    sin_cambios: int = 0
    errores: int = 0
    sin_rif: int = 0
    detalles: list = field(default_factory=list)


# Cache de si l10n_ve_full esta instalado. None = todavia sin comprobar.
_LOCALIZACION_VE: Optional[bool] = None


def _tiene_localizacion_ve(odoo: OdooUniversalAPI) -> bool:
    """
    True si l10n_ve_full esta instalado (aporta los campos de retencion).

    Se comprueba por la existencia del CAMPO y no por el nombre del modulo: lo
    que importa es si se puede escribir, no como se llame el addon.
    """
    global _LOCALIZACION_VE
    if _LOCALIZACION_VE is None:
        try:
            _LOCALIZACION_VE = bool(odoo.execute(
                "ir.model.fields", "search_count",
                [["model", "=", "res.partner"], ["name", "=", "wh_iva_agent"]],
            ))
        except OdooExecutionError:
            # Ante la duda se asume que NO: enviar campos inexistentes haria
            # fallar el create entero.
            _LOCALIZACION_VE = False
    return _LOCALIZACION_VE


def rif_de(cliente: dict) -> Optional[str]:
    """Identificacion fiscal del cliente de Smartier, o None si viene vacia."""
    contenido = (cliente.get("Documento") or {}).get("Contenido")
    texto = str(contenido).strip() if contenido is not None else ""
    return texto or None


def esta_activo(cliente: dict) -> bool:
    """
    Traduce el Estado de Smartier al archivado de Odoo (res.partner.active).

    Se comprueba el valor NEGATIVO en vez de dar por bueno 'Habilitado': si
    Smartier anade manana un estado que hoy no conocemos, el contacto queda
    visible en Odoo y no archivado en silencio. Un falso activo se ve y se
    corrige; un archivado por error desaparece y nadie lo nota.
    """
    return str(cliente.get("Estado") or "").strip() != ESTADO_DESHABILITADO


def valores_de(cliente: dict) -> dict:
    """Traduce un cliente de Smartier a campos de res.partner."""
    rif = rif_de(cliente)
    valores = {
        "name": cliente.get("RazonSocial") or cliente.get("Nombre") or "Sin nombre",
        "ref": f"{PREFIJO_REF}{cliente['Id']}",
        "customer_rank": 1,          # marca el rol de CLIENTE
        "active": esta_activo(cliente),
    }
    if cliente.get("Email"):
        valores["email"] = cliente["Email"]
    # Solo se envia el RIF cuando Smartier trae valor: mandarlo vacio borraria
    # uno cargado a mano en Odoo.
    if rif:
        valores["vat"] = rif
    # Smartier ya distingue Contacto/Empresa; se usa ese dato en vez de
    # deducirlo de si hay RazonSocial (un contacto puede tenerla sin ser
    # empresa). Solo se cae en RazonSocial si el Tipo viniera vacio.
    tipo = str(cliente.get("Tipo") or "").strip()
    if tipo in TIPO_COMPANY_TYPE:
        valores["company_type"] = TIPO_COMPANY_TYPE[tipo]
    else:
        valores["company_type"] = "company" if cliente.get("RazonSocial") else "person"
    return valores


def valores_creacion(cliente: dict, odoo: OdooUniversalAPI) -> dict:
    """
    Valores para un contacto que se crea por primera vez.

    Incluye el estado fiscal NEUTRO: el modulo venezolano declara
    wh_iva_agent e islr_withholding_agent con default=True, asi que sin fijarlos
    el contacto naceria marcado como agente de retencion sin que nadie lo
    decidiera, y Odoo empezaria a retenerle. Retener de menos se detecta y se
    corrige; retener a quien no corresponde es dinero ajeno enviado al fisco.
    """
    valores = valores_de(cliente)
    valores["comment"] = NOTA_CON_RIF if rif_de(cliente) else NOTA_SIN_RIF
    if _tiene_localizacion_ve(odoo):
        valores.update({
            "wh_iva_agent": False,
            "wh_iva_rate": 0.0,
            "islr_withholding_agent": False,
        })
    return valores


def cambios_para(cliente: dict, actual: dict) -> dict:
    """
    Devuelve SOLO los campos que de verdad cambiaron respecto a Odoo.

    Antes se reescribian todos los campos de todos los clientes en cada pasada.
    Con 3 clientes daba igual; con varios cientos son cientos de escrituras
    inutiles y un historial de Odoo lleno de cambios que no cambian nada, donde
    ya no se distingue una modificacion real.

    Compara solo CAMPOS_ACTUALIZABLES: lo que queda fuera (comment, campos de
    retencion, customer_rank) es de contabilidad y no se toca.
    """
    deseados = valores_de(cliente)
    cambios = {}
    for campo in CAMPOS_ACTUALIZABLES:
        if campo not in deseados:
            continue
        nuevo = deseados[campo]
        viejo = actual.get(campo)
        # Odoo devuelve False para los char vacios; normalizamos para no
        # detectar un cambio falso entre False y "".
        if isinstance(nuevo, str) and viejo is False:
            viejo = ""
        if nuevo != viejo:
            cambios[campo] = nuevo
    return cambios


def buscar_en_odoo(odoo: OdooUniversalAPI, cliente: dict) -> tuple:
    """
    Localiza el contacto en Odoo. Devuelve (registro|None, motivo|None).

    Prioriza el RIF, la llave fiable; si no hay, usa la referencia externa con
    el Id de Smartier. NUNCA empareja por nombre: varia en formato, mayusculas
    y espacios.
    """
    # active_test=False en ambas busquedas: Odoo oculta los archivados. Sin
    # esto, un contacto que pasara a 'Deshabilitado' se archivaria y la pasada
    # siguiente no lo encontraria, creando un DUPLICADO.
    ctx = {"active_test": False}
    campos = ["id", *CAMPOS_ACTUALIZABLES]

    rif = rif_de(cliente)
    if rif:
        hallados = odoo.execute(
            "res.partner", "search_read", [["vat", "=", rif]],
            fields=campos, limit=1, context=ctx,
        )
        if hallados:
            return hallados[0], f"RIF {rif}"

    ref = f"{PREFIJO_REF}{cliente['Id']}"
    hallados = odoo.execute(
        "res.partner", "search_read", [["ref", "=", ref]],
        fields=campos, limit=1, context=ctx,
    )
    if hallados:
        return hallados[0], f"referencia {ref}"
    return None, None


def _registrar(cliente: dict, id_odoo, accion: str, detalle: str,
               error: bool = False) -> None:
    """
    Deja constancia en la base de control para que el panel lo muestre.

    Un contacto sin RIF se marca PENDIENTE, no PROCESADO: esta en Odoo pero
    todavia no se le puede facturar, y esa diferencia debe verse en el panel.
    """
    id_origen = str(cliente["Id"])
    if error:
        estado = EstadoSync.ERROR
    elif rif_de(cliente):
        estado = EstadoSync.PROCESADO
    else:
        estado = EstadoSync.PENDIENTE

    state_store.registrar_mapeo(
        ENTIDAD, id_origen, model_odoo="res.partner",
        id_odoo=id_odoo, estado=estado,
    )
    if estado == EstadoSync.ERROR:
        state_store.marcar_estado(ENTIDAD, id_origen, estado, error=detalle)
    elif estado == EstadoSync.PENDIENTE:
        state_store.marcar_estado(
            ENTIDAD, id_origen, estado,
            error="Sin RIF en Smartier: no se puede facturar todavia.",
        )

    state_store.log(
        ENTIDAD, accion, "ERROR" if error else "OK", id_origen,
        detalle if error else f"{detalle} (id_odoo={id_odoo})",
    )


def sincronizar_cliente(cliente: dict, odoo: OdooUniversalAPI,
                        resultado: ResultadoMaestros) -> None:
    """
    Crea o actualiza UN cliente en Odoo, actualizando el recuento.

    Los errores de datos se capturan y se anotan: un cliente que Odoo rechace
    no debe impedir que se sincronicen los demas.
    """
    etiqueta = f"Smartier #{cliente.get('Id')}"
    try:
        existente, motivo = buscar_en_odoo(odoo, cliente)

        if existente is None:
            id_odoo = odoo.execute(
                "res.partner", "create", valores_creacion(cliente, odoo))
            resultado.creados += 1
            _registrar(cliente, id_odoo, "crear", "Creado en Odoo")
            resultado.detalles.append(f"{etiqueta}: creado (id={id_odoo})")
            return

        cambios = cambios_para(cliente, existente)
        if not cambios:
            # Sin cambios no se escribe, pero SI se refresca el state store:
            # el panel debe reflejar que el cliente sigue sincronizado.
            resultado.sin_cambios += 1
            _registrar(cliente, existente["id"], "sin_cambios",
                       f"Sin cambios ({motivo})")
            return

        odoo.execute("res.partner", "write", [existente["id"]], cambios)
        resultado.actualizados += 1
        _registrar(cliente, existente["id"], "actualizar",
                   f"Actualizado {sorted(cambios)} ({motivo})")
        resultado.detalles.append(
            f"{etiqueta}: actualizado {sorted(cambios)}")

    except OdooExecutionError as e:
        resultado.errores += 1
        _registrar(cliente, None, "sincronizar", str(e), error=True)
        resultado.detalles.append(f"{etiqueta}: ERROR {str(e)[:80]}")
        logger.warning("Cliente %s rechazado por Odoo: %s", etiqueta, e)


def sincronizar_clientes(odoo: OdooUniversalAPI, limite: int = 200,
                         cliente_api: Optional[SmartierClient] = None
                         ) -> ResultadoMaestros:
    """
    Lee los clientes de Smartier y los crea o actualiza en Odoo.

    Un fallo de RED contra Smartier aborta la pasada y se propaga, para que
    Celery la reintente: no se puede distinguir "no hay clientes" de "no pude
    preguntar", y dar la pasada por buena ocultaria el problema.
    """
    propio = cliente_api is None
    cli = cliente_api or SmartierClient()
    resultado = ResultadoMaestros()
    try:
        clientes, total = cli.listar(RUTA_CLIENTES, page_size=min(limite, 200))
    except Exception as e:
        raise MaestrosError(f"No se pudieron leer los clientes: {e}") from e
    finally:
        if propio:
            cli.close()

    resultado.leidos = len(clientes)
    for c in clientes:
        if not rif_de(c):
            resultado.sin_rif += 1
        sincronizar_cliente(c, odoo, resultado)

    logger.info(
        "Maestros Smartier: %s leidos, %s creados, %s actualizados, "
        "%s sin cambios, %s con error.",
        resultado.leidos, resultado.creados, resultado.actualizados,
        resultado.sin_cambios, resultado.errores,
    )
    return resultado
