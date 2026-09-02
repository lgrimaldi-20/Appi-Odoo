"""
Prepara Odoo para facturar en Venezuela SIN la localizacion (l10n_ve_full).

Por que existe: el submodulo de la localizacion no consigue instalarse en la
instancia (el build de Odoo.sh muere sin dejar logs legibles). Este script
cubre con campos NATIVOS de Odoo 17 lo que hace falta para facturar mientras
tanto, de modo que el proyecto no quede bloqueado esperando ese despliegue.

Que hace, todo idempotente:

  1. Activa la moneda VES (Odoo la trae desactivada).
  2. Crea el grupo de impuestos "Retenciones".
  3. Crea las retenciones de IVA (75% y 100%) y de ISLR (1%, 2%, 3%, 5%) como
     account.tax NEGATIVOS. Un impuesto negativo resta del total, que es
     exactamente lo que hace una retencion.
  4. Crea posiciones fiscales para clasificar al cliente segun su regimen:
     Contribuyente Ordinario / Especial 75% / Especial 100%.

Que NO hace, y hay que saberlo:

  - No genera el comprobante de retencion (el PDF numerado que exige el
    SENIAT). Eso solo lo da la localizacion.
  - No lleva los libros de compra/venta ni el archivo TXT del SENIAT.
  - No valida el formato del RIF ni asigna Numero de Control.

Es decir: sirve para OPERAR y para que las cifras cuadren, no para cumplir de
forma completa con la declaracion. Cuando l10n_ve_full entre, sus campos
conviven con esto; las retenciones creadas aqui habria que revisarlas para no
duplicar con las suyas.

Uso:
    python scripts/preparar_fiscal_ve.py            # simulacion
    python scripts/preparar_fiscal_ve.py --aplicar  # escribe en Odoo
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from odoo_universal import (  # noqa: E402
    OdooConnectionError,
    OdooExecutionError,
    OdooUniversalAPI,
)

APLICAR = "--aplicar" in sys.argv

# Retenciones de IVA. El agente retiene el 75% del IVA facturado (o el 100% si
# la factura no cumple los requisitos formales o el proveedor no esta al dia).
#
# OJO con el porcentaje: la retencion es un % DEL IVA, no de la base. Un
# account.tax normal se aplica siempre sobre la base imponible, asi que hay que
# convertirlo:
#
#     retener el 75% del IVA (16%)  ==  0,75 x 16%  =  12% de la base
#     retener el 100% del IVA (16%) ==  1,00 x 16%  =  16% de la base
#
# Sin esa conversion, Odoo restaria el 75% de la BASE (750 Bs sobre 1.000) en
# vez del 75% del IVA (120 Bs). Comprobado contra la instancia: el total salia
# 410 Bs en lugar de 1.040.
#
# Contrapartida: el porcentaje queda atado a la alicuota del 16%. Si el IVA
# cambia, estas retenciones hay que recalcularlas -- de ahi que el nombre lo
# diga y que exista la constante de abajo.
IVA_VIGENTE = 16.0

RETENCIONES_IVA = (
    ("Retencion IVA 75% (sobre IVA 16%)", -(0.75 * IVA_VIGENTE), "RET_IVA_75"),
    ("Retencion IVA 100% (sobre IVA 16%)", -(1.00 * IVA_VIGENTE), "RET_IVA_100"),
)

# Retenciones de ISLR por concepto. Los porcentajes dependen del tipo de
# servicio y de si el proveedor es persona natural o juridica; se crean los
# tramos habituales y contabilidad elige el que corresponda.
RETENCIONES_ISLR = (
    ("Retencion ISLR 1%", -1.0, "RET_ISLR_1"),
    ("Retencion ISLR 2%", -2.0, "RET_ISLR_2"),
    ("Retencion ISLR 3%", -3.0, "RET_ISLR_3"),
    ("Retencion ISLR 5%", -5.0, "RET_ISLR_5"),
)

# Posiciones fiscales: agrupan al cliente por su regimen ante el SENIAT. Es el
# equivalente nativo mas cercano al campo wh_iva_agent de la localizacion.
POSICIONES = (
    ("VE - Contribuyente Ordinario",
     "Cliente ordinario: no retiene IVA. Es el caso por defecto."),
    ("VE - Contribuyente Especial (retiene 75%)",
     "Designado agente de retencion por el SENIAT: retiene el 75% del IVA."),
    ("VE - Contribuyente Especial (retiene 100%)",
     "Retiene el 100% del IVA (factura sin requisitos formales o proveedor no "
     "al dia en el RIF)."),
)


def _activar_ves(odoo) -> None:
    """Activa la moneda VES; Odoo la trae desactivada de fabrica."""
    ves = odoo.execute(
        "res.currency", "search_read", [["name", "=", "VES"]],
        fields=["id", "name", "active"], limit=1,
        # active_test=False: una moneda desactivada no aparece en una busqueda
        # normal, y sin esto el script creeria que no existe.
        context={"active_test": False},
    )
    if not ves:
        print("  [!] VES no existe en esta instancia (inesperado).")
        return
    if ves[0]["active"]:
        print(f"  = VES ya esta activa (id={ves[0]['id']})")
        return
    if APLICAR:
        odoo.execute("res.currency", "write", [ves[0]["id"]], {"active": True})
        print(f"  + VES ACTIVADA (id={ves[0]['id']})")
    else:
        print(f"  + VES se activaria (id={ves[0]['id']})")


def _grupo_retenciones(odoo):
    """Devuelve el id del grupo de impuestos 'Retenciones', creandolo si falta."""
    hallado = odoo.execute(
        "account.tax.group", "search_read", [["name", "=", "Retenciones"]],
        fields=["id"], limit=1,
    )
    if hallado:
        print(f"  = grupo 'Retenciones' ya existe (id={hallado[0]['id']})")
        return hallado[0]["id"]
    if not APLICAR:
        print("  + grupo 'Retenciones' se crearia")
        return None
    nuevo = odoo.execute("account.tax.group", "create", {"name": "Retenciones"})
    print(f"  + grupo 'Retenciones' CREADO (id={nuevo})")
    return nuevo


def _crear_retencion(odoo, nombre: str, porcentaje: float, codigo: str,
                     grupo_id) -> None:
    """
    Crea una retencion como account.tax con importe NEGATIVO.

    Un impuesto negativo resta del total del documento, que es justo lo que
    hace una retencion. Se marca type_tax_use='sale' porque se aplica sobre
    facturas de cliente.
    """
    hallado = odoo.execute(
        "account.tax", "search_read",
        [["name", "=", nombre], ["type_tax_use", "=", "sale"]],
        fields=["id"], limit=1, context={"active_test": False},
    )
    if hallado:
        print(f"  = {nombre:28} ya existe (id={hallado[0]['id']})")
        return
    if not APLICAR:
        print(f"  + {nombre:28} se crearia ({porcentaje:g}%)")
        return

    valores = {
        "name": nombre,
        "amount": porcentaje,
        "amount_type": "percent",
        "type_tax_use": "sale",
        "description": codigo,
        # No suma a la base de los impuestos siguientes: la retencion se
        # calcula sobre el IVA ya determinado, no lo modifica.
        "include_base_amount": False,
    }
    if grupo_id:
        valores["tax_group_id"] = grupo_id
    try:
        nuevo = odoo.execute("account.tax", "create", valores)
        print(f"  + {nombre:28} CREADA (id={nuevo}, {porcentaje:g}%)")
    except OdooExecutionError as e:
        print(f"  ! {nombre:28} ERROR: {str(e)[:70]}")


def _crear_posicion(odoo, nombre: str, nota: str) -> None:
    """Crea una posicion fiscal para clasificar al cliente por su regimen."""
    hallado = odoo.execute(
        "account.fiscal.position", "search_read", [["name", "=", nombre]],
        fields=["id"], limit=1,
    )
    if hallado:
        print(f"  = {nombre:44} ya existe (id={hallado[0]['id']})")
        return
    if not APLICAR:
        print(f"  + {nombre:44} se crearia")
        return
    nuevo = odoo.execute("account.fiscal.position", "create", {
        "name": nombre,
        "note": nota,
        "auto_apply": False,   # se asigna a mano: depende de la designacion
    })
    print(f"  + {nombre:44} CREADA (id={nuevo})")


def main() -> int:
    try:
        odoo = OdooUniversalAPI(
            os.getenv("ODOO_URL"), os.getenv("ODOO_DB"),
            os.getenv("ODOO_USERNAME"), os.getenv("ODOO_PASSWORD"),
        )
    except OdooConnectionError as e:
        print(f"[X] No se pudo conectar a Odoo: {e}")
        return 1
    print(f"Odoo conectado (uid={odoo.uid})\n")

    # Si la localizacion llega a instalarse, este script sobra: avisa en vez de
    # duplicar impuestos con los suyos.
    tiene_loc = odoo.execute(
        "ir.model.fields", "search_count",
        [["model", "=", "res.partner"], ["name", "=", "wh_iva_agent"]],
    )
    if tiene_loc:
        print("[!] l10n_ve_full YA esta instalado en esta instancia.")
        print("    Este script es el sustituto provisional: usa los campos de")
        print("    la localizacion en su lugar y revisa que no haya impuestos")
        print("    duplicados.\n")

    if not APLICAR:
        print(">>> SIMULACION (usa --aplicar para escribir en Odoo)\n")

    print("Moneda:")
    _activar_ves(odoo)

    print("\nGrupo de impuestos:")
    grupo = _grupo_retenciones(odoo)

    print("\nRetenciones de IVA:")
    for nombre, pct, codigo in RETENCIONES_IVA:
        _crear_retencion(odoo, nombre, pct, codigo, grupo)

    print("\nRetenciones de ISLR:")
    for nombre, pct, codigo in RETENCIONES_ISLR:
        _crear_retencion(odoo, nombre, pct, codigo, grupo)

    print("\nPosiciones fiscales:")
    for nombre, nota in POSICIONES:
        _crear_posicion(odoo, nombre, nota)

    print("\n" + "-" * 62)
    if not APLICAR:
        print("Nada se ha escrito. Repite con --aplicar para hacerlo efectivo.")
    else:
        print("Listo. Contabilidad debe ahora, por cada cliente:")
        print("  1. Cargar el RIF en el campo 'vat'.")
        print("  2. Asignar la posicion fiscal que le corresponda.")
    print("\nRecuerda que esto NO cubre el comprobante de retencion numerado,")
    print("los libros de compra/venta ni el TXT del SENIAT: eso exige la")
    print("localizacion.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
