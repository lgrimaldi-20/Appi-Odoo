# API-Odoo Middleware

Middleware en Python/FastAPI para conectar Odoo ERP con sistemas externos
(Excel, scripts, agentes de IA, bases de datos).

Ofrece dos capas:

- **Proxy JSON-RPC genérico** (`/odoo`): ejecuta cualquier método sobre cualquier
  modelo de Odoo.
- **Capa contable con estado** (`/facturas`, `/pagos`, `/conciliar`): crea y postea
  facturas y pagos de forma **idempotente**, los **concilia**, valida que los
  **totales/impuestos** cuadren, y puede procesar en **segundo plano** con
  reintentos y *rollback* lógico.

## Inicio rapido

```bash
# 1. Clonar y crear entorno virtual
python -m venv venv
.\venv\Scripts\Activate     # Windows
# source venv/bin/activate  # Linux/Mac

# 2. Instalar dependencias
pip install -r requirements.txt

# 3. Configurar variables de entorno
copy .env.example .env      # Windows
# cp .env.example .env      # Linux/Mac
# Edita .env con tus credenciales reales

# 4. Arrancar
uvicorn api:app --reload
```

La API queda disponible en `http://localhost:8000`.
Documentacion interactiva (Swagger): `http://localhost:8000/docs`

## Uso basico

```bash
curl -X POST http://localhost:8000/odoo \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: tu-clave" \
  -d '{"model": "res.partner", "method": "search_read",
       "args": [[["customer_rank", ">", 0]]],
       "kwargs": {"fields": ["name", "email"], "limit": 5}}'
```

## Capa contable (facturas, pagos, conciliacion)

Ademas del proxy `/odoo`, el middleware sincroniza facturas y pagos con estado
propio e idempotencia. El flujo interno es:

```
state store (idempotencia) -> mapper (traduce) -> create + action_post (Odoo)
```

- **Idempotencia**: cada registro de origen se identifica por `(entidad, id_origen)`.
  Reenviar el mismo registro devuelve el `id_odoo` ya asignado sin duplicar.
- **Mapeo declarativo**: la traduccion de campos y la resolucion de claves
  foraneas (p.ej. NIF -> `res.partner`) se configura en `core/mappings.yaml`,
  sin tocar codigo.
- **Conciliacion**: cruza los apuntes contables de una factura y un pago.
- **Impuestos**: valida que el total en Odoo coincida al centimo con el de origen.
- **Estado y auditoria**: consultable en `GET /estado/{entidad}/{id_origen}`;
  bitacora completa en la base de datos de control.

```bash
# Crear + postear una factura (idempotente)
curl -X POST http://localhost:8000/facturas \
  -H "X-Api-Key: tu-clave" \
  -d '{"registro": {"factura_id": "F-100", "cliente_nif": "B123",
                    "fecha": "2026-01-15", "total": 121.00}}'

# Conciliar factura y pago (por sus IDs de Odoo)
curl -X POST http://localhost:8000/conciliar \
  -H "X-Api-Key: tu-clave" \
  -d '{"factura_id_odoo": 42, "pago_id_odoo": 88}'
```

## Inventario (ajuste de existencias)

Permite dar entrada y salida de unidades de productos que ya existen en Odoo.
No escribe la cantidad "a mano": usa el mecanismo oficial de **ajuste de
inventario** (`stock.quant` + `action_apply_inventory`), de modo que Odoo genera
el movimiento correspondiente y el cambio queda trazable.

Modos (campo `modo`):

| Modo | Efecto |
|------|--------|
| `fijar` (por defecto) | La existencia final sera exactamente `cantidad` (conteo real) |
| `incrementar` | Suma `cantidad` a lo que ya hay (entrada) |
| `decrementar` | Resta `cantidad` (salida). Falla si dejaria el stock negativo |

