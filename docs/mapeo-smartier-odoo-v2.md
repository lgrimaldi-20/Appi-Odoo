# Mapeo de campos: Smartier/Turicopy → Middleware → Odoo v17

**Versión 2** — revisada contra la instancia real y contra el código de la localización.
Fecha: 2026-09-02 · Instancia: `lgrimaldi-20-turicopy-pruebas` (Odoo 17.0)

> **Qué cambia respecto a la v1.** La v1 proponía campos `x_smartier_*` creados con
> Studio y asumía que la localización venezolana estaba disponible. Ambas cosas se
> han verificado y ninguna se sostiene: el middleware ya resuelve la idempotencia
> sin campos custom, y la localización aporta ~50 campos fiscales propios que hacen
> innecesarios varios de los que se proponían. Las secciones marcadas **[v2]**
> corrigen o sustituyen lo anterior.

---

## 0. Estado verificado (2026-09-02)

Todo lo de esta sección está comprobado contra los sistemas reales, no inferido.

### API Smartier

| Recurso | Registros | Nota |
|---|---:|---|
| `/external/clientes` | 3 | los **3 sin RIF** (`Documento.Contenido: null`) |
| `/external/productos` | 55 | todos IVA 16 %, ninguno exento, todos `Disponible` |
| `/external/vendedores` | 3 | |
| `/external/recursos` | 82 | |
| `/external/sectores` | 24 | |
| `/external/notas-entrega` | **0** | |
| `/external/ordenes` | **0** | |
| `/external/tickets` | **0** | |

El DTO de cliente tiene **8 campos** y ninguno es fiscal: `Id`, `Nombre`, `Tipo`,
`Estado`, `Documento{Tipo,Contenido}`, `Email`, `RazonSocial`, `CreadoUtc`.
Comprobado también el detalle individual (`/external/clientes/8`): devuelve los
mismos 8. **Smartier no conoce las retenciones**; ese dato tiene que salir de otro
sitio.

### Odoo

| Módulo | Estado |
|---|---|
| `account`, `account_accountant`, `account_reports`, `sale_management`, `stock`, `crm`, `contacts` | instalados |
| `mrp`, `project`, `delivery`, `l10n_ve`, `purchase`, `base_vat` | **sin instalar** (presentes en el servidor) |
| `l10n_ve_full` (localización) | **no cargado** — ver §13 |

Consecuencia inmediata: `mrp.production`, `mrp.workorder`, `mrp.workcenter`,
`mrp.workcenter.tag`, `project.task` y `delivery.carrier` **no existen como
modelos** hoy. Las secciones §5b, §6, §8, §9 y parte de §10 no son programables
hasta que se instale Fabricación.

---

## 1. Alcance

- **Origen:** API Smartier/Turicopy (`https://turicopy.smartier.software/api/v2/external/...`),
  solo lectura, `X-Api-Key`, paginada (`Page`, `PageSize` máx. **200**, `Sort`).
  Campos ordenables: **`Id`, `Fecha`, `Estado`** — cualquier otro devuelve 400.
  Límite de **5 req/s** por key.
- **Destino:** Odoo 17 vía JSON-RPC (`execute_kw`).
- **Dirección:** unidireccional. Smartier es la fuente de verdad; Odoo no escribe
  hacia atrás.

### Alcance implementado vs. alcance del mapeo **[v2]**

El middleware hoy cubre **un** flujo, no el ERP entero:

```
Smartier → ingesta → cola_sincronizacion → poller → account.move (factura)
```

Este documento mapea además producción, tickets, recursos, sectores y entregas.
Son **dos proyectos de tamaño distinto**; conviene decidir explícitamente el
alcance antes de estimar (§14, pregunta 3).

---

## 2. Clientes → `res.partner` **[v2]**

### 2.1 Campos que vienen de Smartier

