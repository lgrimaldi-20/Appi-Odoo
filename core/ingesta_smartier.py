"""
Ingesta desde Smartier hacia la cola de sincronizacion (modo pull, lado lectura).

Este modulo es la MITAD IZQUIERDA del flujo; el poller es la derecha:

    Smartier API --> [ingesta_smartier] --> cola_sincronizacion --> [poller] --> Odoo

La API de Smartier es de SOLO LECTURA, asi que no se le puede marcar nada como
"ya enviado". El control de que se ha leido vive aqui, en la base de CONTROL del
middleware:

  - Marca de agua (entidad "_ingesta_smartier" del sync_map): guarda la fecha
    del ultimo registro leido, para pedir solo lo nuevo en la siguiente pasada.
  - Antiduplicado: antes de encolar se comprueba que ese id_origen no este ya
    en la cola. Y aunque se colara repetido, sincronizar_entidad consulta el
    sync_map antes de tocar Odoo, asi que NO se duplicaria la factura.

Traduccion Smartier -> formato de mappings.yaml (nota de entrega -> factura):

    Nota.Id                    -> factura_id ("NE-<id>")
    Orden.Cliente.Documento    -> cliente_nif        (RIF; ver AVISO abajo)
    PrecioUnitario.Moneda      -> moneda_iso  (Nacional->VES, Extranjera->USD)
    FechaEntregaReal           -> fecha
    Cantidad / Monto / Descuento -> linea de factura
    Producto.PorcentajeIVA     -> impuesto de la linea (resuelto en Odoo)

AVISO (comprobado contra la API real el 2026-08-21): los clientes de Smartier
tienen Documento.Contenido a null, es decir SIN RIF. Odoo, con la localizacion
venezolana, rechaza la factura sin identificacion fiscal. Por eso una nota sin
RIF se encola igualmente pero el poller la dejara en ERROR con un mensaje claro,
en vez de descartarla en silencio: asi el problema es visible en el panel.
"""

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from core import poller_source, state_store
from core.models_db import EstadoSync
from core.smartier_client import SmartierClient, SmartierError, smartier_habilitado

logger = logging.getLogger("api-odoo")

# Entidad ficticia del sync_map donde se guarda la marca de agua de la ingesta.
# No representa un registro de negocio: es el "por donde iba" del lector.
ENTIDAD_MARCA = "_ingesta_smartier"

# Rutas de la API externa.
RUTA_NOTAS = "/external/notas-entrega"
RUTA_CLIENTES = "/external/clientes"
RUTA_PRODUCTOS = "/external/productos"

# Estados de la nota de entrega en Smartier que deben facturarse en Odoo.
# "Facturada" es el disparador natural: Smartier ya decidio que toca facturar.
ESTADOS_A_FACTURAR = tuple(
    e.strip() for e in os.getenv("SMARTIER_ESTADOS_FACTURAR", "Facturada").split(",")
    if e.strip()
)

# Traduccion del enum de moneda de Smartier al codigo ISO que espera Odoo.
MONEDA_ISO = {"Nacional": "VES", "Extranjera": "USD"}


@dataclass
class ResultadoIngesta:
    """Resumen de una pasada de ingesta."""
    leidas: int        # filas traidas de Smartier
    encoladas: int     # filas nuevas insertadas en la cola
    omitidas: int      # ya estaban en la cola (o no aplicaban)
    marca_agua: Optional[str] = None  # fecha del ultimo registro procesado


class IngestaError(Exception):
    """Fallo al ingerir datos de Smartier."""


# ---------------------------------------------------------------------------
# Marca de agua (hasta donde se leyo la ultima vez)
# ---------------------------------------------------------------------------


def _leer_marca(recurso: str) -> Optional[str]:
    """Devuelve la marca de agua guardada para un recurso, o None."""
    mapa = state_store.buscar_mapeo(ENTIDAD_MARCA, recurso)
    if mapa is None:
        return None
    # Se guarda en hash_payload: es el campo de texto libre que registrar_mapeo
    # persiste tal cual. (El campo 'error' NO sirve: marcar_estado lo borra
    # salvo que el estado sea ERROR.)
    return mapa.hash_payload or None


def _guardar_marca(recurso: str, valor: str) -> None:
    """Persiste la marca de agua de un recurso."""
    # hash_payload es String(64); una fecha ISO cabe de sobra, pero se recorta
    # por seguridad ante formatos inesperados.
    state_store.registrar_mapeo(
        ENTIDAD_MARCA, recurso,
        model_odoo="-", estado=EstadoSync.PROCESADO,
        hash_payload=str(valor)[:64],
    )