```bash
# Entrada de 5 unidades del producto con referencia ABC-123
curl -X POST http://localhost:8000/stock/ajustar \
  -H "X-Api-Key: tu-clave" \
  -d '{"registro": {"ajuste_id": "AJ-001", "producto_ref": "ABC-123",
                    "cantidad": 5, "modo": "incrementar",
                    "motivo": "Recepcion proveedor"}}'

# Consultar existencia actual (solo lectura)
curl -X POST http://localhost:8000/stock/consultar \
  -H "X-Api-Key: tu-clave" \
  -d '{"registro": {"producto_ref": "ABC-123"}}'
```

**Importante:** el campo `ajuste_id` es obligatorio y hace el ajuste idempotente.
Reenviar el mismo `ajuste_id` no vuelve a aplicarlo — sin esto, un `incrementar`
repetido descuadraria el inventario.

El producto se identifica por su referencia interna (`producto_ref` →
`default_code`) o por `producto_id_odoo`. La ubicacion es opcional: si no se
indica, se usa la primera ubicacion interna del almacen.

### Procesamiento en segundo plano (cola)

`/facturas`, `/pagos` y `/stock/ajustar` aceptan `"async": true` para encolar en background y
responder con un `task_id`; el resultado se consulta luego en `/estado/...`.
La cola usa **Celery + Redis**. Si `CELERY_BROKER_URL` esta vacio, las tareas
corren en **modo eager** (sincrono inline, sin worker) — util en desarrollo.

## Seguridad

| Mecanismo | Configuracion |
|-----------|--------------|
| API Key | Variable `API_KEY` en `.env` |
| Whitelist modelos | Variable `ALLOWED_MODELS` (separados por coma) |
| Whitelist metodos | Variable `ALLOWED_METHODS` (separados por coma) |
| Rate limiting | 60 peticiones/minuto por IP |

## Endpoints

| Metodo | Ruta | Auth | Descripcion |
|--------|------|------|-------------|
| GET | `/health` | No | Estado del servicio y conexion Odoo |
| POST | `/odoo` | Si | Ejecutar operacion generica en Odoo |
| POST | `/facturas` | Si | Crear + postear factura (idempotente) |
| POST | `/pagos` | Si | Crear + postear pago (idempotente) |
| POST | `/conciliar` | Si | Conciliar una factura con un pago |
| POST | `/stock/ajustar` | Si | Ajustar existencias de un producto (idempotente) |
| POST | `/stock/consultar` | Si | Consultar existencias de un producto |
| GET | `/estado/{entidad}/{id_origen}` | Si | Estado de sincronizacion de un registro |
| GET | `/docs` | No | Documentacion interactiva (Swagger) |

## Codigos de respuesta

| Codigo | Significado |
|--------|-------------|
| 200 | Operacion exitosa |
| 401 | API Key invalida o ausente |
| 422 | Modelo/metodo no permitido, error de Odoo, mapeo, conciliacion o descuadre |
| 429 | Rate limit superado |
| 503 | Odoo no disponible |

## Docker

```bash
# Solo la API
docker build -t api-odoo .
docker run --env-file .env -p 8000:8000 api-odoo

# Stack completo con cola: API + worker Celery + Redis
docker compose up --build
```

## Tests

```bash
pytest tests/ -v
```

## Documentacion adicional

- [docs/guia-agentes.md](docs/guia-agentes.md) - **Guia completa para equipos de agentes** (FAQ, cliente Python, ejemplos)
- [docs/ejemplos-agentes.md](docs/ejemplos-agentes.md) - Ejemplos rapidos en Python, Node.js y curl
- [docs/power-query-template.md](docs/power-query-template.md) - Plantilla M para Excel/Power Query
- [docs/etl-sync.md](docs/etl-sync.md) - Sincronizacion ETL a PostgreSQL/MySQL
- [docs/paso-a-produccion.md](docs/paso-a-produccion.md) - **Flujo automatico y checklist de produccion**
