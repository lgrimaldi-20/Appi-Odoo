"""
Registro del tenant por defecto de Odoo, compartido por la API y el worker.

Por que existe: el registro vivia solo dentro de api.py. La API lo ejecutaba al
arrancar y todo funcionaba en desarrollo, donde Celery corre en modo eager
DENTRO del proceso de la API. Pero un worker en su propio contenedor no importa
api.py, asi que nadie registraba el tenant y todas las tareas fallaban con
"Tenant 'default' no registrado.".

Se extrae a core/ -y no se importa api.py desde las tareas- para no arrastrar
FastAPI, los routers y el limitador dentro del worker, que no los necesita.
"""

import logging
import os

from odoo_universal import (
    OdooConnectionError,
    OdooUniversalAPI,
    get_tenant,
    register_tenant,
)

logger = logging.getLogger("api-odoo")

TENANT_POR_DEFECTO = "default"


def registrar_tenant_por_defecto(nombre: str = TENANT_POR_DEFECTO):
    """
    Crea y registra el conector "default" a partir de las variables ODOO_*.

    Devuelve el conector, o None si Odoo no responde. Un fallo de conexion NO
    es fatal a proposito: la API debe poder arrancar para que /health informe
    del estado degradado, y el worker debe poder arrancar para reintentar
    cuando Odoo vuelva, en vez de morir en el arranque.
    """
    try:
        odoo = OdooUniversalAPI(
            url=os.getenv("ODOO_URL", ""),
            db=os.getenv("ODOO_DB", ""),
            username=os.getenv("ODOO_USERNAME", ""),
            password=os.getenv("ODOO_PASSWORD", ""),
        )
    except OdooConnectionError as e:
        logger.error("No se pudo conectar a Odoo al iniciar: %s", e)
        return None

    register_tenant(nombre, odoo)
    logger.info("Conexion con Odoo establecida correctamente (tenant '%s').", nombre)
    return odoo


def asegurar_tenant(nombre: str = TENANT_POR_DEFECTO):
    """
    Devuelve el conector del tenant, registrandolo si aun no lo estaba.

    Lo usan las tareas Celery: si Odoo estaba caido al arrancar el worker, el
    tenant no quedo registrado, y sin este reintento el worker se quedaria
    inutil hasta que alguien lo reiniciara a mano.
    """
    try:
        return get_tenant(nombre)
    except KeyError:
        odoo = registrar_tenant_por_defecto(nombre)
        if odoo is None:
            raise OdooConnectionError(
                f"Odoo no disponible: no se pudo registrar el tenant '{nombre}'."
            )
        return odoo