| Campo Smartier | Campo Odoo | Estado | Nota |
|---|---|---|---|
| `Id` | `ref` = `SMARTIER-<id>` | ✅ implementado | Ver §11: no hace falta `x_smartier_id` |
| `Nombre` / `RazonSocial` | `name` | ✅ | `RazonSocial` si existe, si no `Nombre` |
| `Tipo` (`Contacto`/`Empresa`) | `company_type` (`person`/`company`) | ✅ | El `Tipo` manda; antes se deducía de `RazonSocial`, lo que marcaba como empresa a un contacto con razón social |
| `Estado` (`Habilitado`/`Deshabilitado`) | `active` | ✅ | Solo `Deshabilitado` archiva; un estado desconocido deja el registro **visible** (un archivado por error desaparece de las búsquedas sin que nadie lo note) |
| `Documento.Contenido` | `vat` **y** `rif` | ⏳ hoy `null` en los 3 | `rif` solo con localización |
| `Documento.Tipo` (int) | `nationality` (V/E/P) | ❓ | Falta el catálogo de códigos; hoy todos traen `6` |
| `Email` | `email` | ✅ | |
| `CreadoUtc` | — | ❌ descartado | No aporta: `create_date` de Odoo ya fecha el alta, y la trazabilidad de origen vive en `sync_map` |

### 2.2 Campos fiscales de la localización — **NO vienen de Smartier**

Extraídos de `l10n_ve_full/models/res_partner.py` (v17.0.2.3.0):

| Campo | Tipo | Etiqueta | Default |
|---|---|---|---|
| `rif` | Char | RIF | — |
| `nationality` | Selection | V / E / P | `V` |
| `identification_id` | Char | Documento de Identidad | — |
| `people_type_individual` | Selection | PNRE / PNNR | `pnre` |
| `people_type_company` | Selection | PJDO / PJND | `pjdo` |
| `contribuyente_seniat` | Selection | Ordinario/Especial/Formal/Gubernamental | `ordinario` |
| **`wh_iva_agent`** | Boolean | ¿Es Agente de Retención (IVA)? | **`True`** ⚠️ |
| **`wh_iva_rate`** | Float | % Retención de IVA | **0.0** ⚠️ (ver aviso) |
| `vat_subjected` | Boolean | Declaración legal de IVA | `True` |
| **`islr_withholding_agent`** | Boolean | ¿Agente de retención de ISLR? | **`True`** ⚠️ |
| `islr_exempt` | Boolean | ¿Exento de retención de ingresos? | `False` |
| `spn` | Boolean | ¿Sociedad de personas físicas? | `False` |
| `agente_retencion_mun` | Boolean | ¿Agente de Retención Municipal? | `False` |
| `porc_reten_muni` | Float | % Retención Municipal | `100.0` |
| `municipality_id` / `parish_id` | Many2one | Municipio / Parroquia | — |
| `licencia_municipal` | Char | Licencia Municipal | — |

> **⚠️ Aviso 1 — bug en el módulo.** `wh_iva_rate` se declara con
> `dafault=75.0` (línea 36, `res_partner.py`). Está mal escrito: Odoo ignora el
> kwarg desconocido y **el campo arranca en 0.0, no en 75 %**. Cualquiera que cree
> un contacto desde la interfaz se topará con esto. Conviene reportarlo al equipo
> de `sma_l10n_ve`.

> **⚠️ Aviso 2 — defaults peligrosos.** `wh_iva_agent` e `islr_withholding_agent`
> vienen en `True`. Todo contacto nuevo nace marcado como agente de retención sin
> que nadie lo decida, y a partir de ahí Odoo le retiene.

### 2.3 Regla adoptada: neutro al crear, intocable al actualizar **[v2]**

Ya implementado en `scripts/sincronizar_clientes_smartier.py`:

| Campo | `create` | `write` |
|---|---|---|
| `wh_iva_agent`, `wh_iva_rate`, `islr_withholding_agent` | `False` / `0.0` explícito | **no se envía** |
| `name`, `ref`, `active`, `company_type` | ✅ | ✅ |
| `vat` | solo si Smartier lo trae | solo si Smartier lo trae |

Razones:

1. **Al crear** se fija un estado neutro para no heredar los defaults del módulo.
   Retener de menos se detecta y se corrige; retener a quien no corresponde es
   dinero ajeno enviado al fisco.
2. **Al actualizar** no se toca nada fiscal: es competencia de contabilidad y una
   pasada del sincronizador no debe revertir lo configurado a mano.
3. **`vat` solo con valor**, nunca vacío, para no borrar un RIF cargado en Odoo.

La localización se detecta por la existencia del campo `wh_iva_agent`, no por el
nombre del módulo. Sin ella no se envía nada, porque mandar campos inexistentes
rompería el `create` entero.

