"""
Prepara la instancia para poder facturar: plan contable minimo, diarios, RIF y
una nota de entrega simulada.

Por que existe: el asistente de plan contable de Odoo no es accesible por API
(account.chart.template esta restringido), y sin cuentas ni diarios el modelo
account.move no tiene donde asentar. Este script crea el minimo imprescindible
para ejercitar el flujo completo de facturacion.

Que crea, todo idempotente (se puede repetir sin duplicar):

  1. Plan de cuentas MINIMO -- las 6 que hacen falta para facturar y cobrar.
     No es un plan contable venezolano completo: para eso hay que instalar el
     oficial desde la interfaz. Sirve para PROBAR, no para llevar la
     contabilidad real de la empresa.
  2. Diarios de venta, banco y caja, mas los de retencion de IVA e ISLR que
     exige la localizacion (is_iva_journal / is_islr_journal).
  3. RIF de la compania emisora y de los 3 clientes. Son ficticios pero con
     formato valido (J-########-#), porque Smartier los trae a null y sin
     identificacion fiscal la localizacion rechaza la factura.
  4. Una nota de entrega SIMULADA en la cola del middleware, con la forma
     exacta que devolveria la API de Smartier. Permite ejercitar
     ingesta -> cola -> poller -> factura sin esperar a que Turicopy cargue
     datos reales.

Uso:
    python scripts/preparar_pruebas_ve.py            # simulacion
    python scripts/preparar_pruebas_ve.py --aplicar  # escribe
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

# ---------------------------------------------------------------------------
# 1. Plan de cuentas minimo
# ---------------------------------------------------------------------------
# Solo lo imprescindible para emitir una factura y cobrarla. Un plan
# venezolano real tiene cientos de cuentas; aqui interesa poder probar sin
# ruido. Los codigos siguen la estructura habitual (1=activo, 2=pasivo,
# 4=ingresos, 5=gastos) para que resulten reconocibles.
CUENTAS = (
    # (codigo, nombre, tipo Odoo, conciliable)
    ("1101", "Caja y Bancos", "asset_cash", False),
    ("1201", "Cuentas por Cobrar Clientes", "asset_receivable", True),
    ("2101", "Cuentas por Pagar Proveedores", "liability_payable", True),
    ("2401", "IVA Debito Fiscal", "liability_current", False),
    ("2402", "IVA Retenido por Terceros", "liability_current", False),
    ("4101", "Ingresos por Servicios", "income", False),
    ("5101", "Costo de Servicios", "expense", False),
)

# ---------------------------------------------------------------------------
# 2. Diarios
# ---------------------------------------------------------------------------
# Los dos ultimos son de la localizacion: sin is_iva_journal / is_islr_journal
# marcados, no se pueden seleccionar en la ficha del contacto y las retenciones
# quedan sin diario donde asentarse.
DIARIOS = (
    # (codigo, nombre, tipo, cuenta por defecto, marca de localizacion)
    ("VEN", "Facturas de Cliente", "sale", "4101", None),
    ("BCO", "Banco Principal", "bank", "1101", None),
    ("CAJA", "Caja", "cash", "1101", None),
    ("RIVA", "Retenciones de IVA", "general", "2402", "is_iva_journal"),
    ("RISLR", "Retenciones de ISLR", "general", "2402", "is_islr_journal"),
)

# ---------------------------------------------------------------------------
# 3. Identificacion fiscal
# ---------------------------------------------------------------------------
# RIF FICTICIOS. Smartier trae Documento.Contenido a null en los 3 clientes, y
# sin RIF la localizacion rechaza la factura. Estos permiten probar el circuito;
# hay que sustituirlos por los reales cuando Turicopy los facilite.
RIF_COMPANIA = "J-40123456-7"
RIF_CLIENTES = {
    "SMARTIER-6": ("J-30111111-1", "JUNIOR SUMOZA"),
    "SMARTIER-7": ("J-30222222-2", "Mohammad Siddique"),
    "SMARTIER-8": ("J-30333333-3", "LISBETH SANCHEZ"),
}

# ---------------------------------------------------------------------------
# 4. Nota de entrega simulada
# ---------------------------------------------------------------------------
# Reproduce el NotaEntregaExternalDto tal como lo devuelve la API real,
# comprobado contra /external/notas-entrega. Se encola para que el poller la
# procese igual que una autentica.
NOTA_SIMULADA = {
    "Id": 9001,
    "Estado": "Facturada",
    "Tipo": "Entrega",
    "Cantidad": 10,
    "Descuento": 0,
    "FechaEntregaReal": "2026-09-03T10:00:00",
    "FechaEntrega": "2026-09-03T10:00:00",
    "FechaReferencia": "2026-09-03T10:00:00",
    "PrecioUnitario": {"Monto": 4020.0, "Moneda": "Nacional"},
    "Orden": {
        "Id": 5001,
        "Numero": "ORD-5001",
        "Cliente": {
            "Id": 8,
            "Nombre": "LISBETH SANCHEZ",
            "RazonSocial": None,
            "Documento": {"Tipo": 6, "Contenido": RIF_CLIENTES["SMARTIER-8"][0]},
        },
        "Producto": {"Id": 283, "Nombre": "Hojas", "PorcentajeIVA": 16,
                     "Exento": False},
    },
}


def _cuenta(odoo, codigo: str):
    """Busca una cuenta por codigo. Devuelve el id o None."""
    r = odoo.execute("account.account", "search_read", [["code", "=", codigo]],
                     fields=["id"], limit=1)
    return r[0]["id"] if r else None


def crear_cuentas(odoo) -> dict:
    """Crea el plan minimo. Devuelve {codigo: id_odoo}."""
    print("\n1. PLAN DE CUENTAS")
    ids = {}
    for codigo, nombre, tipo, conciliable in CUENTAS:
        existe = _cuenta(odoo, codigo)
        if existe:
            ids[codigo] = existe
            print(f"   = {codigo} {nombre[:34]:34} ya existe (id={existe})")
            continue
        if not APLICAR:
            print(f"   + {codigo} {nombre[:34]:34} se crearia")
            continue
        try:
            nuevo = odoo.execute("account.account", "create", {
                "code": codigo, "name": nombre,
                "account_type": tipo,
                # Conciliable en las cuentas por cobrar y pagar: sin esto no se
                # pueden cruzar facturas con pagos.
                "reconcile": conciliable,
            })
            ids[codigo] = nuevo
            print(f"   + {codigo} {nombre[:34]:34} CREADA (id={nuevo})")
        except OdooExecutionError as e:
            print(f"   ! {codigo} ERROR: {str(e)[:70]}")
    return ids


def crear_diarios(odoo, cuentas: dict) -> None:
    """Crea los diarios, incluidos los de retencion de la localizacion."""
    print("\n2. DIARIOS")
    for codigo, nombre, tipo, cta, marca in DIARIOS:
        existe = odoo.execute("account.journal", "search_read",
                              [["code", "=", codigo]], fields=["id"], limit=1)
        if existe:
            print(f"   = {codigo:6} {nombre[:32]:32} ya existe (id={existe[0]['id']})")
            continue
        if not APLICAR:
            print(f"   + {codigo:6} {nombre[:32]:32} se crearia")
            continue

        valores = {"name": nombre, "code": codigo, "type": tipo}
        cta_id = cuentas.get(cta) or _cuenta(odoo, cta)
        if cta_id:
            valores["default_account_id"] = cta_id
        # Marcas de la localizacion: sin ellas el diario no aparece como
        # seleccionable para retenciones en la ficha del contacto.
        if marca:
            valores[marca] = True
        try:
            nuevo = odoo.execute("account.journal", "create", valores)
            extra = f" [{marca}]" if marca else ""
            print(f"   + {codigo:6} {nombre[:32]:32} CREADO (id={nuevo}){extra}")
        except OdooExecutionError as e:
            print(f"   ! {codigo:6} ERROR: {str(e)[:70]}")


def poner_rif(odoo) -> None:
    """Asigna RIF ficticios a la compania y a los 3 clientes."""
    print("\n3. IDENTIFICACION FISCAL (RIF ficticios)")

    comp = odoo.execute("res.company", "search_read", [],
                        fields=["id", "name", "vat"])[0]
    if comp.get("vat"):
        print(f"   = compania ya tiene RIF: {comp['vat']}")
    elif APLICAR:
        # vat y rif a la vez: el espejo del modulo es un @api.onchange y NO se
        # dispara por RPC, asi que hay que escribir los dos campos.
        odoo.execute("res.partner", "write", [comp["id"]], {
            "vat": RIF_COMPANIA, "rif": RIF_COMPANIA,
        })
        print(f"   + compania -> {RIF_COMPANIA}")
    else:
        print(f"   + compania -> {RIF_COMPANIA} (se pondria)")

    for ref, (rif, nombre) in sorted(RIF_CLIENTES.items()):
        p = odoo.execute("res.partner", "search_read", [["ref", "=", ref]],
                         fields=["id", "name", "vat"], limit=1,
                         context={"active_test": False})
        if not p:
            print(f"   ! {ref} no encontrado en Odoo")
            continue
        if p[0].get("vat"):
            print(f"   = {ref} ya tiene RIF: {p[0]['vat']}")
            continue
        if not APLICAR:
            print(f"   + {ref} {nombre[:24]:24} -> {rif} (se pondria)")
            continue
        try:
            odoo.execute("res.partner", "write", [p[0]["id"]], {
                "vat": rif, "rif": rif,
                # Persona juridica domiciliada: es lo que corresponde a un RIF
                # que empieza por J.
                "company_type": "company",
                "people_type_company": "pjdo",
                "nationality": "V",
            })
            print(f"   + {ref} {nombre[:24]:24} -> {rif}")
        except OdooExecutionError as e:
            print(f"   ! {ref} ERROR: {str(e)[:70]}")


def encolar_nota(odoo) -> None:
    """Encola una nota de entrega simulada para que la procese el poller."""
    print("\n4. NOTA DE ENTREGA SIMULADA")

    from core import poller_source
    from core.ingesta_smartier import encolar_registros, nota_a_registro

    if not poller_source.polling_habilitado():
        print("   ! SOURCE_DATABASE_URL no configurada: no hay cola.")
        return

    registro = nota_a_registro(NOTA_SIMULADA)
    total = NOTA_SIMULADA["Cantidad"] * NOTA_SIMULADA["PrecioUnitario"]["Monto"]
    print(f"   factura_id : {registro['factura_id']}")
    print(f"   cliente_nif: {registro['cliente_nif']}")
    print(f"   moneda     : {registro.get('moneda_iso', '(compania)')}")
    print(f"   importe    : {NOTA_SIMULADA['Cantidad']} x "
          f"{NOTA_SIMULADA['PrecioUnitario']['Monto']:,.2f} = {total:,.2f} Bs")
    print(f"   con IVA 16%: {total * 1.16:,.2f} Bs")

    if not APLICAR:
        print("   (se encolaria)")
        return

    poller_source.init_source_db()
    encoladas, omitidas = encolar_registros(
        "factura", [(registro["factura_id"], registro)]
    )
    if encoladas:
        print(f"   + ENCOLADA ({encoladas})")
    else:
        print(f"   = ya estaba en la cola ({omitidas} omitida)")


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

    if not APLICAR:
        print("\n>>> SIMULACION (usa --aplicar para escribir en Odoo)")

    cuentas = crear_cuentas(odoo)
    crear_diarios(odoo, cuentas)
    poner_rif(odoo)
    encolar_nota(odoo)

    print("\n" + "-" * 64)
    if not APLICAR:
        print("Nada se ha escrito. Repite con --aplicar para hacerlo efectivo.")
    else:
        print("Listo. Para procesar la nota encolada:")
        print("    POST /poller/ejecutar   (o el boton 'Poller ahora' del panel)")
    print("\nAVISO: el plan de cuentas es MINIMO y los RIF son FICTICIOS.")
    print("Sirven para probar el flujo, no para la contabilidad real. Antes de")
    print("produccion hay que instalar el plan venezolano oficial desde la")
    print("interfaz y cargar los RIF que facilite Turicopy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
