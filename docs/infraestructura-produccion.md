# Infraestructura de producción — dónde y cómo se despliega

Estado a 2026-09-07. Complementa a [paso-a-produccion.md](paso-a-produccion.md):
aquel documento explica **qué cambia** en la configuración; este explica **dónde
va a correr** y qué hay que montar.

---

## 1. El punto de partida: Docker no se sustituye

Hoy la pila corre en la PC de desarrollo con `docker compose up`. En producción
son **los mismos cuatro contenedores y la misma imagen** — lo único que cambia
es la máquina donde viven.

Lo que hoy no funciona es evidente en cuanto se dice: **si se apaga la PC, se
apaga el flujo**. Beat deja de encolar, el poller deja de sondear y las notas
que Smartier publique en ese rato no las lee nadie.

Los datos no se pierden (quedan en disco, y la marca de agua hace que al
reanudar se lea desde donde se quedó), pero **no avanza nada mientras está
apagado**. Un flujo automático necesita una máquina que no se apague.

---

## 2. Dónde alojarlo

### Opción recomendada: EC2 con Docker Compose

Una máquina virtual encendida permanentemente. Se instala Docker, se clona el
repositorio y se levanta la pila — es literalmente lo mismo que ya se probó en
local, sin traducir nada.

Razones concretas por las que es la opción sensata aquí:

- **Es lo ya probado.** Cero riesgo de que algo se comporte distinto.
- **El RDS va en la misma cuenta y región:** latencia mínima, y el tráfico no
  sale a internet.
- **Cuando algo falle, se entra por SSH y se miran los logs.** En plataformas
  gestionadas eso es bastante más incómodo.

Una `t3.small` sobra para este volumen (~15-20 USD/mes).

### Descartadas, y por qué

| Opción | Por qué no |
|---|---|
| **ECS Fargate** | Más caro y más configuración, a cambio de escalar — que aquí no hace falta: el poller es un reloj, no escala |
| **App Runner / Beanstalk** | Asumen que todo servicio atiende HTTP. `beat` y `worker` no lo hacen |

> Lo que este sistema necesita no es escalar, es **no apagarse nunca**. Eso una
> VM lo hace perfectamente.

---

## 3. La forma que tendría

```
EC2 (siempre encendida)
├── web ──── Nginx / HTTPS ──── panel y API
├── worker
├── beat                        ← UNA sola instancia
└── redis                       ← solo interno, sin puerto publicado
         │
         └──> RDS PostgreSQL (misma región)
              ├── sync_map / sync_log      (control)
              └── cola_sincronizacion      (notas + payload_original)
```

Coste aproximado: **35-40 USD/mes** entre EC2 y un RDS pequeño.

**Montar EC2 y RDS en el mismo movimiento.** Están acoplados: la base debe estar
en la misma región que la máquina, y separarlos obliga a configurar la red dos
veces.

---

## 4. Lo que hay que cambiar en el `docker-compose`

### 4.1 Reinicio automático — lo que de verdad da las 24 horas

```yaml
restart: unless-stopped    # en los CUATRO servicios (hoy solo lo tiene redis)
```

Más, en el servidor:

```bash
sudo systemctl enable docker
```

Sin estas dos cosas, un reinicio del servidor deja el flujo parado. Y ahí nadie
se entera, porque nadie apagó nada a propósito — es peor que en la PC, donde al
menos se sabe que se apagó.

### 4.2 Redis sin puerto expuesto

Hoy el compose publica el `6379`. En un servidor con IP pública eso es un Redis
**abierto a internet**: cualquiera puede leer la cola de tareas o inyectar
trabajos. Solo lo necesitan los contenedores entre sí.

```yaml
redis:
  image: redis:7-alpine
  restart: unless-stopped
  # ports: ELIMINADO — se comunica por la red interna de compose
```

### 4.3 Sin volúmenes SQLite

Al pasar a PostgreSQL, los tres bloques `volumes` desaparecen y las dos URL
apuntan al RDS. Es el cambio más visible del fichero.

### 4.4 Los secretos no viajan al servidor

Hoy `env_file: .env` toma un fichero del escritorio con la clave de Odoo, la de
Smartier y la `API_KEY`. En producción esos valores van en el gestor de secretos
del proveedor (AWS Secrets Manager o Parameter Store), **no en un fichero junto
al código**.

### 4.5 Usuario no-root en el Dockerfile

Los contenedores corren hoy como **root** (verificado). Si alguien entra por la
API, entra como root. Es una línea en el Dockerfile y reduce mucho el daño de
cualquier fallo:

```dockerfile
RUN useradd -m app && chown -R app /app
USER app
```

### 4.6 Healthchecks

No hay ninguno. Sin ellos, un servicio vivo pero colgado no lo detecta nadie:
el contenedor figura "arriba" mientras no procesa nada.

---

## 5. HTTPS: no es opcional

`uvicorn` sirve **HTTP plano**. Sin TLS, la `API_KEY` viaja en claro en cada
petición — incluida la del panel, que la envía en `X-Api-Key` en cada refresco.

Nginx delante, con certificado (Let's Encrypt sirve). El panel y la API quedan
detrás; los contenedores no se exponen directamente.

---

## 6. Orden de despliegue

1. Crear **RDS PostgreSQL** y **EC2** en la misma región
2. Instalar Docker en la EC2 + `systemctl enable docker`
3. `git clone` del repositorio
4. Cargar los secretos desde el gestor del proveedor
5. `docker-compose.prod.yml` con los cambios de la sección 4
6. `docker compose -f docker-compose.prod.yml up -d --build`
7. Nginx + certificado HTTPS
8. Verificar: `/health`, el panel, y que Beat encola (`docker compose logs beat`)

> **Mantener `docker-compose.prod.yml` aparte del actual.** Mezclar ambas
> configuraciones en un solo fichero termina en un despliegue donde nadie sabe
> si está en modo prueba o real.

---

## 7. Antes de encender: lo que no depende de la infraestructura

Esto es lo que de verdad frena el arranque, y ningún servidor lo resuelve:

- [ ] **RIF de los clientes** — sin esto el sistema estaría encendido 24 horas
      sin poder facturar nada
- [ ] Plan contable venezolano oficial (el actual son 7 cuentas de prueba)
- [ ] Revisar `wh_iva_agent` / `islr_withholding_agent` de la compañía
- [ ] Usuario de Odoo dedicado, no `admin`
- [ ] Claves nuevas: la de Smartier se compartió por chat durante el desarrollo
- [ ] Probar cobro, conciliación y una factura en USD

**Los RIF van primero.** Sin ellos el flujo automático funciona perfectamente y
no factura nada.
