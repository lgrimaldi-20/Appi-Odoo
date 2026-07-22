"""
Routers de negocio del middleware API-Odoo.

  - facturas: POST /facturas  -> crea + postea account.move (idempotente)
  - pagos:    POST /pagos     -> crea + postea account.payment (idempotente)

Se montan en api.py con app.include_router(...).
"""
