# Respuesta de Smartier a nuestro informe — análisis y acciones

Fecha de la respuesta: **2026-09-09**. Documento recibido: *"API /external —
Consultas del equipo de integración y respuestas"*, seis consultas.

Todo lo que sigue se **verificó contra la API real** antes de darlo por bueno;
cuando algo se comprobó, se indica cómo.

---

## 1. Lo que nos respondieron, en corto

| # | Consulta | Su respuesta | Nos afecta |
|---|---|---|---|
| 1 | RIF obligatorio | Opcional en el alta; **no** puede volverse obligatorio por configuración. Existe un control que lo exige al **generar la orden**, activo por defecto | Sí — hay que confirmar su estado |
| 2 | Catálogo `Documento.Tipo` | **Tipo 6 = "Documento de identidad" genérico, no es un RIF.** RIF Venezolano = **30**. En nuestro tenant solo hay 4 tipos habilitados: 6, 7, 29, 30 | **Sí — bug en nuestro código** |
| 3 | Datos de retención | No hay campos personalizados por tenant. Las categorías CRM ni se exponen en `/external` | No — confirma lo que ya hacíamos |
| 4 | Contacto vs. Empresa | Selección manual; son **dos caminos de alta distintos**. El RIF solo se admite en clientes tipo **Empresa** | Sí — afecta a la deduplicación |
| 5 | Tenant sin datos | `/external/ordenes` no excluye nada; **`/external/notas-entrega` excluye siempre** las notas cuya orden esté en `EnEspera`, `Anulado` o sin estado | Sí — puede haber notas ocultas |
| 6 | Filtros | `Sort` inválido → 400 deliberado. **Un filtro no reconocido se descarta en silencio** (200 con el listado completo) | **Sí — bug en nuestro código** |

La respuesta es sólida: separan lo que es decisión de diseño de lo que es
comportamiento heredado de la plataforma, y reconocen las limitaciones de
producto sin rodeos.

---

## 2. Los dos bugs que esto destapó en nuestro código

Ambos **ya están corregidos** (commit `ad09f4a`, rama `Turicopy-V17`).

### 2.1 Enviábamos a Odoo como RIF el contenido de un documento genérico

`rif_de()` leía `Documento.Contenido` **sin mirar el `Tipo`**. Y el tipo 6 —el
único que traen hoy los clientes— es *"Documento de identidad" genérico*, cuya
validación es permisiva: acepta letras, dígitos y espacios.

Consecuencia: cualquier cosa escrita en ese campo habría acabado en el `vat` del
contacto y, de ahí, en el campo fiscal de la factura.

**Corregido:** solo cuentan el **29** (CI Venezolana) y el **30** (RIF
Venezolano). Un genérico con contenido se trata como ausente y el contacto queda
`PENDIENTE`, que es la situación real. `ingesta_smartier._extraer_nif` delega en
la misma función: con dos criterios distintos, la factura buscaría un partner por
un `vat` que el sincronizador de clientes nunca escribió.

### 2.2 El filtro de fecha de la ingesta no existe

Enviábamos `FechaDesde` como marca de agua — un nombre **supuesto**, y el propio
comentario del código lo admitía. Su consulta 6 explica que un filtro no
reconocido se descarta sin error, así que nunca se vio el fallo.

Verificado con la prueba que ellos mismos sugieren, comparar el `Count` con
filtro contra el `Count` sin filtro:

| consulta sobre `/external/clientes` | Count | |
|---|---|---|
| sin filtro | 4 | |
| `FechaDesde=2099-01-01` | 4 | **no filtra** |
| `FiltroInventado=xyz` | 4 | control: no filtra |
| `NombreContains=ZZZ` | 0 | control: **sí** filtra |
| `Sort=CampoInventado` | HTTP 400 | control: rechaza, como dicen |

Los dos controles confirman que el método es válido. Se probaron además 13
nombres alternativos (`CreadoDesde`, `CreadoUtcDesde`, `FechaMinima`…): ninguno
se aplica.

**Corregido:** `FILTRO_FECHA` queda **vacío** y configurable por
`SMARTIER_FILTRO_FECHA`, sin tocar código, cuando confirmen el nombre real.

> **Impacto mientras tanto:** la marca de agua se sigue guardando y el
> antiduplicado evita reprocesar, pero **cada pasada lee el histórico entero**.
> Hoy no se nota con 0 notas; con volumen real son miles de registros cada
> 5 minutos.