---

## 3. Vendedores → `res.partner` **[v2 — cambia el destino]**

La v1 proponía `res.users`. **Se recomienda `res.partner`** salvo que los
vendedores deban entrar en Odoo:

- Cada `res.users` **consume una licencia** de Odoo Enterprise.
- Smartier solo da `Id`, `Nombre` y `Email` — insuficiente para un usuario real.
- Como referencia comercial basta un contacto con `ref = SMARTIER-V<id>`.

| Campo Smartier | Campo Odoo (`res.partner`) |
|---|---|
| `Id` | `ref` = `SMARTIER-V<id>` |
| `Nombre` | `name` |
| `Email` | `email` |

Si más adelante hacen falta usuarios reales, se crea el `res.users` enlazado al
`res.partner` que ya existe. El camino inverso (users → partners) es más costoso.

---

## 4. Productos → `product.product` **[v2]**

| Campo Smartier | Campo Odoo | Estado |
|---|---|---|
| `Id` | `default_code` = `SMARTIER-<id>` | ✅ implementado |
| `Nombre` | `name` | ✅ |
| `Estado` | `active` + `sale_ok` | ✅ ver tabla |
| `PorcentajeIVA` | `taxes_id` → `account.tax` | ✅ IVA 16 % creado (id=3) |
| `Exento` | `taxes_id` vacío | ✅ |
| `Tipo` | `description_sale` (informativo) | ✅ parcial |

**Estados** (implementado y con tests):

| Estado Smartier | `active` | `sale_ok` |
|---|---|---|
| `Disponible` | ✅ | ✅ |
| `Borrador` | ✅ | ❌ existe pero no se factura |
| `Deshabilitado` | ❌ archivado | ❌ |
| *(desconocido)* | ✅ | ✅ no archiva por defecto |

**Tipo de producto:** todos se crean como `service` (decisión "Opción A"): Odoo no
lleva inventario paralelo, Smartier es la única fuente de verdad de stock. Si se
instala Fabricación habrá que revisar los 10 `Multicomponente`, que sugieren
`mrp.bom`.

> **Gotcha verificado.** Las búsquedas de deduplicación necesitan
> `context={'active_test': False}`. Odoo excluye los archivados de toda búsqueda,
> así que sin eso el propio mapeo de estados de arriba crearía **duplicados**: al
> archivar un producto, la pasada siguiente no lo encontraría y lo daría por nuevo.

---

## 5. Órdenes → `sale.order` — nivel comercial

Sin cambios respecto a la v1, salvo §11 (no hacen falta campos `x_smartier_*`
para la clave externa; se usa `client_order_ref` o `origin`).

**Bloqueado:** `/external/ordenes` devuelve **0 registros**.

## 5b / 6 / 8 / 9. Producción → módulo MRP

**No programable hoy:** `mrp` está sin instalar y sus modelos no existen. El
mapeo de la v1 sigue siendo válido como diseño, pero requiere decisión previa
(§14, pregunta 1). Además `/external/ordenes` y `/external/tickets` están vacíos,
así que tampoco hay datos para validarlo.

---

## 7. Estados a normalizar

Sin cambios: ningún estado de Smartier calza 1:1 con Odoo y sigue pendiente de
decisión de negocio. **Nota [v2]:** el criterio ya adoptado para clientes y
productos —*solo el estado negativo explícito archiva; lo desconocido queda
visible*— conviene aplicarlo también aquí.

---

## 10. Notas de entrega → `account.move` (factura) **[v2 — cambia el destino]**

La v1 mapeaba a `stock.picking`. **El flujo implementado va a `account.move`**,
porque el objetivo del proyecto es facturar, no mover inventario (y con productos
`service` no hay stock que mover).

| Campo Smartier | Registro middleware | Campo Odoo |
|---|---|---|
| `Id` | `factura_id` = `NE-<id>` | idempotencia en `sync_map` |
| `Orden.Cliente.Documento.Contenido` | `cliente_nif` | `partner_id` (resuelto por `vat`) |
| `FechaEntregaReal` / `FechaEntrega` | `fecha` | `invoice_date` |
| `Orden.Numero` + `Id` | `referencia` | `ref` |
| `PrecioUnitario.Moneda` | `moneda_iso` (`Nacional`→VES, `Extranjera`→USD) | `currency_id` |
| `Cantidad`, `PrecioUnitario.Monto`, `Descuento` | `lineas` | `invoice_line_ids` |
| `Estado` | filtro de ingesta | solo se encola si está en `SMARTIER_ESTADOS_FACTURAR` |

