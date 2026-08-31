"""
Limitador de tasa compartido entre api.py y los routers de negocio.

Vive en un modulo neutral (como core/seguridad.py) para que los routers puedan
aplicar limites sin importar api.py, que los importa a ellos.

Por que existe un limite mas estricto en algunos endpoints: /smartier/ingerir y
/poller/ejecutar disparan trabajo pesado contra sistemas externos. Repetirlos
agota la cuota de 5 req/s que Smartier concede por API Key y puede dejar la
integracion bloqueada, asi que se limitan por debajo del limite global.
"""

import os

from slowapi import Limiter
from slowapi.util import get_remote_address

# Limite por defecto para TODAS las rutas. Antes solo /odoo estaba limitado, lo
# que dejaba la API Key expuesta a fuerza bruta por el resto de endpoints.
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[os.getenv("RATE_LIMIT_GLOBAL", "120/minute")],
)


def limitar(regla: str):
    """
    Aplica un limite propio a un endpoint.

    Nota: slowapi exige que la funcion decorada reciba un parametro llamado
    'request' (de tipo Request); si falta, lanza un error en tiempo de peticion.
    """
    return limiter.limit(regla)
