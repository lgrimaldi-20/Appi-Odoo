"""
Siembra la cola de origen (modo pull) con filas de prueba.

Simula lo que haria el CLIENTE en su propia base de datos: dejar registros en
cola_sincronizacion para que el middleware los sondee. No toca Odoo.

Uso:  python scripts/sembrar_cola_poller.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

if not os.getenv("SOURCE_DATABASE_URL"):
    print("[X] Falta SOURCE_DATABASE_URL en .env (modo pull apagado).")
    sys.exit(1)

from core import poller_source  # noqa: E402

poller_source.init_source_db()
print(f"Cola lista en {poller_source.SOURCE_DATABASE_URL}")

NIF = "J-12345678-9"
IVA = [[6, 0, [1]]]

# Casos: dos correctos (uno en USD, otro en bolivares) y uno con un NIF que no
# existe en Odoo, para comprobar el AISLAMIENTO de errores: esa fila debe quedar
# en ERROR sin arrastrar a las demas.
FILAS = [
    ("factura", "POLL-001", {
        "factura_id": "POLL-001", "cliente_nif": NIF, "fecha": "2026-08-17",
        "referencia": "Poller USD", "total": 230.0,
        "lineas": [[0, 0, {"name": "Servicio via poller", "quantity": 2,
                           "price_unit": 100.0, "tax_ids": IVA}]],
    }),
    ("factura", "POLL-002-VES", {
        "factura_id": "POLL-002-VES", "cliente_nif": NIF, "moneda_iso": "VES",
        "fecha": "2026-08-17", "referencia": "Poller bolivares", "total": 2300.0,
        "lineas": [[0, 0, {"name": "Servicio en Bs via poller", "quantity": 2,
                           "price_unit": 1000.0, "tax_ids": IVA}]],
    }),
    ("factura", "POLL-003-MALA", {
        "factura_id": "POLL-003-MALA", "cliente_nif": "J-00000000-0",
        "fecha": "2026-08-17", "referencia": "NIF inexistente", "total": 115.0,
        "lineas": [[0, 0, {"name": "Debe fallar", "quantity": 1,
                           "price_unit": 100.0, "tax_ids": IVA}]],
    }),
]

with poller_source.get_source_session() as s:
    for entidad, id_origen, payload in FILAS:
        ya = (
            s.query(poller_source.ColaSincronizacion)
            .filter_by(entidad=entidad, id_origen=id_origen)
            .first()
        )
        if ya:
            print(f"  = {id_origen}: ya existe (#{ya.id}, {ya.estado})")
            continue
        s.add(poller_source.ColaSincronizacion(
            entidad=entidad, id_origen=id_origen, payload=payload,
            estado="PENDIENTE",
        ))
        print(f"  + {id_origen}: encolada")

with poller_source.get_source_session() as s:
    filas = s.query(poller_source.ColaSincronizacion).order_by(
        poller_source.ColaSincronizacion.id).all()
    print("\nEstado de la cola:")
    for f in filas:
        print(f"  #{f.id} {f.entidad}/{f.id_origen} -> {f.estado}")
