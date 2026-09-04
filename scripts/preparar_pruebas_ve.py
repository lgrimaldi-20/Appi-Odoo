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
    # Cuentas transitorias de pago. Odoo no asienta un cobro directamente
    # contra el banco: lo deja en una cuenta puente hasta que el extracto
    # bancario lo confirma. Sin ellas, account.payment.create() falla con
    # "No puede crear un nuevo pago sin una cuenta de pagos/recibos pendientes".
    ("1102", "Cobros Pendientes", "asset_current", True),
    ("1103", "Pagos Pendientes", "asset_current", True),
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


def fijar_cuentas_por_defecto(odoo, cuentas: dict) -> None:
    """
    Fija las cuentas por cobrar y por pagar por defecto de la compania.

    En Odoo estas cuentas no viven en el contacto, sino en una propiedad
    (ir.property) a nivel de compania, de la que cada partner hereda. El plan
    contable oficial las crea al instalarse; nuestro plan minimo, no. Sin
    ellas, un contacto queda sin cuenta por cobrar y crear un pago falla con
    "Missing required account on accountable line" -- un mensaje que no
    menciona al contacto y despista bastante.

    Se fija la propiedad global en vez de escribir la cuenta en cada contacto:
    asi la heredan tambien los clientes que se creen despues.
    """
    print("\n3. CUENTAS POR DEFECTO")
    pares = (
        ("property_account_receivable_id", "1201", "por cobrar"),
        ("property_account_payable_id", "2101", "por pagar"),
    )
    for campo, codigo, etiqueta in pares:
        campo_id = odoo.execute(
            "ir.model.fields", "search_read",
            [["model", "=", "res.partner"], ["name", "=", campo]],
            fields=["id"], limit=1,
        )
        if not campo_id:
            print(f"   ! {etiqueta}: no existe el campo {campo}")
            continue
        existe = odoo.execute(
            "ir.property", "search_read",
            [["fields_id", "=", campo_id[0]["id"]], ["res_id", "=", False]],
            fields=["id", "value_reference"], limit=1,
        )
        if existe:
            print(f"   = {etiqueta:10} ya definida ({existe[0]['value_reference']})")
            continue
        if not APLICAR:
            print(f"   + {etiqueta:10} se fijaria a {codigo}")
            continue
        cta = cuentas.get(codigo) or _cuenta(odoo, codigo)
        if not cta:
            print(f"   ! {etiqueta:10} falta la cuenta {codigo}")
            continue
        try:
            odoo.execute("ir.property", "create", {
                "name": campo,
                "fields_id": campo_id[0]["id"],
                "type": "many2one",
                # ir.property guarda el destino como "modelo,id" en texto.
                "value_reference": f"account.account,{cta}",
            })
            print(f"   + {etiqueta:10} FIJADA a {codigo} (id={cta})")
        except OdooExecutionError as e:
            print(f"   ! {etiqueta:10} ERROR: {str(e)[:60]}")


def configurar_metodos_pago(odoo, cuentas: dict) -> None:
    """
    Asigna las cuentas transitorias a los metodos de pago de banco y caja.

    Odoo crea un account.payment.method.line "Manual" por diario y sentido,
    pero deja su payment_account_id vacio. Mientras siga vacio, crear un pago
    falla, porque Odoo no sabe contra que cuenta puente asentarlo. Se rellena
    aqui y no a mano en la interfaz para que el entorno de pruebas se levante
    entero desde este script.
    """
    print("\n4. METODOS DE PAGO")
    cobros = cuentas.get("1102") or _cuenta(odoo, "1102")
    pagos = cuentas.get("1103") or _cuenta(odoo, "1103")
    if not (cobros and pagos):
        print("   ! faltan las cuentas transitorias 1102/1103")
        return

    lineas = odoo.execute(
        "account.payment.method.line", "search_read",
        [["journal_id.type", "in", ["bank", "cash"]]],
        fields=["id", "name", "journal_id", "payment_type", "payment_account_id"],
    )
    for linea in lineas:
        diario = linea["journal_id"][1]
        if linea.get("payment_account_id"):
            print(f"   = {diario[:22]:22} {linea['payment_type']:8} ya configurado")
            continue
        if not APLICAR:
            print(f"   + {diario[:22]:22} {linea['payment_type']:8} se configuraria")
            continue
        cta = cobros if linea["payment_type"] == "inbound" else pagos
        try:
            odoo.execute("account.payment.method.line", "write",
                         [linea["id"]], {"payment_account_id": cta})
            print(f"   + {diario[:22]:22} {linea['payment_type']:8} CONFIGURADO")
        except OdooExecutionError as e:
            print(f"   ! {diario[:22]:22} ERROR: {str(e)[:60]}")


def poner_rif(odoo) -> None:
    """Asigna RIF ficticios a la compania y a los 3 clientes."""
    print("\n5. IDENTIFICACION FISCAL (RIF ficticios)")

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
    print("\n6. NOTA DE ENTREGA SIMULADA")

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
    fijar_cuentas_por_defecto(odoo, cuentas)
    configurar_metodos_pago(odoo, cuentas)
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