El IVA, la cuenta contable y la tasa de cambio **no se envían**: Odoo los resuelve
desde la ficha del producto y del cliente.

Si además se quiere reflejar el movimiento físico, el mapeo a `stock.picking` de
la v1 sigue en pie como trabajo adicional.

**Bloqueado:** `/external/notas-entrega` devuelve **0 registros**.

---

## 11. Idempotencia: `x_smartier_id` **no hace falta** **[v2 — sustituye a la v1]**

La v1 pedía crear `x_smartier_id` y `x_smartier_last_sync` en cada modelo, vía
Studio o módulo custom. **Ya está resuelto de otra forma:**

| Necesidad | Solución implementada |
|---|---|
| Clave externa | `res.partner.ref` / `product.product.default_code` = `SMARTIER-<id>` |
| Estado de sincronización | Tabla `sync_map` en la **base de control** del middleware |
| Fecha de última sincronización | `sync_map.actualizado` |
| Auditoría | Tabla `sync_log` (append-only) |

Ventajas de lo que ya existe:

- La trazabilidad vive **fuera de Odoo**: el panel funciona sin consultarlo.
- No hace falta Studio ni un módulo custom.
- Una sola fuente de verdad. Añadir `x_smartier_id` la duplicaría.

**Donde la v1 tiene razón:** `ref` y `default_code` son editables por un usuario
desde la interfaz de Odoo, y tocarlos rompe la deduplicación. Un campo indexado y
de solo lectura sería más robusto. Es una mejora legítima **como refuerzo**, no
como base — y no antes de que la localización esté instalada, para no acumular
campos custom sobre un modelo que va a cambiar.

---

## 12. Campos que aporta la localización **[v2 — sección nueva]**

Inventario de lo que `l10n_ve_full` v17.0.2.3.0 añade y que el mapeo necesita.
Extraído del código, no de documentación.

### 12.1 Factura — `account.move`

| Campo | Tipo | Para qué |
|---|---|---|
| **`nro_ctrl`** | Char | **Número de Control** — obligatorio en factura fiscal venezolana |
| `rif` | Char | RIF del cliente, copiado a la factura |
| `identification_id1`, `nationality1` | Char/Selection | Identidad fiscal en el documento |
| `people_type_company1` / `people_type_individual1` | Selection | Tipo de persona |
| `contribuyente_seniat` | Selection | Régimen del cliente |
| `wh_iva`, `wh_iva_id`, `rela_wh_iva` | Bool/M2o | Enlace al comprobante de retención de IVA |
| `islr_wh_doc_id` | Many2one | Enlace al comprobante de ISLR |
| `iva_number_asignado`, `islr_number_asignado` | Char | Números de comprobante asignados |
| `fb_id`, `issue_fb_id` | Many2one | Libro fiscal al que pertenece |
| `alicuota_line_ids` | One2many | Desglose por alícuota |
| `porc_reten_muni`, `monto_reten_muni` | Float/Monetary | Retención municipal |
| `date_document` | Date | Fecha del documento (distinta de la contable) |
| `supplier_invoice_number` | Char | Nº de factura del proveedor (compras) |

### 12.2 Retenciones — modelos propios

| Modelo | Qué es |
|---|---|
| `account.wh.iva` | **Comprobante de retención de IVA** (`number`, `date_ret`, `fortnight`, `amount_base_ret`, `total_tax_ret`, `withholding_rate`) |
| `account.wh.iva.line` / `.line.tax` | Líneas y desglose por impuesto |
| `account.wh.iva.txt` | **Archivo TXT para el portal del SENIAT** |
| `account.wh.islr.doc` | Comprobante de retención de ISLR |
| `account.wh.islr.concept` | Conceptos ISLR (`codigo`, `withholdable`, `rate_ids`) |
| `account.wh.islr.rates` | **Tarifas**: `base`, `minimum`, `wh_perc`, `subtract`, `residence`, `nature` |
| `account.wh.municipal.docs` / `.rates` | Retención municipal |