---

## 3. Lo que hay que corregir en nuestro informe (`mapeo-smartier-odoo-v2.md`)

| Línea | Dice | Debe decir |
|---|---|---|
| 88 | *"Falta el catálogo de códigos; hoy todos traen `6`"* | El catálogo ya lo tenemos: **6 = genérico, 30 = RIF Venezolano**. En nuestro tenant solo hay 4 tipos habilitados (6, 7, 29, 30) |
| 88 | `Documento.Tipo` → `nationality` (V/E/P) | Mapeo **incorrecto**: el tipo no es la nacionalidad, es la clase de documento. Sirve para **decidir si el contenido es identificación fiscal**, no para rellenar `nationality` |
| 87 | `Documento.Contenido` → `vat` **y** `rif` | Solo si `Tipo ∈ {29, 30}` |
| 403 | *"Catálogo de códigos numéricos: pendiente"* | **Resuelto.** Incluir la tabla de los 4 tipos habilitados |
| 399 | *"Los 3 clientes tienen `Documento.Contenido: null`"* | Son **4** clientes (llegó *Omer Rivas*, #9), los 4 sin RIF. Confirmado con `DocumentoContenidoContains=J-` → `Count=0` |

Añadir además dos apartados que el informe no cubre:

- **El RIF exige cliente tipo Empresa** y sus consecuencias (ver §4).
- **El filtro de fecha no existe**, con la tabla de verificación de §2.2.

---

## 4. El punto que más nos condiciona: RIF ⇄ tipo de cliente

Contacto y Empresa son **dos caminos de alta distintos** en Smartier, con
formularios propios. Y el tipo de cliente restringe qué documentos admite: el
**RIF Venezolano solo se puede asignar a empresas**.

Nuestros cuatro clientes están como `Contacto`. Es decir: al operador **no le
aparece la opción de RIF** — no es un descuido de captura, es que ese camino de
alta no lo permite.

**La consecuencia que hay que anticipar:** corregirlo no es editar un campo, es
dar de alta al cliente **otra vez** por el otro camino. Eso genera un `Id` nuevo
en Smartier y, por tanto, un `SMARTIER-<id>` nuevo — nuestra deduplicación
crearía un **contacto nuevo** en Odoo en vez de reutilizar el existente, y el
histórico quedaría partido en dos fichas.

Conviene decidir cómo se maneja **antes** de que empiecen a recrear clientes:
o se acepta la ficha nueva y se archiva la vieja, o se cargan los RIF en Odoo a
mano y se deja Smartier como está.

---

## 5. Qué responderles

**Confirmar recibido y agradecer** — la respuesta cerró dos huecos reales de
nuestro lado.

**Pedirles tres cosas concretas:**

1. **El nombre real del filtro de fecha**, si existe, para `/external/notas-entrega`
   y `/external/ordenes`. Verificamos que `FechaDesde` no se aplica y probamos 13
   alternativas sin acierto. Sin él, cada pasada lee el histórico completo.
   Si no existe ninguno, decirlo también: cambia cómo dimensionamos la ingesta.
2. **Que verifiquen el control de documento obligatorio** en nuestra instancia
   — se ofrecieron a hacerlo en la consulta 1.
3. **Confirmar si hay notas de entrega ocultas** por el estado de su orden
   (`EnEspera`, `Anulado` o sin estado). Antes de concluir que el tenant está
   vacío queremos descartar esa exclusión, tal como sugieren.

**Informarles de lo que corregimos:**

- Ya no tratamos el contenido de un documento genérico como RIF. Su aclaración
  del tipo 6 evitó que datos sin validar acabaran en el campo fiscal de facturas.
- Retiramos el filtro de fecha inventado.

**Un punto para Turicopy, no para Smartier:** los clientes empresa deben darse de
alta por el camino "Empresa" para poder llevar RIF, y hay que decidir qué se hace
con los cuatro ya creados como "Contacto".

---

## 6. Estado tras estos cambios

- Código corregido y verificado contra la API real — **234 tests en verde**
- Tres fixtures de test combinaban `Tipo: 6` con un RIF: reflejaban el supuesto
  equivocado, corregidas al tipo 30
- Sin cambios en el flujo automático: sigue funcionando igual
- **Los RIF siguen siendo el bloqueo principal.** Con todo automatizado, lo que
  llegue sin identificación fiscal seguirá parando en `PENDIENTE`
