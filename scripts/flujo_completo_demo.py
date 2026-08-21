"""
Flujo de negocio COMPLETO contra el middleware (demo end-to-end).

Simula un ciclo real de venta de equipos, tocando las cinco capas:

    1. INVENTARIO  entra mercancia al almacen (recepcion de proveedor)
    2. FACTURA     se vende al cliente (account.move, posteada)
    3. INVENTARIO  sale la mercancia vendida (decremento)
    4. PAGO        el cliente paga (account.payment)
    5. CONCILIACION se cruza la factura con el pago
    6. ASIENTO     se registra un gasto asociado (comision del vendedor)

Todo por HTTP contra el middleware, como lo haria el sistema del cliente.
Es idempotente: relanzarlo con el mismo PREFIJO no duplica nada en Odoo.

Uso:
    python scripts/flujo_completo_demo.py [PREFIJO]
"""

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.getenv("API_URL_BASE", "http://127.0.0.1:8000")


def _api_key() -> str:
    """API Key del entorno o, en su defecto, parseada del .env."""
    clave = os.getenv("API_KEY", "")
    if clave:
        return clave
    ruta = os.path.join(os.path.dirname(__file__), "..", ".env")
    with open(ruta, encoding="utf-8") as fh:
        for linea in fh:
            if linea.startswith("API_KEY="):
                return linea.split("=", 1)[1].strip()
    return ""


API_KEY = _api_key()


def llamar(ruta: str, cuerpo: dict, metodo: str = "POST") -> tuple[int, dict]:
    """POST/DELETE al middleware. Devuelve (codigo_http, json)."""
    req = urllib.request.Request(
        f"{BASE}{ruta}",
        data=json.dumps(cuerpo).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Api-Key": API_KEY},
        method=metodo,
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def paso(n: str, titulo: str, ruta: str, cuerpo: dict, metodo: str = "POST") -> dict:
    """Ejecuta un paso e imprime el resultado de forma legible."""
    codigo, datos = llamar(ruta, cuerpo, metodo)
    marca = "OK " if codigo == 200 else f"HTTP {codigo}"
    print(f"\n[{n}] {titulo}")
    print(f"     -> {marca} {json.dumps(datos, ensure_ascii=False)}")
    return datos


def main() -> int:
    pref = sys.argv[1] if len(sys.argv) > 1 else "DEMO"
    print("=" * 74)
    print(f"FLUJO COMPLETO  ({pref})   inventario -> factura -> pago -> conciliacion")
    print("=" * 74)

    # --- 1. Entrada de mercancia: 20 routers del proveedor -------------------
    paso("1", "INVENTARIO  entrada de 20 unidades (recepcion)",
         "/stock/ajustar",
         {"registro": {"ajuste_id": f"{pref}-ENT-01", "producto_ref": "HW-ROUTER-01",
                       "cantidad": 20, "modo": "incrementar",
                       "motivo": "Recepcion de proveedor"}})

    # --- 2. Venta: factura de 3 routers a 250 Bs -----------------------------
    # 3 x 250 = 750 Bs. El campo total lo valida el middleware contra Odoo.
    fac = paso("2", "FACTURA     venta de 3 unidades (750 Bs)",
               "/facturas",
               {"registro": {"factura_id": f"{pref}-FAC-01", "cliente_nif": "B12345678",
                             "fecha": "2026-08-21", "referencia": f"{pref} venta de routers",
                             "moneda_iso": "VES", "total": 750.0,
                             "lineas": [[0, 0, {"product_id": 2, "quantity": 3,
                                                "price_unit": 250.0,
                                                "tax_ids": [[6, 0, []]]}]]}})

    # --- 3. Salida de mercancia: los 3 vendidos ------------------------------
    paso("3", "INVENTARIO  salida de 3 unidades (entrega al cliente)",
         "/stock/ajustar",
         {"registro": {"ajuste_id": f"{pref}-SAL-01", "producto_ref": "HW-ROUTER-01",
                       "cantidad": 3, "modo": "decrementar",
                       "motivo": f"Entrega de {pref}-FAC-01"}})

    # --- 4. Cobro ------------------------------------------------------------
    pag = paso("4", "PAGO        cobro de 750 Bs",
               "/pagos",
               {"registro": {"pago_id": f"{pref}-PAG-01", "cliente_nif": "B12345678",
                             "diario_codigo": "BNK1", "monto": 750.0,
                             "fecha": "2026-08-21", "moneda_iso": "VES"}})

    # --- 5. Conciliacion factura <-> pago ------------------------------------
    if fac.get("id_odoo") and pag.get("id_odoo"):
        paso("5", "CONCILIAR   cruce factura <-> pago",
             "/conciliar",
             {"factura_id_odoo": fac["id_odoo"], "pago_id_odoo": pag["id_odoo"],
              "factura_id_origen": f"{pref}-FAC-01", "pago_id_origen": f"{pref}-PAG-01"})
    else:
        print("\n[5] CONCILIAR   omitido: falta el id de la factura o del pago")

    # --- 6. Asiento de gasto: comision del vendedor (10% de 750) -------------
    paso("6", "ASIENTO     comision del vendedor (75 Bs)",
         "/asientos",
         {"registro": {"asiento_id": f"{pref}-ASI-01", "diario_codigo": "MISC",
                       "fecha": "2026-08-21", "referencia": f"Comision {pref}-FAC-01",
                       "lineas": [
                           {"cuenta_codigo": "600000", "debe": 75.0, "haber": 0,
                            "concepto": "Comision vendedor"},
                           {"cuenta_codigo": "101401", "debe": 0, "haber": 75.0,
                            "concepto": "Pago comision"}]}})

    # --- Comprobacion final --------------------------------------------------
    codigo, stock = llamar("/stock/consultar",
                           {"registro": {"producto_ref": "HW-ROUTER-01"}})
    print("\n" + "-" * 74)
    print(f"Stock final del producto: {stock.get('cantidad')} unidades")
    print("-" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
