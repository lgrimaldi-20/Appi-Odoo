"""
Sincroniza el catalogo de productos de Smartier hacia Odoo (product.product).

Deduplicacion (seccion 7.1 del documento de integracion): el Id de Smartier se
guarda en product.product.default_code como "SMARTIER-<id>". Es la llave de
coincidencia; el nombre NO se usa para emparejar porque puede repetirse o
cambiar de formato.

Impuestos: Smartier SI provee PorcentajeIVA y Exento por producto, asi que el
tratamiento fiscal no hay que completarlo a mano, solo mapearlo al account.tax
equivalente en Odoo. Si la alicuota no existe, el script la crea (por ejemplo el
16%, que Smartier usa y una instancia recien creada no trae).

Tipo de producto (decision "Opcion A" del documento): Odoo NO lleva inventario
paralelo al de Smartier, que sigue siendo la unica fuente de verdad de stock.
Por eso los productos se crean como SERVICIO, sin control de existencias: asi no
se generan movimientos de inventario que luego no podrian sincronizarse de
vuelta, ya que la API de Smartier es de solo lectura.

Uso:
    python scripts/sincronizar_productos_smartier.py            # simulacion
    python scripts/sincronizar_productos_smartier.py --aplicar  # escribe
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
PREFIJO = "SMARTIER-"

# Estados de Smartier con efecto en Odoo. Ver _estado_odoo(): cualquier otro
# valor deja el producto activo y vendible.
ESTADO_DESHABILITADO = "Deshabilitado"
ESTADO_BORRADOR = "Borrador"

# Cache de si la localizacion aporta account.tax.type_tax. None = sin comprobar.
_TYPE_TAX = None

# Cache de si existe product.template.list_price_usd (modulo de dualidad).
_PRECIO_USD = None

# Marcador de precio para satisfacer la validacion del modulo venezolano.
# NO es una tarifa: Smartier no publica precios de catalogo. Ver _valores().
PRECIO_MARCADOR = 0.01

# Entidad del state store: hace visible en el panel la carga del catalogo, que
# de otro modo solo dejaria rastro dentro de Odoo.
ENTIDAD = "producto"


def _tiene_type_tax(odoo) -> bool:
    """
    True si la localizacion venezolana aporta account.tax.type_tax.

    Se comprueba por la existencia del campo y no por el nombre del modulo:
    lo que importa es si se puede escribir. Se cachea porque no cambia durante
    la ejecucion.
    """
    global _TYPE_TAX
    if _TYPE_TAX is None:
        try:
            _TYPE_TAX = bool(odoo.execute(
                "ir.model.fields", "search_count",
                [["model", "=", "account.tax"], ["name", "=", "type_tax"]],
            ))
        except OdooExecutionError:
            _TYPE_TAX = False
    return _TYPE_TAX


def _tiene_precio_usd(odoo) -> bool:
    """
    True si el modulo de dualidad monetaria aporta list_price_usd.

    Se comprueba por la existencia del campo, no por el nombre del modulo: lo
    que importa es si se puede escribir. Sin el, mandarlo romperia el create.
    """
    global _PRECIO_USD
    if _PRECIO_USD is None:
        try:
            _PRECIO_USD = bool(odoo.execute(
                "ir.model.fields", "search_count",
                [["model", "=", "product.template"],
                 ["name", "=", "list_price_usd"]],
            ))
        except OdooExecutionError:
            _PRECIO_USD = False
    return _PRECIO_USD


def _tax_por_alicuota(odoo, porcentaje: float, cache: dict) -> int | None:
    """
    Devuelve el id del account.tax de venta con ese porcentaje, creandolo si no
    existe. Se cachea para no repetir la consulta en cada producto.
    """
    if porcentaje in cache:
        return cache[porcentaje]

    hallados = odoo.execute(
        "account.tax", "search_read",
        [["type_tax_use", "=", "sale"], ["amount", "=", porcentaje],
         ["amount_type", "=", "percent"]],
        fields=["id", "name"], limit=1,
    )
    if hallados:
        cache[porcentaje] = hallados[0]["id"]
        print(f"    impuesto {porcentaje}% -> ya existe (id={hallados[0]['id']})")
        return cache[porcentaje]

    if not APLICAR:
        print(f"    impuesto {porcentaje}% -> se crearia")
        cache[porcentaje] = None
        return None

    valores = {
        "name": f"IVA {porcentaje:g}%",
        "amount": porcentaje,
        "amount_type": "percent",
        "type_tax_use": "sale",
        "description": f"IVA {porcentaje:g}%",
    }
    # La localizacion venezolana declara type_tax con required=True y no
    # existe en Odoo estandar: sin este campo el create falla con
    # "The operation cannot be completed: Field: Tipo de Impuesto".
    # Se envia solo si el campo existe, para no romper una instancia sin
    # localizacion.
    if _tiene_type_tax(odoo):
        valores["type_tax"] = "iva"
    nuevo = odoo.execute("account.tax", "create", valores)
    cache[porcentaje] = nuevo
    print(f"    impuesto {porcentaje}% -> CREADO (id={nuevo})")
    return nuevo


def _estado_odoo(producto: dict) -> tuple[bool, bool]:
    """
    Traduce el Estado de Smartier a (active, sale_ok) de Odoo.

        Disponible     -> activo y vendible
        Borrador       -> activo pero NO vendible (existe, aun no se vende)
        Deshabilitado  -> archivado

    'Deshabilitado' se comprueba de forma explicita y todo lo demas queda
    ACTIVO a proposito: si Smartier anade manana un estado que hoy no
    conocemos, el producto sigue visible en Odoo en vez de archivarse solo. Un
    producto de mas se ve y se corrige; uno archivado por error desaparece de
    las busquedas y del catalogo sin que nadie lo note.
    """
    estado = str(producto.get("Estado") or "").strip()
    if estado == ESTADO_DESHABILITADO:
        return False, False
    if estado == ESTADO_BORRADOR:
        return True, False
    return True, True


def _valores(producto: dict, tax_id: int | None,
             precio_usd: bool = False) -> dict:
    """Traduce un producto de Smartier a los campos de product.product."""
    activo, vendible = _estado_odoo(producto)
    valores = {
        "name": producto.get("Nombre") or f"Producto {producto['Id']}",
        "default_code": f"{PREFIJO}{producto['Id']}",
        # Servicio: Odoo no lleva inventario propio (Opcion A del documento).
        "type": "service",
        # Estado de Smartier -> visibilidad en Odoo. Un producto en borrador
        # existe pero no puede facturarse todavia; uno deshabilitado se archiva
        # (no se borra: conserva su historial en facturas anteriores).
        "active": activo,
        "sale_ok": vendible,
        "purchase_ok": False,
        "description_sale": (
            f"Tipo Smartier: {producto.get('Tipo', '-')}\n"
            "PRECIO PENDIENTE: Smartier no publica tarifa de catalogo; el "
            "precio real viaja en cada nota de entrega. El importe que figura "
            "aqui es solo un marcador."
        ),
    }
    if precio_usd and vendible:
        # La localizacion exige list_price_usd > 0 en todo producto vendible
        # (_check_list_price_usd, smai_homologacion/models/product_template.py).
        # Smartier NO publica precios de catalogo -- su API de productos solo
        # da nombre, tipo e IVA -- asi que se pone un marcador para que el
        # create no falle. Queda avisado en description_sale: no es un precio,
        # y facturar toma el importe de la nota de entrega, no este.
        valores["list_price"] = PRECIO_MARCADOR
        valores["list_price_usd"] = PRECIO_MARCADOR
    # Exento: sin impuestos; si no, la alicuota que indique Smartier.
    if producto.get("Exento"):
        valores["taxes_id"] = [(6, 0, [])]
    elif tax_id:
        valores["taxes_id"] = [(6, 0, [tax_id])]
    return valores


def _registrar(producto: dict, id_odoo, accion: str, detalle: str,
               error: bool = False) -> None:
    """Deja constancia en la base de control para que el panel lo muestre."""
    id_origen = str(producto["Id"])
    estado = EstadoSync.ERROR if error else EstadoSync.PROCESADO

    state_store.registrar_mapeo(
        ENTIDAD, id_origen, model_odoo="product.template",
        id_odoo=id_odoo, estado=estado,
    )
    if error:
        state_store.marcar_estado(ENTIDAD, id_origen, estado, error=detalle)

    state_store.log(
        ENTIDAD, accion, "ERROR" if error else "OK", id_origen,
        f"{producto.get('Nombre', '')[:40]} — {detalle}"
        + ("" if error else f" (id_odoo={id_odoo})"),
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
        productos, total = cli.listar("/external/productos", page_size=200)
    finally:
        cli.close()
    print(f"Smartier: {total} producto(s)")

    if not APLICAR:
        print("\n>>> SIMULACION (usa --aplicar para escribir en Odoo)")

    # 1. Resolver las alicuotas necesarias antes de crear nada.
    print("\nImpuestos necesarios:")
    cache: dict = {}
    for alicuota in sorted({p.get("PorcentajeIVA") for p in productos
                            if p.get("PorcentajeIVA") and not p.get("Exento")}):
        _tax_por_alicuota(odoo, float(alicuota), cache)

    # 2. Crear o actualizar cada producto.
    print("\nProductos:")
    creados = actualizados = errores = 0
    for p in productos:
        codigo = f"{PREFIJO}{p['Id']}"
        tax_id = None if p.get("Exento") else cache.get(float(p.get("PorcentajeIVA") or 0))
        valores = _valores(p, tax_id, precio_usd=_tiene_precio_usd(odoo))

        try:
            existente = odoo.execute(
                "product.template", "search_read", [["default_code", "=", codigo]],
                fields=["id"], limit=1,
                # active_test=False: Odoo excluye los archivados de cualquier
                # busqueda por defecto. Sin esto, un producto que pasara a
                # 'Deshabilitado' se archivaria y en la pasada siguiente no se
                # encontraria, creando un DUPLICADO con el mismo default_code.
                context={"active_test": False},
            )
            if existente:
                if APLICAR:
                    odoo.execute("product.template", "write", [existente[0]["id"]], valores)
                    _registrar(p, existente[0]["id"], "actualizar",
                               f"IVA {p.get('PorcentajeIVA')}%")
                actualizados += 1
                estado = f"ya existe (id={existente[0]['id']})"
            else:
                if APLICAR:
                    nuevo = odoo.execute("product.template", "create", valores)
                    _registrar(p, nuevo, "crear",
                               f"{p.get('Tipo','-')} · IVA {p.get('PorcentajeIVA')}%")
                    estado = f"creado (id={nuevo})"
                else:
                    estado = "se crearia"
                creados += 1
            print(f"  {codigo:14} {p['Nombre'][:34]:34} {estado}")
        except OdooExecutionError as e:
            errores += 1
            if APLICAR:
                _registrar(p, None, "crear", str(e)[:180], error=True)
            print(f"  {codigo:14} {p['Nombre'][:34]:34} ERROR: {str(e)[:60]}")

    print(f"\nResumen: {creados} nuevo(s), {actualizados} existente(s), "
          f"{errores} con error")
    if not APLICAR:
        print("\nNada se ha escrito. Repite con --aplicar para hacerlo efectivo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
