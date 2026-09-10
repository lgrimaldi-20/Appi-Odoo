# Entornos por rama

Cada rama del middleware es un **cliente distinto**, con su instancia de Odoo y
su base de datos de control. El `.env` no viaja con la rama —está en
`.gitignore` a propósito, porque lleva credenciales—, así que después de un
`git switch` el `.env` seguía apuntando a la instancia anterior.

Ese desajuste no da ningún error: el middleware arranca igual y **escribe en el
Odoo del otro cliente**. De ahí que cambiar de rama y cambiar de `.env` se hagan
aquí en un solo paso, para que no puedan separarse.

## Uso

```powershell
.\entornos\cambiar.ps1 Turicopy-V17   # cambia de rama y activa su .env
.\entornos\cambiar.ps1 Fibex-V19
.\entornos\cambiar.ps1                # alinea el .env con la rama actual
```

Imprime siempre a qué instancia quedó apuntando. Es la comprobación que evita
el error caro y verla cuesta menos que ir a buscarla.

Si `git switch` falla (cambios sin guardar), **el `.env` no se toca**: es
preferible quedarse como estabas a tener el código de una rama contra el Odoo
de la otra.

## Ficheros

| Fichero | Rama | Instancia Odoo |
|---|---|---|
| `Turicopy-V17.env` | `Turicopy-V17` | `smartautomatai-pruebaturycopi` (Odoo 17) |
| `Fibex-V19.env` | `Fibex-V19` | `lgrimaldi-20-apifibexv19` (Odoo 19) |

El nombre del fichero **debe coincidir con el de la rama**: es así como el
script lo encuentra. Para añadir un cliente basta con dejar aquí su `.env` con
el nombre de su rama; no hay que tocar el script.

Los `.env` de esta carpeta **no se suben** (`entornos/*.env` en `.gitignore`);
el script y este README sí. Al clonar el repo hay que traerlos aparte.

## Después de cambiar

Reinicia `uvicorn`: `DATABASE_URL` y `SOURCE_DATABASE_URL` se leen **al
arrancar**. Si no, el panel sigue mostrando la base anterior mientras los
scripts escriben en la nueva.