> **Esto es lo que la solución nativa NO puede replicar.** Ver §13.

### 12.3 Libros fiscales y numeración

| Modelo | Qué es |
|---|---|
| `account.fiscal.book` (+ `.line`, `.taxes`, `.taxes.summary`) | **Libros de compra y venta** (`period_start`, `period_end`, `fortnight`, `base_amount`, `tax_amount`) |
| `account.control.sequence.rule` | Reglas de **Número de Control** por diario y tipo de documento |
| `account.control.number.log` | Bitácora de números emitidos |
| `account.ut` | **Unidad Tributaria** (valor y vigencia) |
| `res.country.state.municipality` / `.parish` | Municipios y parroquias de Venezuela |

### 12.4 `account.tax`

| Campo | Para qué |
|---|---|
| `type_tax` | Distingue IVA de retención |
| `appl_type` | Tipo de aplicación |
| `wh_vat_collected_account_id` / `wh_vat_paid_account_id` | Cuentas contables de la retención |

---

## 13. Solución provisional sin la localización **[v2 — sección nueva]**

`l10n_ve_full` **no consigue instalarse**: cuatro intentos, todos con
`Test: Failed` / `KILLED` en Odoo.sh, en producción y en staging, sin logs
accesibles (el contenedor muere y con él las pestañas SHELL y LOGS).

Descartado con verificación: sintaxis Python de los 4 addons, XML mal formado,
ficheros del manifest ausentes, dependencias Odoo inexistentes (las 17 están en el
servidor) y librerías Python (`xlwt` y `XlsxWriter` **sí** vienen en los
requirements oficiales de Odoo 17).

También se descubrió que **los addons no son separables**: `l10n_ve_full`
referencia XMLIDs de `account_dual_currency` en `hooks.py` y en diez archivos más,
y la migración `17.0.2.3.0/pre-migrate.py` reparte registros de `ir_model_data`
entre ambos. Su propia documentación los llama *"cadena de dependencias"*.

Para no bloquear el proyecto, `scripts/preparar_fiscal_ve.py` cubre lo mínimo con
campos **nativos** de Odoo 17:

| | Nativo (hoy) | Localización |
|---|---|---|
| Moneda VES | ✅ activada | ✅ |
| IVA 16 % | ✅ `account.tax` id=3 | ✅ |
| Retención IVA 75 %/100 % | ✅ `account.tax` negativo | ✅ + comprobante |
| Retención ISLR 1-5 % | ✅ tramos fijos | ✅ + tarifas con sustraendo |
| Clasificar cliente por régimen | ✅ posición fiscal | ✅ `wh_iva_agent` + `%` |
| **Comprobante numerado SENIAT** | ❌ | ✅ `account.wh.iva` |
| **Archivo TXT SENIAT** | ❌ | ✅ `account.wh.iva.txt` |
| **Libros compra/venta** | ❌ | ✅ `account.fiscal.book` |
| **Número de Control** | ❌ | ✅ `nro_ctrl` |
| **Validación de RIF** | ❌ | ✅ |
| **Unidad Tributaria** | ❌ | ✅ `account.ut` |
| Retención municipal | ❌ | ✅ |

> **El detalle que costó una prueba fallida.** La retención de IVA es un porcentaje
> **del IVA**, pero un `account.tax` se aplica siempre sobre la **base imponible**.
> Hay que convertirlo: retener el 75 % del IVA del 16 % es un **12 % de la base**.
> Creándolo con `-75`, una factura de 1.000 Bs daba **410** en vez de 1.040 (Odoo
> restaba 750, el 75 % de la base, en lugar de 120).
>
> Verificado con factura real en VES: `1.000,00 + 160,00 − 120,00 = 1.040,00 Bs`.
>
> Contrapartida: el porcentaje queda atado a la alícuota del 16 %. Si el IVA cambia
> hay que recalcularlo — de ahí la constante `IVA_VIGENTE` y que el nombre del
> impuesto lo diga.

**Conclusión honesta:** esto sirve para **operar y que las cifras cuadren**, no
para cumplir del todo con la declaración ante el SENIAT. Cuando la localización
entre, hay que revisar que sus impuestos no dupliquen los creados aquí.

---

## 14. Preguntas abiertas

