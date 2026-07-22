"""
Paquete core del middleware API-Odoo.

Contiene la logica de negocio con estado que se construye sobre el conector
de transporte (odoo_universal.py):

  - state_store:   base de datos de control (idempotencia, reintentos, auditoria)
  - mapper:        traduce registros de origen al esquema Odoo y resuelve FKs
  - sincronizador: orquesta idempotencia -> mapeo -> create -> post
  - facturacion:   sincroniza account.move
  - pagos:         sincroniza account.payment
  - conciliacion:  cruza (concilia) apuntes de factura y pago
  - impuestos:     valida que el total en Odoo cuadre con el de origen
  - inventario:    ajusta existencias (stock.quant + action_apply_inventory)
  - seguridad:     dependencias FastAPI compartidas (API key, tenant)
  - celery_app:    configuracion de la cola (con modo eager sin broker)
  - tasks:         tareas en background con reintentos y rollback logico
  - rollback:      compensacion (cancelar factura) ante fallos posteriores
"""
