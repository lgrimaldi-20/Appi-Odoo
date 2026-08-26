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
# Tipos de documento: el campo "tipo" elige el move_type en Odoo.
#   factura            -> out_invoice  venta (por defecto)
#   nota_credito       -> out_refund   devolucion a cliente (abono)
#   factura_proveedor  -> in_invoice   compra
#   nota_debito        -> in_refund    devolucion a proveedor
# Los de compra identifican al tercero por "proveedor_nif" en vez de
# "cliente_nif". Un tipo desconocido se RECHAZA (no cae a factura de venta:
# eso invertiria el signo contable sin avisar).

# Impuestos: se envian por NOMBRE, no por id de Odoo (los ids son internos y
# cambian entre instancias). El middleware los resuelve filtrando por el uso
# que toca (venta/compra), porque el mismo nombre suele existir para ambos:
#   "lineas": [[0, 0, {"product_id": 2, "quantity": 4, "price_unit": 250.0,
#                      "impuestos": ["15%"]}]]
# Un nombre inexistente es error (cambiaria el total). Sigue admitiendose
# "tax_ids": [[6, 0, [1]]] con ids crudos para lo ya integrado.

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

## Modo pull (poller): el cliente no llama al middleware

Alternativa al modo push. El cliente deja los registros en **su propia base de
datos** (por aislamiento: es dueno de sus datos) y el middleware la sondea.

```
Cliente escribe -> poller lee (Celery Beat) -> Odoo -> escribe el resultado de vuelta
```

Se activa apuntando `SOURCE_DATABASE_URL` a la base del cliente, que debe tener
una tabla `cola_sincronizacion` (`entidad`, `id_origen`, `payload` JSON,
`estado` PENDIENTE|PROCESADO|ERROR, `error_detalle`). La columna `entidad`
acepta `factura`, `pago`, `ajuste_stock` y `asiento`: el poller despacha cada
una a su modulo, asi que por modo pull se puede enviar todo lo que admite el
modo push. Es el **unico** permiso de
escritura que el middleware necesita alli: marcar el resultado.

```bash
# Tick automatico cada POLLER_INTERVALO_SEG (30 s por defecto). Necesita Redis.
celery -A core.celery_app.celery_app beat --loglevel=info

# Pasada manual, sin esperar al tick (util en pruebas o desde el panel)
curl -X POST http://localhost:8000/poller/ejecutar -H "X-Api-Key: $API_KEY"
# -> {"leidas": 5, "procesadas": 3, "con_error": 2}
```

**Aislamiento de errores:** un fallo de datos deja esa fila en `ERROR` y el
poller **continua con las demas**. Un fallo de conexion aborta el lote entero y
Celery reintenta la pasada completa mas tarde.

**Doble idempotencia:** la cola dice *que* enviar; el `sync_map` del middleware
dice *que ya* se envio. Reencolar una fila ya procesada **no** duplica nada en
Odoo.

## Panel de observabilidad

```
http://localhost:8000/panel
```

Dashboard de solo lectura sobre la base de control: totales por estado y
entidad, sincronizaciones, logs y el detalle de cada registro con su
traza completa (`crear -> postear -> validar_total -> rollback -> ...`). Es
donde se revisan los `ERROR`. La pagina pide la API Key y la envia en cada
consulta; los endpoints de datos (`/panel/api/*`) van protegidos.

Las tablas paginan (50/100/250/500 por pagina) con botones Anterior/Siguiente y
un contador "N-M de T", asi que ninguna sincronizacion queda inalcanzable por
muchos registros que haya.

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
| 409 | Ese mismo `id_origen` se esta procesando ahora mismo (reintenta en unos segundos) |
| 422 | Modelo/metodo no permitido, error de Odoo, mapeo, conciliacion o descuadre |
| 429 | Rate limit superado |
| 503 | Odoo no disponible |

## Que pasa cuando algo falla

El middleware es idempotente y no deja registros a medias en Odoo. Esta tabla
resume que ocurre en cada tipo de fallo y **quien tiene que actuar**.

| Fallo | Estado en Odoo | Estado en el middleware | Se compensa solo | Que hacer |
|-------|----------------|-------------------------|------------------|-----------|
| Cliente/producto/diario inexistente (mapeo) | nada creado | `ERROR` | no hace falta | crear el dato maestro en Odoo y reenviar |
| Odoo rechaza el `create` (campo invalido) | nada creado | `ERROR` | no hace falta | corregir el payload y reenviar |
| Falla el `action_post` | creado **en borrador** (no contabiliza) | `ERROR` con `id_odoo` | **no** | revisar en Odoo: postear a mano o borrar |
| **Descuadre de total** | **posteado** (asiento real) | `ERROR` con `id_odoo` | **si**, por HTTP y por poller | corregir el importe en origen y reenviar |
| Odoo caido / red | puede haber quedado creado | `PENDIENTE`, conserva `id_odoo` | se **adopta** al reintentar | nada: se reintenta solo |
| Peticion duplicada simultanea | 1 solo registro | `PROCESANDO` | — | la segunda recibe **409** |
| Reenvio de algo ya procesado | 1 solo registro | `PROCESADO` | — | responde `idempotente: true` |

### Por que el descuadre es el caso especial

La validacion de total corre **despues** del `action_post` (hay que preguntarle
a Odoo cuanto suma con sus impuestos). Si no cuadra, la factura ya esta
**posteada**: es un asiento contable real que nadie dio por bueno.

El middleware la **cancela automaticamente**, tanto si llego por HTTP como por el
poller (`button_draft` + `button_cancel`; un pago usa `action_cancel`), para no
dejar contabilidad huerfana. Se desactiva con `POLLER_CANCELAR_DESCUADRE=false`.

Solo se cancela el descuadre, a proposito:

- un fallo de `action_post` deja la factura **en borrador**: no contabiliza nada
  y la causa puede ser transitoria;
- un fallo de mapeo **no llego a crear nada**, asi que no hay nada que cancelar.

La fila queda en `ERROR` y **no se reintenta sola**: reintentarla recrearia y
volveria a cancelar la misma factura en bucle en cada pasada. El descuadre suele
venir de un impuesto mal mapeado, y eso no se arregla con el tiempo.

### Si se cae la conexion a mitad

Odoo no participa en la transaccion del middleware: si la red cae despues del
`create`, el registro queda vivo en Odoo aunque la peticion aborte. En ese caso
el estado vuelve a `PENDIENTE` **conservando el `id_odoo`**, y el reintento
**adopta** ese registro en vez de crear un duplicado.

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
