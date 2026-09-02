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

# Cache de si l10n_ve_full esta instalado. None = todavia sin comprobar.
_LOCALIZACION_VE = None

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


def _tiene_localizacion_ve(odoo) -> bool:
    """
    True si l10n_ve_full esta instalado (aporta los campos de retencion).

    Se comprueba por la existencia del campo y no por el nombre del modulo:
    lo que importa es si el campo se puede escribir, no como se llame el addon
    que lo trae. El resultado se cachea porque no cambia durante la ejecucion.
    """
    global _LOCALIZACION_VE
    if _LOCALIZACION_VE is None:
        try:
            _LOCALIZACION_VE = bool(odoo.execute(
                "ir.model.fields", "search_count",
                [["model", "=", "res.partner"], ["name", "=", "wh_iva_agent"]],
            ))
        except OdooExecutionError:
            # Ante la duda se asume que NO esta: enviar campos inexistentes
            # haria fallar el create entero.
            _LOCALIZACION_VE = False
    return _LOCALIZACION_VE


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


def _campos_fiscales_iniciales(odoo) -> dict:
    """
    Estado fiscal NEUTRO para un contacto que se crea por primera vez.

    Solo aplica si la localizacion venezolana (l10n_ve_full) esta instalada; si
    no, estos campos no existen y se devuelve un dict vacio.

    Por que hace falta: el modulo declara wh_iva_agent e islr_withholding_agent
    con default=True. Un contacto creado por este script quedaria marcado como
    agente de retencion de IVA e ISLR sin que nadie lo haya decidido, y a partir
    de ahi Odoo le retendria. Se prefiere lo contrario: nace sin retencion y es
    contabilidad quien la activa a la vista del RIF y de la designacion del
    SENIAT. Retener de menos se detecta y se corrige; retener a quien no
    corresponde es dinero ajeno enviado al fisco.

    Ojo: esto es SOLO para la creacion. En una actualizacion no se tocan (ver
    _valores_actualizacion): pisarian lo que contabilidad haya configurado.
    """
    if not _tiene_localizacion_ve(odoo):
        return {}
    return {
        "wh_iva_agent": False,          # ¿Es agente de retencion de IVA?
        "wh_iva_rate": 0.0,             # % de retencion (el modulo NO lo fija;
                                        # su 'default' tiene un typo: 'dafault')
        "islr_withholding_agent": False,  # ¿Agente de retencion de ISLR?
    }


def _valores_actualizacion(cliente: dict) -> dict:
    """
    Campos a escribir sobre un contacto que YA existe en Odoo.

    Se excluyen los campos fiscales a proposito: son competencia de
    contabilidad (RIF, condicion de agente de retencion, porcentajes, tipo de
    contribuyente) y una pasada de sincronizacion no debe revertir lo que
    alguien configuro a mano. Smartier tampoco los conoce: su API expone 8
    campos y ninguno es fiscal.

    'vat' es la excepcion: viene de Smartier (Documento.Contenido) y solo se
    envia cuando trae valor, nunca vacio, para no borrar un RIF ya cargado.
    """
    return _valores(cliente)


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
    if _tiene_localizacion_ve(odoo):
        print("Localizacion venezolana detectada: los contactos NUEVOS se crean")
        print("  sin retencion (wh_iva_agent=False, islr_withholding_agent=False).")
        print("  Contabilidad activa la retencion que corresponda a cada uno.")
    else:
        print("Sin localizacion venezolana (l10n_ve_full): no hay campos de")
        print("  retencion que fijar.")
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
                    # Actualizacion: SIN campos fiscales. Contabilidad es quien
                    # decide la condicion de agente de retencion y sus
                    # porcentajes; una pasada del sincronizador no debe
                    # revertirlo.
                    odoo.execute("res.partner", "write", [existente],
                                 _valores_actualizacion(c))
                    _registrar(c, existente, "actualizar",
                               f"vinculado por {motivo}", rif)
                print(f"  = {etiqueta} ya existe (por {motivo}) -> id={existente}")
                actualizados += 1
            else:
                if APLICAR:
                    # Creacion: se fija un estado fiscal NEUTRO explicito. Sin
                    # esto, los default=True del modulo venezolano marcarian al
                    # contacto como agente de retencion de IVA e ISLR nada mas
                    # nacer, sin que nadie lo haya decidido.
                    nuevo = odoo.execute(
                        "res.partner", "create",
                        {**valores, **_campos_fiscales_iniciales(odoo)},
                    )
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
