# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Python/FastAPI middleware between an external system and Odoo ERP. It started as a **generic JSON-RPC proxy** (`POST /odoo` — run any method on any model) and now also provides a **stateful accounting sync layer**: idempotent creation + posting of invoices and payments, reconciliation, tax-total validation, and background processing with retries and logical rollback.

Code comments and log messages are in Spanish; keep that convention. Follow PEP8.

## Commands

```bash
# Run dev server (auto-reload)
uvicorn api:app --reload            # http://localhost:8000, Swagger at /docs

# Tests
pytest tests/ -v
pytest tests/test_sincronizador.py -v                          # one file
pytest tests/test_api.py::TestAutenticacion -v                 # one class
pytest tests/test_api.py::TestAutenticacion::test_acepta_api_key_correcta -v   # one test

# Docker (API only)
docker build -t api-odoo .
docker run --env-file .env -p 8000:8000 api-odoo

# Full stack with queue (web + Celery worker + Redis)
docker compose up --build
# Run the Celery worker directly (needs CELERY_BROKER_URL set):
celery -A core.celery_app.celery_app worker --loglevel=info
```

**Environment note:** the checked-in `venv/` has a Unix layout (`venv/bin/`) and is **not** runnable on native Windows. Tests here run against a global Python 3.14 with the deps installed. `.\venv\Scripts\Activate` will not work until the venv is recreated on Windows.

## Architecture

The service is layered. The two original files are the **transport layer**; everything stateful lives under `core/` and `routers/`.

### Transport (unchanged foundation)
- **[odoo_universal.py](odoo_universal.py)** — `OdooUniversalAPI`, a thin JSON-RPC client. Constructor logs in immediately (raises `OdooConnectionError`); `execute(model, method, *args, **kwargs)` calls `execute_kw`. Typed exceptions: `OdooConnectionError` (network/auth) vs `OdooExecutionError` (Odoo returned an error). Multi-tenant via a module-level `_tenants` dict — `register_tenant()` / `get_tenant()`.
- **[api.py](api.py)** — the FastAPI app. On import: builds the `"default"` tenant from `ODOO_*` env vars (a failed connection is logged, does **not** crash startup — `/health` reports `degradado`), calls `init_db()`, and mounts the business routers. Holds `/odoo` (generic proxy) and `/health`.

### Stateful accounting layer (`core/`)
The invoice/payment flow chains three pieces: **state store** (idempotency) → **mapper** (translate) → **execute** (create + post).

- **[core/state_store.py](core/state_store.py)** + **[core/models_db.py](core/models_db.py)** — the **control database** (SQLAlchemy, separate from Odoo). Two tables: `sync_map` (unique `(entidad, id_origen)` ↔ `id_odoo` + `estado` + `hash_payload`) and `sync_log` (append-only audit). States: `PENDIENTE → PROCESANDO → PROCESADO | ERROR`. `DATABASE_URL` defaults to local SQLite; use PostgreSQL in prod. This is what makes everything idempotent.
- **[core/mapper.py](core/mapper.py)** + **[core/mappings.yaml](core/mappings.yaml)** — declarative mapping from a source record to Odoo values. Applies `defaults`, renames fields, and resolves FKs (`resolver`) by looking up the real Odoo id from a business value (e.g. NIF→`res.partner.vat`). FK resolution doubles as master-data validation. Add/change an entity by editing the **YAML**, not code. Raises `MapeoError`.
- **[core/sincronizador.py](core/sincronizador.py)** — the orchestrator. `sincronizar_entidad(entidad, registro, odoo)`: idempotency check → `PROCESANDO` → map → `create` (id saved immediately) → `action_post` → optional total validation → `PROCESADO`. Every step logs; any failure sets `ERROR` and raises `SincronizacionError`. **Key detail:** if `create` succeeds but `action_post` fails, the `id_odoo` is kept and the error says "created but not posted" — so rollback knows what to compensate.
- **[core/facturacion.py](core/facturacion.py)** / **[core/pagos.py](core/pagos.py)** — thin business wrappers (`crear_factura` / `crear_pago`) over the orchestrator for `account.move` / `account.payment`.
- **[core/conciliacion.py](core/conciliacion.py)** — `conciliar(factura_id_odoo, pago_id_odoo, odoo, ...)`. Reconciles by finding the receivable/payable `account.move.line`s of the invoice and the payment's move and calling `reconcile()`. Idempotent via entity `"conciliacion"` keyed `"<factura>:<pago>"`. Raises `ConciliacionError`. **Note:** it checks `estado == PROCESADO` directly (not `ya_procesado()`) because reconciliation has no single `id_odoo`.
- **[core/impuestos.py](core/impuestos.py)** — `verificar_total()`. Compares Odoo's `amount_total` with the source total using `Decimal` (via `str`, to avoid float noise) and a 1-cent tolerance. Raises `DescuadreError`; the invoice stays `ERROR` (not marked done). Wired into the orchestrator only when the entity's YAML has `validar_total: <campo>`.
- **[core/inventario.py](core/inventario.py)** — stock adjustments. `ajustar_stock(registro, odoo)` resolves the product (`default_code`) and internal location, finds/creates the `stock.quant`, writes `inventory_quantity`, then calls **`action_apply_inventory()`** — the official path, so Odoo generates the traceable `stock.move` instead of silently overwriting a quantity. Modes: `fijar` (absolute count, default) / `incrementar` / `decrementar`; a decrement below zero is refused. Idempotent by the source `ajuste_id` — **essential**, since replaying an `incrementar` would silently corrupt inventory. `consultar_stock()` is the read-only counterpart. Raises `InventarioError`. Note this module does **not** use `mappings.yaml` (its input is a fixed, small schema, not a record translation).