# ---------------------------------------------------------------------------
# Traduccion Smartier -> registro del middleware
# ---------------------------------------------------------------------------


def _fecha_iso(valor: Optional[str]) -> Optional[str]:
    """Recorta un datetime ISO de Smartier a fecha (YYYY-MM-DD)."""
    if not valor:
        return None
    return str(valor)[:10]


def _extraer_nif(cliente: dict) -> Optional[str]:
    """
    Saca la identificacion fiscal del cliente de Smartier.

    El DTO trae Documento = {"Tipo": <int>, "Contenido": <str|null>}. Hoy viene
    null en todos los clientes de pruebas; se devuelve None y el poller dejara
    la fila en ERROR con un mensaje explicito.
    """
    doc = cliente.get("Documento") or {}
    contenido = doc.get("Contenido")
    if contenido and str(contenido).strip():
        return str(contenido).strip()
    return None


def nota_a_registro(nota: dict) -> dict:
    """
    Traduce una NotaEntregaExternalDto al registro que espera mappings.yaml
    para la entidad "factura".

    Los campos que Odoo resuelve por su cuenta (impuesto, cuenta contable, tasa
    de cambio) no se envian: se configuran en la ficha del producto/cliente de
    Odoo. Aqui solo va lo que Smartier conoce.
    """
    orden = nota.get("Orden") or {}
    cliente = orden.get("Cliente") or nota.get("Cliente") or {}
    producto = orden.get("Producto") or nota.get("Producto") or {}
    precio = nota.get("PrecioUnitario") or {}

    cantidad = nota.get("Cantidad") or 0
    monto = precio.get("Monto") or 0.0
    descuento = nota.get("Descuento") or 0.0

    linea = {
        "name": producto.get("Nombre") or f"Nota {nota.get('Id')}",
        "quantity": cantidad,
        "price_unit": monto,
    }
    if descuento:
        linea["discount"] = descuento

    registro = {
        "factura_id": f"NE-{nota.get('Id')}",
        "cliente_nif": _extraer_nif(cliente),
        "fecha": _fecha_iso(
            nota.get("FechaEntregaReal") or nota.get("FechaEntrega")
            or nota.get("FechaReferencia")
        ),
        "referencia": _referencia(nota, orden),
        "lineas": [[0, 0, linea]],
        # Metadatos de origen: no los usa mappings.yaml, pero quedan en el
        # payload para trazabilidad y para futuras reglas de mapeo.
        "_smartier": {
            "nota_id": nota.get("Id"),
            "orden_id": orden.get("Id"),
            "cliente_id": cliente.get("Id"),
            "cliente_nombre": cliente.get("RazonSocial") or cliente.get("Nombre"),
            "producto_id": producto.get("Id"),
            "producto_iva": producto.get("PorcentajeIVA"),
            "producto_exento": producto.get("Exento"),
            "estado": nota.get("Estado"),
            "tipo": nota.get("Tipo"),
        },
    }

    iso = MONEDA_ISO.get(precio.get("Moneda"))
    if iso:
        registro["moneda_iso"] = iso

    return registro


def _referencia(nota: dict, orden: dict) -> str:
    """Texto de referencia para la factura en Odoo."""
    partes = []
    if orden.get("Numero"):
        partes.append(f"Orden {orden['Numero']}")
    elif orden.get("Id"):
        partes.append(f"Orden {orden['Id']}")
    partes.append(f"Nota {nota.get('Id')}")
    return " / ".join(partes)


# ---------------------------------------------------------------------------
# Encolado
# ---------------------------------------------------------------------------


def _ya_en_cola(session, entidad: str, id_origen: str) -> bool:
    """True si esa fila ya existe en la cola (en cualquier estado)."""
    return session.query(poller_source.ColaSincronizacion).filter_by(
        entidad=entidad, id_origen=id_origen,
    ).first() is not None


def encolar_registros(entidad: str, registros: list[tuple[str, dict]]) -> tuple[int, int]:
    """
    Inserta registros en la cola de sincronizacion, saltando los repetidos.

    Recibe una lista de (id_origen, payload). Devuelve (encolados, omitidos).
    """
    if not registros:
        return 0, 0

    encolados = 0
    omitidos = 0
    with poller_source.get_source_session() as session:
        for id_origen, payload in registros:
            if _ya_en_cola(session, entidad, id_origen):
                omitidos += 1
                continue
            session.add(poller_source.ColaSincronizacion(
                entidad=entidad,
                id_origen=id_origen,
                payload=payload,
                estado="PENDIENTE",
            ))
            encolados += 1
    return encolados, omitidos


# ---------------------------------------------------------------------------
# Pasada de ingesta
# ---------------------------------------------------------------------------


