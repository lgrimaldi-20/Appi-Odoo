"""
Dependencias compartidas de seguridad y resolucion de tenant.

Se extraen a un modulo neutral para que los routers de negocio (facturas, pagos)
puedan reutilizarlas sin importar api.py (evita imports circulares).

La API Key se lee del entorno igual que en api.py: si no esta configurada, el
acceso pasa sin validar (modo desarrollo), coherente con el resto del servicio.
"""

import os
from typing import Optional

from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader

from odoo_universal import OdooUniversalAPI, get_tenant

api_key_header = APIKeyHeader(name="X-Api-Key", auto_error=False)


def verify_api_key(x_api_key: Optional[str] = Depends(api_key_header)) -> None:
    """
    Verifica la cabecera X-Api-Key contra API_KEY del entorno.
    Sin API_KEY configurada, no valida (modo desarrollo).
    """
    api_key = os.getenv("API_KEY", "")
    if api_key and x_api_key != api_key:
        raise HTTPException(status_code=401, detail="API Key invalida o ausente.")


def resolver_tenant(tenant: str = "default") -> OdooUniversalAPI:
    """
    Devuelve el conector Odoo del tenant indicado o lanza 400 si no existe.
    """
    try:
        return get_tenant(tenant)
    except KeyError:
        raise HTTPException(
            status_code=400, detail=f"Tenant '{tenant}' no configurado."
        )
