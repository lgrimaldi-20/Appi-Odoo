"""
Sincroniza los clientes de Smartier hacia Odoo (res.partner).

Deduplicacion (seccion 6.2 del documento de integracion):

  1. Si el cliente trae RIF (Documento.Contenido), se busca en Odoo por ese
     valor: es la llave de coincidencia fiable. Un contacto que ya exista -por
     ejemplo creado como proveedor- se REUTILIZA anadiendole el rol de cliente,
     nunca se duplica.
  2. Si no trae RIF -hoy es el caso de los 3 clientes-, se cae en el Id de
     Smartier, guardado en res.partner.ref como "SMARTIER-<id>". Es la unica
     llave estable que queda: el nombre varia en formato, mayusculas y espacios,
     asi que NO se usa para emparejar.

Los clientes sin RIF se crean marcados en el campo 'comment' como pendientes de
validacion fiscal: Odoo con localizacion venezolana rechazara su factura hasta
que contabilidad complete el dato.

Uso:
    python scripts/sincronizar_clientes_smartier.py            # simulacion
    python scripts/sincronizar_clientes_smartier.py --aplicar  # escribe en Odoo
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from core import state_store  # noqa: E402
from core.models_db import EstadoSync  # noqa: E402
from core.smartier_client import SmartierClient  # noqa: E402
from odoo_universal import (  # noqa: E402
    OdooConnectionError,
    OdooExecutionError,
    OdooUniversalAPI,
)

APLICAR = "--aplicar" in sys.argv

# Prefijo de la referencia externa en Odoo. Permite localizar despues que
# contactos vinieron de Smartier y con que Id.
PREFIJO_REF = "SMARTIER-"

# Entidad del state store: con esto la sincronizacion de contactos aparece en
# el panel igual que las facturas o los pagos, en vez de ser una operacion
# invisible que solo deja rastro en Odoo.
ENTIDAD = "cliente"

# Estado de Smartier que archiva el contacto en Odoo. Ver _activo(): se
# comprueba este valor concreto, no la ausencia de 'Habilitado'.
ESTADO_DESHABILITADO = "Deshabilitado"

# Tipo de Smartier -> company_type de Odoo.
TIPO_COMPANY_TYPE = {"Contacto": "person", "Empresa": "company"}

NOTA_SIN_RIF = (
    "[Sincronizado desde Smartier] PENDIENTE DE VALIDACION FISCAL: "
    "este contacto no tiene RIF en Smartier. Odoo rechazara su factura "
    "hasta que contabilidad complete la identificacion fiscal."
)
NOTA_CON_RIF = "[Sincronizado desde Smartier]"


def _rif(cliente: dict):
    """Identificacion fiscal del cliente, o None si viene vacia."""
    contenido = (cliente.get("Documento") or {}).get("Contenido")
    return str(contenido).strip() if contenido and str(contenido).strip() else None


def _buscar_en_odoo(odoo, cliente: dict):
    """
    Localiza el contacto en Odoo. Devuelve (id, motivo) o (None, None).

    Prioriza el RIF; si no hay, usa la referencia externa con el Id de Smartier.
    Nunca empareja por nombre: varia demasiado para ser fiable.
    """
    # active_test=False en ambas busquedas: Odoo oculta los archivados por
    # defecto. Sin esto, un contacto que pasara a 'Deshabilitado' se archivaria
    # y la pasada siguiente no lo encontraria, creando un DUPLICADO.
    sin_filtro_activo = {"active_test": False}

    rif = _rif(cliente)
    if rif:
        hallados = odoo.execute(
            "res.partner", "search_read", [["vat", "=", rif]],
            fields=["id", "name"], limit=1, context=sin_filtro_activo,
        )
        if hallados:
            return hallados[0]["id"], f"RIF {rif}"

    ref = f"{PREFIJO_REF}{cliente['Id']}"
    hallados = odoo.execute(
        "res.partner", "search_read", [["ref", "=", ref]],
        fields=["id", "name"], limit=1, context=sin_filtro_activo,
    )
    if hallados:
        return hallados[0]["id"], f"referencia {ref}"
    return None, None


def _activo(cliente: dict) -> bool:
    """
    Traduce el Estado de Smartier al archivado de Odoo (res.partner.active).

    Solo 'Deshabilitado' archiva. Se comprueba el valor NEGATIVO en vez de dar
    por bueno 'Habilitado' a proposito: si Smartier anade manana un estado
    nuevo que hoy no conocemos, el contacto queda activo y visible en Odoo, no
    archivado en silencio. Un falso activo se ve y se corrige; un archivado por
    error desaparece de las busquedas y nadie lo nota.
    """
    return str(cliente.get("Estado") or "").strip() != ESTADO_DESHABILITADO


def _valores(cliente: dict) -> dict:
    """Traduce un cliente de Smartier a los campos de res.partner."""
    rif = _rif(cliente)
    valores = {
        "name": cliente.get("RazonSocial") or cliente.get("Nombre") or "Sin nombre",
        "ref": f"{PREFIJO_REF}{cliente['Id']}",
        "customer_rank": 1,          # marca el rol de CLIENTE
        "comment": NOTA_CON_RIF if rif else NOTA_SIN_RIF,
        # Estado de Smartier -> archivado de Odoo. Un contacto dado de baja en
        # el origen deja de aparecer en las busquedas de Odoo, pero se conserva
        # con su historial: 'active' archiva, no borra.
        "active": _activo(cliente),
    }
    if cliente.get("Email"):
        valores["email"] = cliente["Email"]
    if rif:
        valores["vat"] = rif
    # company_type: Smartier ya distingue Contacto/Empresa, asi que se usa ese
    # dato en vez de deducirlo de si hay RazonSocial (un contacto puede tener
    # razon social sin ser empresa). Se cae en RazonSocial solo si el Tipo
    # viniera vacio.
    tipo = str(cliente.get("Tipo") or "").strip()
    if tipo in TIPO_COMPANY_TYPE:
        valores["company_type"] = TIPO_COMPANY_TYPE[tipo]
    else:
        valores["company_type"] = "company" if cliente.get("RazonSocial") else "person"
    return valores


def _registrar(cliente: dict, id_odoo, accion: str, detalle: str,
               rif, error: bool = False) -> None:
    """
    Deja constancia en la base de control para que el panel lo muestre.

    Un contacto sin RIF se marca PENDIENTE, no PROCESADO: esta en Odoo pero
    todavia no se le puede facturar, y esa diferencia debe verse en el panel.
    """
    id_origen = str(cliente["Id"])
    if error:
        estado = EstadoSync.ERROR
    elif rif:
        estado = EstadoSync.PROCESADO
    else:
        estado = EstadoSync.PENDIENTE

    state_store.registrar_mapeo(
        ENTIDAD, id_origen, model_odoo="res.partner",
        id_odoo=id_odoo, estado=estado,
    )
    if estado == EstadoSync.ERROR:
        state_store.marcar_estado(ENTIDAD, id_origen, estado, error=detalle)
    elif not rif:
        state_store.marcar_estado(
            ENTIDAD, id_origen, estado,
            error="Sin RIF en Smartier: no se puede facturar todavia.",
        )

    state_store.log(
        ENTIDAD, accion, "ERROR" if error else "OK", id_origen,
        detalle if error else f"{detalle} (id_odoo={id_odoo})",
    )


def main() -> int:
    try:
        odoo = OdooUniversalAPI(
            os.getenv("ODOO_URL"), os.getenv("ODOO_DB"),
            os.getenv("ODOO_USERNAME"), os.getenv("ODOO_PASSWORD"),
        )
    except OdooConnectionError as e:
        print(f"[X] No se pudo conectar a Odoo: {e}")
        return 1
    print(f"Odoo conectado (uid={odoo.uid})")
    state_store.init_db()

    cli = SmartierClient()
    try:
        clientes, total = cli.listar("/external/clientes", page_size=200)
    finally:
        cli.close()
    print(f"Smartier: {total} cliente(s)\n")

    if not APLICAR:
        print(">>> SIMULACION (usa --aplicar para escribir en Odoo)\n")

    creados = actualizados = sin_rif = 0
    for c in clientes:
        rif = _rif(c)
        valores = _valores(c)
        existente, motivo = _buscar_en_odoo(odoo, c)

        etiqueta = f"Smartier #{c['Id']:<4} {valores['name'][:30]:30}"
        if not rif:
            sin_rif += 1

        try:
            if existente:
                if APLICAR:
                    # Solo se anade el rol de cliente y se refrescan los datos;
                    # nunca se pisa un contacto que ya existia por otra via.
                    odoo.execute("res.partner", "write", [existente], valores)
                    _registrar(c, existente, "actualizar",
                               f"vinculado por {motivo}", rif)
                print(f"  = {etiqueta} ya existe (por {motivo}) -> id={existente}")
                actualizados += 1
            else:
                if APLICAR:
                    nuevo = odoo.execute("res.partner", "create", valores)
                    _registrar(c, nuevo, "crear", valores["name"], rif)
                    print(f"  + {etiqueta} creado -> id={nuevo}")
                else:
                    print(f"  + {etiqueta} se crearia")
                creados += 1
        except OdooExecutionError as e:
            if APLICAR:
                _registrar(c, None, "crear", str(e)[:180], rif, error=True)
            print(f"  ! {etiqueta} ERROR: {str(e)[:90]}")

        if not rif:
            print(f"      sin RIF: queda marcado como pendiente de validacion fiscal")

    print(f"\nResumen: {creados} nuevo(s), {actualizados} existente(s), "
          f"{sin_rif} sin RIF")
    if sin_rif:
        print("\n[!] Los contactos sin RIF NO podran facturarse hasta que se")
        print("    cargue su identificacion fiscal (en Smartier, preferible, o")
        print("    directamente en Odoo).")
    if not APLICAR:
        print("\nNada se ha escrito. Repite con --aplicar para hacerlo efectivo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