def ingerir_notas_entrega(
    limite: int = 200,
    desde: Optional[str] = None,
    cliente: Optional[SmartierClient] = None,
) -> ResultadoIngesta:
    """
    Lee notas de entrega de Smartier y encola las que toca facturar.

    - Solo se encolan las notas cuyo Estado este en ESTADOS_A_FACTURAR.
    - 'desde' acota por fecha; si no se indica, se usa la marca de agua guardada.
    - Se ordena por -FechaReferencia para leer primero lo mas reciente.

    Devuelve un ResultadoIngesta. Lanza IngestaError si Smartier falla.
    """
    if not smartier_habilitado():
        logger.debug("Ingesta: Smartier no configurado; nada que hacer.")
        return ResultadoIngesta(leidas=0, encoladas=0, omitidas=0)

    if not poller_source.polling_habilitado():
        raise IngestaError(
            "No hay cola de destino: define SOURCE_DATABASE_URL para poder "
            "encolar lo que se lea de Smartier."
        )

    propio = cliente is None
    cli = cliente or SmartierClient()

    marca = desde or _leer_marca(RUTA_NOTAS)
    filtros = {}
    if marca:
        # El nombre exacto del filtro depende de la API; se envia el habitual y
        # si el endpoint lo ignora, el antiduplicado evita reprocesar de mas.
        filtros["FechaDesde"] = marca

    leidas = 0
    a_encolar: list[tuple[str, dict]] = []
    mas_reciente = marca

    try:
        for nota in cli.paginar(
            # Campos ordenables segun la API: Id, Fecha, Estado (comprobado:
            # cualquier otro devuelve 400 con la lista de los validos).
            RUTA_NOTAS, page_size=min(limite, 200),
            sort="-Fecha,Id", filtros=filtros,
        ):
            leidas += 1

            estado = nota.get("Estado")
            if ESTADOS_A_FACTURAR and estado not in ESTADOS_A_FACTURAR:
                continue

            registro = nota_a_registro(nota)
            a_encolar.append((registro["factura_id"], registro))

            fecha = (nota.get("FechaReferencia") or nota.get("Fecha")
                     or nota.get("FechaEntregaReal"))
            if fecha and (mas_reciente is None or str(fecha) > str(mas_reciente)):
                mas_reciente = str(fecha)

            if len(a_encolar) >= limite:
                break
    except SmartierError as e:
        logger.error("INGESTA_SMARTIER_ERROR | %s (correlation=%s)",
                     e, e.correlation_id)
        raise IngestaError(f"Error leyendo Smartier: {e}") from e
    finally:
        if propio:
            cli.close()

    encoladas, omitidas = encolar_registros("factura", a_encolar)

    if mas_reciente and mas_reciente != marca:
        _guardar_marca(RUTA_NOTAS, mas_reciente)

    if leidas:
        logger.info(
            "INGESTA_SMARTIER | leidas=%s encoladas=%s omitidas=%s marca=%s",
            leidas, encoladas, omitidas, mas_reciente,
        )
    return ResultadoIngesta(
        leidas=leidas, encoladas=encoladas, omitidas=omitidas,
        marca_agua=mas_reciente,
    )


def diagnostico(cliente: Optional[SmartierClient] = None) -> dict:
    """
    Radiografia rapida del estado de Smartier, para el panel y para depurar.

    No encola nada: solo cuenta lo que hay y avisa de los datos que faltarian
    para poder facturar en Odoo (RIF de clientes, sobre todo).
    """
    if not smartier_habilitado():
        return {"habilitado": False}

    propio = cliente is None
    cli = cliente or SmartierClient()
    try:
        info: dict = {"habilitado": True, "recursos": {}}
        for nombre, ruta in (
            ("clientes", RUTA_CLIENTES),
            ("productos", RUTA_PRODUCTOS),
            ("notas_entrega", RUTA_NOTAS),
        ):
            try:
                filas, total = cli.listar(ruta, page=1, page_size=200)
                info["recursos"][nombre] = {
                    "total": total if total is not None else len(filas),
                    "muestra": len(filas),
                }
                if nombre == "clientes":
                    sin_rif = [f for f in filas if not _extraer_nif(f)]
                    info["clientes_sin_rif"] = len(sin_rif)
                if nombre == "productos":
                    alicuotas = sorted({
                        f.get("PorcentajeIVA") for f in filas
                        if f.get("PorcentajeIVA") is not None
                    })
                    info["alicuotas_iva"] = alicuotas
            except SmartierError as e:
                info["recursos"][nombre] = {"error": str(e)}
        return info
    finally:
        if propio:
            cli.close()