### Bloqueantes

1. **¿Se instala Fabricación (MRP)?** Verificado: `mrp` está sin instalar y sus
   modelos no existen. Sin esa decisión, §5b, §6, §8 y §9 no son programables.
2. **¿Por qué falla el build de `l10n_ve_full`?** Hace falta el log de Odoo.sh
   —vía soporte, que tiene partnership code, o vía el equipo de `sma_l10n_ve`—.
   Define si se sigue con la solución nativa o se completa el cumplimiento fiscal.
3. **¿El alcance es facturación o el ERP completo?** Cambia la estimación por
   entero. Hoy hay implementado un flujo; este documento mapea seis.

### Fiscales (decisión de negocio)

4. **¿Cuáles de los 3 clientes son agentes de retención de IVA, y a qué
   porcentaje (75 % o 100 %)?** Es designación del SENIAT, cliente por cliente.
   Smartier no lo sabe.
5. **¿Son personas naturales o jurídicas?** Smartier dice `Contacto` en los 3,
   pero los nombres sugieren empresas (`VENTASIMPRENTA@TURICOPYIMPRESOS.NET`).
   Cambia la alícuota de ISLR y el `people_type_*`.
6. **¿Qué concepto ISLR aplica a impresión/artes gráficas?** Determina el
   porcentaje y el sustraendo.
7. **¿Dónde vive el dato de "es agente de retención"?** Recomendación: **en Odoo,
   a mano**, por contabilidad. Son 3 clientes y el dato cambia poco. El middleware
   no lo toca (§2.3).

### De datos (dependen de Turicopy)

8. **Los RIF.** Los 3 clientes tienen `Documento.Contenido: null`. Sin RIF no hay
   factura fiscal válida. ¿Se cargan en Smartier (preferible) o en Odoo?
9. **Notas de entrega y órdenes.** Los tres endpoints de producción devuelven 0.
   Sin al menos una nota no se puede probar el flujo de punta a punta.
10. **Catálogo de códigos numéricos:** `Documento.Tipo` (hoy `6` en los 3),
    `Unidad` de ticket, `Tipo` de recurso. ¿Hay endpoint o tabla de referencia?

### De diseño (v1, siguen abiertas)

11. Mapeo estado-a-estado (§7).
12. ¿"Planta" en notas de entrega es un almacén físico (`stock.warehouse`)?
13. ¿Un Componente pertenece siempre a una única Orden?
14. `Accion.Tipo = Tarea/Servicio/Custom` sin `Accion.Id`: ¿`mrp.workorder` o
    `project.task`?

### Cerradas desde la v1

- ~~¿Vendedores en `res.users` o `res.partner`?~~ → `res.partner`, por licencias (§3).
- ~~¿Hacen falta campos `x_smartier_id`?~~ → No: `sync_map` + `ref`/`default_code` (§11).
- ~~Unidades de tiempo y plazo~~ → minutos y días, confirmado en la doc de modelos.

---

## 15. Riesgo operativo detectado

La API de Smartier **ignora en silencio los filtros que no reconoce** (devuelve
200 como si nada), mientras que sí valida `Sort` (devuelve 400 con la lista de
campos válidos). Es una asimetría peligrosa: un filtro mal escrito no falla, solo
devuelve **todo**.

Si el control de duplicados dependiera de un filtro del lado de Smartier
(p. ej. `TieneFactura`), un error tipográfico provocaría **refacturación masiva**.
Por eso la idempotencia vive en el `sync_map` del middleware y no en la consulta:
aunque la API devuelva de más, `sincronizar_entidad` consulta el estado antes de
tocar Odoo y no duplica.

---

## 16. Aportes del documento "Migración Smartier a Odoo 17" **[v2.1]**

Documento externo de 15 páginas, orientado a **carga inicial por importación**
(124 campos, orden de carga por bloques). Es complementario a este: aquel resuelve
*cómo migrar el histórico una vez*; este, *cómo sincronizar día a día por API*.
Sus hallazgos se han verificado contra el código y se incorporan aquí.

### 16.1 Verificado y confirmado

