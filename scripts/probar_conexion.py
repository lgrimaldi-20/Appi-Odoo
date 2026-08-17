"""
Comprobacion rapida de la conexion con Odoo usando el .env del proyecto.

No arranca la API: construye el conector directamente, para separar un problema
de credenciales/red de un problema del middleware.

Uso:  python scripts/probar_conexion.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

from odoo_universal import (  # noqa: E402
    OdooConnectionError,
    OdooExecutionError,
    OdooUniversalAPI,
)

load_dotenv()

URL = os.getenv("ODOO_URL", "")
DB = os.getenv("ODOO_DB", "")
USER = os.getenv("ODOO_USERNAME", "")
PWD = os.getenv("ODOO_PASSWORD", "")

faltan = [n for n, v in
          (("ODOO_URL", URL), ("ODOO_DB", DB),
           ("ODOO_USERNAME", USER), ("ODOO_PASSWORD", PWD)) if not v]
if faltan:
    print(f"[X] Faltan variables en .env: {', '.join(faltan)}")
    sys.exit(1)

print(f"Conectando a {URL} (db={DB}) como {USER}...")

try:
    odoo = OdooUniversalAPI(URL, DB, USER, PWD)
except OdooConnectionError as e:
    print(f"[X] No se pudo conectar/autenticar: {e}")
    print("    Revisa la URL del build, el nombre de la DB y que la clave sea")
    print("    una API Key de Odoo (Ajustes > Usuarios > Seguridad de la cuenta).")
    sys.exit(1)

print(f"[OK] Autenticado. uid={odoo.uid}")

# La deteccion de version (atributos .version / .version_mayor) solo existe en la
# rama Fibex-V19. En Turicopy-V17 se consulta common.version aparte.
version = getattr(odoo, "version", None)
if version is None:
    import requests
    try:
        r = requests.post(
            f"{URL.rstrip('/')}/jsonrpc",
            json={"jsonrpc": "2.0", "method": "call",
                  "params": {"service": "common", "method": "version", "args": []}},
            timeout=30,
        )
        version = (r.json().get("result") or {}).get("server_serie", "")
    except Exception:
        version = ""

print(f"[OK] Version del servidor: {version or 'desconocida'}")

if version and not str(version).startswith("17"):
    print(f"[!] AVISO: la rama Turicopy-V17 apunta a Odoo 17, pero el servidor "
          f"reporta la {version}. Para la 19 usa la rama Fibex-V19.")

# Lectura inocua para confirmar que el uid tiene permisos reales.
try:
    empresas = odoo.execute("res.company", "search_read", [], fields=["name"], limit=5)
    print(f"[OK] Companias visibles: {[c['name'] for c in empresas]}")
except OdooExecutionError as e:
    print(f"[!] Autentico pero fallo la lectura de res.company: {e}")