### Queue, retries, rollback (`core/`)
- **[core/celery_app.py](core/celery_app.py)** — Celery config. **Eager mode is automatic when `CELERY_BROKER_URL` is empty** (tasks run inline, synchronously) — that's how tests and dev run without Redis.
- **[core/tasks.py](core/tasks.py)** — background tasks. `sincronizar_factura_task` / `sincronizar_pago_task` retry with exponential backoff **only** on `OdooConnectionError` (not on data errors). `procesar_venta_task` runs the full invoice→payment→reconcile flow and triggers **logical rollback** ([core/rollback.py](core/rollback.py) `cancelar_factura`: `button_draft` + `button_cancel`) if payment/reconciliation fails after the invoice was created. Tasks resolve the tenant by name via `get_tenant` (the connector isn't serializable).

### Routers (`routers/`)
`/facturas`, `/pagos`, `/conciliar`, `/stock/*` — each protected by the shared **[core/seguridad.py](core/seguridad.py)** (`verify_api_key` reads `API_KEY` from env each call; `resolver_tenant` → 400 on unknown tenant). Extracted to a neutral module so routers don't import `api.py` (avoids a circular import). `/facturas`, `/pagos` and `/stock/ajustar` take an optional `"async": true` field → enqueue and return `{encolado, task_id}` (in eager mode this still runs inline); otherwise synchronous. `api.py` also has `GET /estado/{entidad}/{id_origen}` to read a record's sync state.

### Endpoints
`/odoo` (generic, auth) · `/facturas` · `/pagos` · `/conciliar` · `/stock/ajustar` · `/stock/consultar` · `/estado/{entidad}/{id_origen}` · `/health` (no auth).

Note the whitelists (`ALLOWED_MODELS`/`ALLOWED_METHODS`) only gate the generic `/odoo` endpoint — the business routers call Odoo directly through the connector.

## Security & error codes

Security layers, all `.env`-driven and **no-ops when their var is empty**: `API_KEY` (empty → endpoint unprotected, dev mode), `ALLOWED_MODELS` / `ALLOWED_METHODS` (comma-separated whitelists, enforced inside `OdooRequest`'s `field_validator`s → 422), rate limit fixed at `60/minute` per IP (slowapi).

Error mapping: `401` bad/missing API key · `422` whitelist rejection, `OdooExecutionError`, `SincronizacionError`, `ConciliacionError`, `DescuadreError` · `429` rate limit · `503` `OdooConnectionError` · `400` unknown tenant · `500` other.

## Adding capabilities

- **New model/method for `/odoo`:** edit `ALLOWED_MODELS` / `ALLOWED_METHODS` in `.env`.
- **New mapped entity (or change a field mapping):** edit [core/mappings.yaml](core/mappings.yaml). `id_origen` names the source-record key used for idempotency; `resolver` entries look up FKs; optional `validar_total` enables the tax-total check.
- **Add a tenant:** `register_tenant(name, OdooUniversalAPI(...))` at startup in [api.py](api.py); clients pass `"tenant": "<name>"`.

## Tests

HTTP tests ([tests/test_api.py](tests/test_api.py), [tests/test_routers.py](tests/test_routers.py)) patch `OdooUniversalAPI._login` **before importing api.py** and set dummy `ODOO_*` env vars, so no real Odoo connection is made. `core/` unit tests isolate the state store by pointing `DATABASE_URL` at a temp SQLite file (via `monkeypatch.setenv` + `importlib.reload(state_store)`) and mock the `odoo` connector.

**Gotcha:** do **not** `importlib.reload(core.tasks)` in a fixture — it re-registers the Celery tasks and corrupts global state across tests (this caused rollback tests to pass in isolation but fail in the full suite). Patch dependencies by name with `monkeypatch` instead (auto-reverted). When mocking a function a router imported by name (`from core.facturacion import crear_factura`), patch it **on the router module** (`routers.facturas.crear_factura`), not on the source module.

## scripts/ — standalone Odoo utilities (not part of the API)

CLI tools that call the running middleware over HTTP (they read `API_KEY` from env or by parsing `.env`). `excel_a_odoo.py` ↔ `generar_excel_demo.py` do bidirectional Excel⇄Odoo sync on `res.partner` (rows with an ID → `write`, without → `create`). `borrar_duplicados.py` supports `--dry-run`. These require the server running.

## Docs

Extended integration guides (agent client, Power Query/Excel M template, ETL to PostgreSQL/MySQL) live in [docs/](docs/); [README.md](README.md) links each one.