| Afirmación | Comprobación |
|---|---|
| `tax_today` congela la tasa en cada documento | ✅ `account_dual_currency/models/account_move.py:36` |
| El módulo dual **invierte** `rate` / `inverse_rate` | ✅ el propio código lo comenta: *"el override de `_compute_current_rate` intercambia `rate`/`inverse_rate`"* (`res_currency.py:369-371`) |
| `wh_iva_rate` arranca en 0, no en 75 | ✅ typo `dafault` — ya documentado en §2.2 |
| `is_iva_journal`, `is_islr_journal`, `default_iva_account`, `default_islr_account` | ✅ existen en `account.journal` |
| `type_tax` es **obligatorio** y no existe en Odoo estándar | ✅ `required=True` en `account_tax.py:27-29` |
| `nro_ctrl` lleva correlativo propio y se auditan los saltos | ✅ `account.control.number.log` + `account.control.sequence.rule` |

### 16.2 Corrección: el espejo `rif` → `vat` **no aplica por API**

El documento dice que `vat` *"el módulo lo espeja desde `rif` automáticamente,
no hace falta cargarlo aparte"*. **Cierto solo en la interfaz.** El espejo es un
`@api.onchange('rif')` (`res_partner.py:248-253`), y los `onchange` **no se
disparan** en un `create`/`write` por JSON-RPC.

Consecuencia para el middleware: hay que escribir **`vat` y `rif` explícitamente**,
los dos. Si solo se manda `rif`, `vat` queda vacío y la deduplicación por `vat`
—que es la llave primaria de nuestro mapeo (§2.1)— deja de encontrar al contacto.

Relacionado: las validaciones de formato y duplicado de RIF (`validate_rif_er`,
`validate_rif_duplicate`) están **definidas pero comentadas** en `create()` y
`write()`. Es decir, la localización **no valida el formato del RIF por API**.
Esa validación tiene que hacerla el middleware si se quiere.

### 16.3 Lo que este informe incorpora

**Campos que faltaban en nuestro mapeo** (tabla completa en §16.5):

- `account.journal`: `is_iva_journal` / `is_islr_journal` y sus cuentas. Sin
  marcarlos, los diarios **no se pueden seleccionar** en la ficha del contacto.
- `account.tax`: `type_tax` (obligatorio) y `appl_type` (`exento`, `sdcf`,
  `general`, `reducido`, `adicional`), del que dependen **las columnas del libro
  de compra/venta**.
- `account.move.line`: `concept_id` → `account.wh.islr.concept`, para el ISLR.
- `res.partner`: `property_account_receivable_id` / `property_account_payable_id`
  y `supplier_rank`.
- `account.move`: `tax_today` — **crítico**, ver abajo.

**Campos calculados que NUNCA se deben enviar.** Aviso valioso: mandarlos no
sirve de nada y puede dejar valores que no cuadren con lo que Odoo recalcula.

| Modelo | Campos |
|---|---|
| `account.move` | `amount_total`, `amount_untaxed`, `amount_tax`, sus variantes `_usd`, `amount_residual` |
| `account.move.line` | `debit_usd`, `credit_usd`, `balance_usd`, `price_subtotal`, `price_total`, `price_unit_usd` |
| `res.partner` | `total_due`, `total_overdue` |
| `account.move` | `wh_iva_id`, `islr_wh_doc_id` — se generan al procesar la retención dentro de Odoo |

Nuestro flujo ya cumple esto: `mappings.yaml` envía solo `invoice_line_ids` con
cantidad y precio, y deja que Odoo calcule los totales. La validación posterior
(`core/impuestos.py`) **compara** `amount_total` contra el total de origen; no lo
escribe.

### 16.4 `tax_today`: el riesgo que no teníamos identificado

> *"Cada factura guarda su propia tasa en `tax_today` al momento de crearse. Si se
> importan facturas viejas sin ese campo, todas toman la tasa del día de la
> importación y los montos en dólares quedan mal."*

Esto **también afecta al flujo diario**, no solo a la migración. Si una nota de
entrega de Smartier se factura con retraso (se encoló ayer, el poller la procesa
hoy), la factura tomaría la tasa de **hoy** en lugar de la de la fecha del
documento. Con la volatilidad del bolívar, la diferencia no es menor.

**Acción pendiente:** cuando `account_dual_currency` esté instalado, añadir
`tax_today` al mapeo de `factura` en `mappings.yaml`, alimentado desde la fecha
de la nota de entrega. Hoy no se puede: el campo no existe sin ese módulo.

