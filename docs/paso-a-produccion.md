# Paso a producción — flujo automático

Estado a 2026-09-04. Recoge lo añadido en la última tanda: sincronización
automática de clientes, servicio `beat`, registro compartido de tenant y
traducción de errores.

---

## 1. El flujo, de punta a punta

Con la pila levantada y `beat` corriendo, esto ocurre sin que nadie intervenga:

```
Smartier (API, solo lectura)
   │
   ├── [maestros]  cada 15 min ── crea/actualiza CLIENTES en Odoo
   │                              (solo escribe lo que cambió)
   │
   └── [ingesta]   cada  5 min ── lee NOTAS "Facturada" → cola_sincronizacion
                                       │
                                       ▼
                            [poller] cada 30 s
                                       │
                                       ▼
                          Odoo: factura creada y posteada
```

**Por qué los maestros van primero:** una nota necesita que su cliente exista
en Odoo para resolver el `partner_id`. Los intervalos (15 min vs 5 min) están
elegidos para que el cliente llegue antes que su primera nota.

### Qué sigue siendo manual

| Tarea | Quién |
|---|---|
| Cobros y conciliación | Contabilidad, en Odoo |
| Condición de agente de retención por cliente | Contabilidad (la designa el SENIAT) |
| RIF que Smartier no envía | Turicopy / Contabilidad |
| Conciliación bancaria | Contabilidad |
| Productos | Script manual (`sincronizar_productos_smartier.py`) |

El flujo automático llega hasta **la factura posteada**. De ahí en adelante es
trabajo contable, y así debe ser: son decisiones con consecuencias fiscales.

---

## 2. Lo que cambia respecto a hoy

### 2.1 Base de datos: SQLite → PostgreSQL

**Obligatorio.** Hoy el `docker-compose` monta las bases SQLite desde el host.
Sirve para probar, pero con `web`, `worker` y `beat` escribiendo a la vez
SQLite bloquea la base entera en cada escritura y aparecen errores de
`database is locked` bajo carga.

```bash
DATABASE_URL=postgresql+psycopg2://usuario:clave@host:5432/control
```

Al cambiarla, **quitar los `volumes` de los tres servicios** en el compose: ya
no hay ficheros que montar.

> El panel y los scripts leen `DATABASE_URL` **al arrancar**. Si se cambia, hay
> que reiniciar, o el panel seguirá mostrando la base anterior mientras los
> procesos escriben en la nueva.

### 2.2 Broker: obligatorio

```bash
CELERY_BROKER_URL=redis://redis:6379/0
CELERY_RESULT_BACKEND=redis://redis:6379/0
```

Con esta variable **vacía**, Celery entra en modo *eager*: las tareas se
ejecutan inline y **no hay temporizador**. Es decir, sin broker el flujo
automático no existe, por muy bien configurado que esté `beat`.

### 2.3 Beat: exactamente una instancia

`beat` es el reloj. Si se escala el servicio a 2 réplicas, **cada tarea se
encola dos veces**: dos ingestas leyendo lo mismo y dos pasadas de maestros
compitiendo. El `worker` sí se puede escalar; `beat` no.

### 2.4 Usuario de Odoo

Hoy la integración usa `admin`. En producción debe ser un usuario dedicado con
permisos de facturación y contactos, y nada más. Si la clave se filtra, el daño
queda acotado y el rastro de auditoría en Odoo dice quién hizo qué.

### 2.5 Claves

La API Key de Smartier se compartió por chat durante el desarrollo: hay que
**revocarla y generar una nueva** antes de producción. Lo mismo con `API_KEY`
del middleware — la de desarrollo no debe viajar al servidor.

---

## 3. El punto que más vigilar: notas en ERROR no se reintentan

Es la consecuencia menos evidente del diseño y conviene tenerla clara.

Cuando el poller falla al procesar una fila por un problema de **datos** —
cliente sin RIF, producto que no existe, total descuadrado — la marca `ERROR`
en la cola y **sigue con las demás**. Eso es correcto: una nota mala no debe
frenar al resto.

Pero `tomar_lote()` solo lee filas en `PENDIENTE`. **Una fila en ERROR no se
vuelve a intentar nunca**, aunque la causa se corrija después.

Caso concreto que va a pasar: llega una nota de un cliente nuevo antes de que
la pasada de maestros lo haya creado. La nota queda en ERROR. Quince minutos
más tarde el cliente ya está en Odoo — pero la nota sigue en ERROR y no se
factura sola.

**Mitigación hoy:** son visibles en el panel (pestaña *Cola del poller*, con el
motivo ya traducido al castellano), y se reprocesan poniendo la fila de nuevo
en `PENDIENTE`. Reprocesar es seguro: `sync_map` garantiza que no se duplique
en Odoo.

**Pendiente:** un reintento automático para los errores que sí son
transitorios. No está hecho, y es el hueco más relevante del flujo.

Los fallos de **conexión** se comportan al revés y bien: no marcan la fila,
abortan el lote y Celery reintenta con espera creciente.

---

## 4. Antes de encender

- [ ] `DATABASE_URL` a PostgreSQL, y quitar los `volumes` del compose
- [ ] `CELERY_BROKER_URL` apuntando a Redis
- [ ] `beat` en **una** sola instancia
- [ ] Usuario de Odoo dedicado, no `admin`
- [ ] Claves nuevas (Smartier y `API_KEY`)
- [ ] **RIF cargados** — sin esto las facturas se rechazan
- [ ] Plan contable venezolano oficial (el actual son 7 cuentas de prueba)
- [ ] Revisar `wh_iva_agent` / `islr_withholding_agent` de la compañía: hoy
      están en `True` por el `default` del módulo, sin que nadie lo decidiera
- [ ] Probar en Odoo: cobro, conciliación y una factura en USD

---

## 5. Lo que no cubre este flujo

- **Comprobante de retención del SENIAT**, libros de compra/venta y TXT. La
  localización los trae; no se han ejercitado.
- **Notificación de errores.** Hoy nadie se entera de que algo falló salvo que
  mire el panel. Con volumen real, conviene un aviso cuando se acumulen.
- **Productos automáticos.** Solo los clientes están programados en Beat.