### 16.5 Campos por bloque — complemento al §12

Consolidado de ambos documentos. **Negrita** = aporta el documento externo.

**`res.partner`** — además de §2.2:

| Campo | Pri. | Nota |
|---|---|---|
| **`property_account_receivable_id`** | IMP | Cuenta por cobrar; si se omite toma la del plan |
| **`property_account_payable_id`** | IMP | Cuenta por pagar |
| **`supplier_rank`** | IMP | `1` en proveedores (nosotros ya ponemos `customer_rank`) |
| **`property_payment_term_id`** | REC | Condiciones de pago |
| `identification_id` | OBL si es persona | Cédula/pasaporte |
| `vat` **y** `rif` | OBL | **Ambos**, ver §16.2 |

**`account.journal`** — bloque que no teníamos:

| Campo | Pri. | Nota |
|---|---|---|
| **`is_iva_journal`** | IMP | Sin esto el diario no aparece en el contacto |
| **`is_islr_journal`** | IMP | Ídem para ISLR |
| **`default_iva_account`** | IMP | Cuenta contable de la retención de IVA |
| **`default_islr_account`** | IMP | Cuenta contable de la retención de ISLR |

**`account.tax`** — además de §12.4:

| Campo | Pri. | Nota |
|---|---|---|
| **`type_tax`** | **OBL** | `iva` o `municipal`. `required=True`, no existe en Odoo estándar |
| **`appl_type`** | IMP | `exento`/`sdcf`/`general`/`reducido`/`adicional` — define las columnas del libro |
| **`country_id`** | OBL | Venezuela |
| **`invoice_repartition_line_ids`** | IMP | Reparto contable; conviene copiar de un impuesto existente |

**`account.move`** (factura) — además de §12.1:

| Campo | Pri. | Nota |
|---|---|---|
| **`tax_today`** | IMP | Tasa **del día de la factura**, no de hoy (§16.4) |
| **`invoice_date_due`** | IMP | Vencimiento, para la cobranza |
| `nro_ctrl` | IMP | Número de Control, correlativo propio auditado |
| **`state`** | REC | Cargar en `draft` y publicar en lote tras cuadrar |

**`account.move.line`**:

| Campo | Pri. | Nota |
|---|---|---|
| **`concept_id`** | REC | `account.wh.islr.concept`, solo si hay retención ISLR |
| **`tax_ids`** | IMP | Sin esto no hay IVA y los libros salen incompletos |

### 16.6 Lo que ese documento asume y aquí no aplica

Está escrito para **importación por CSV desde la interfaz de Odoo**; nuestro flujo
es **JSON-RPC continuo**. Dos consecuencias:

1. **"El ID externo es la red de seguridad"** — propone `smartier.cliente_1234` en
   la columna de ID externo (`ir.model.data`). Es el equivalente, para importación
   CSV, de lo que nosotros resolvemos con `sync_map` (§11). **No hacen falta las
   dos cosas**; nuestra vía además sobrevive a que alguien edite el campo en Odoo.

2. **"Importar en borrador y publicar después"** — sensato para una carga masiva
   que hay que cuadrar contra el sistema anterior. En el flujo diario,
   `crear_factura` crea y postea en la misma operación, con validación de totales
   (`core/impuestos.py`) y rollback lógico si el posteo falla. Son estrategias
   distintas para problemas distintos; ninguna sustituye a la otra.

### 16.7 Orden de carga (si se hace migración histórica)

Del documento externo, y es correcto. Solo tiene sentido si además del flujo
diario se decide migrar el histórico contable:

```
1. Plan de cuentas   →  2. Contactos    →  3. Diarios
4. Impuestos         →  5. Tasas        →  6. Productos
7. Asiento apertura  →  8. Facturas     →  9. Pagos  →  10. Conciliación
```

> **Verificación final:** el balance de comprobación de Odoo debe coincidir con el
> de Smartier a la fecha de corte, cuenta por cuenta, **antes** de publicar.

**Decisión pendiente:** ¿se migra el histórico contable, o el proyecto arranca
desde cero en Odoo con solo el flujo diario? Hoy hay **0 facturas** en la
instancia. Cambia el alcance por completo y no está definido (§14, pregunta 3).
