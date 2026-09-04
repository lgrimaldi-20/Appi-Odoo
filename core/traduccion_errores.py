"""
Traduccion al castellano de los errores que devuelve Odoo.

Por que existe: los mensajes propios del middleware ya estan en castellano,
pero cuando Odoo rechaza una operacion su texto llega tal cual, en el idioma
del servidor, y acaba en la columna de observaciones del panel. Quien vigila
el panel es personal de administracion, no quien programo esto.

Ademas de traducir, se AMPLIA el mensaje cuando el original es enganoso. El
caso que motivo el modulo:

    "Missing required account on accountable line"

no menciona al contacto, que es justo donde esta el problema: el cliente no
tiene cuenta por cobrar. Traducirlo literalmente conservaria el desconcierto;
lo util es decir que revisar.

Criterio de diseno: si un mensaje no esta en la tabla se devuelve INTACTO. Es
preferible una frase en ingles a una traduccion aproximada que despiste, y asi
un cambio de texto en una version de Odoo nunca rompe nada.
"""

import re

# ---------------------------------------------------------------------------
# Cada entrada: (patron a buscar en el texto de Odoo, mensaje en castellano).
#
# Se comparan en minusculas y sin acentos del original, porque Odoo formatea
# el mismo error de maneras ligeramente distintas segun la version y el modulo
# que lo lanza. Por eso son fragmentos y no textos completos.
# ---------------------------------------------------------------------------
TRADUCCIONES: tuple[tuple[str, str], ...] = (
    (
        "missing required account on accountable line",
        "Falta la cuenta contable. Suele ser que el cliente no tiene cuenta "
        "por cobrar asignada, o que la compania no tiene definida la cuenta "
        "por cobrar por defecto.",
    ),
    (
        "without a payments/receipts account set",
        "El diario no tiene cuenta de cobros/pagos pendientes configurada. "
        "Revisa el metodo de pago del diario en Contabilidad.",
    ),
    (
        "you cannot create a new payment without an outstanding",
        "El diario no tiene cuenta de cobros/pagos pendientes configurada. "
        "Revisa el metodo de pago del diario en Contabilidad.",
    ),
    (
        "no journal could be found",
        "No se encontro un diario adecuado para la operacion.",
    ),
    (
        # Especifico a proposito: este error lo lanza la localizacion cuando un
        # producto no lleva precio en dolares. Traducirlo como "el importe debe
        # ser mayor que cero" mandaria a revisar la factura, que no es donde
        # esta el problema.
        "'usd list price' field cannot be less than or equal to 0",
        "El producto no tiene precio en dolares (Precio USD). Smartier no "
        "publica precios de catalogo, asi que hay que fijarlo en Odoo.",
    ),
    (
        "cannot be less than or equal to 0",
        "El valor debe ser mayor que cero.",
    ),
    (
        "this record does not exist",
        "El registro ya no existe en Odoo (puede haberse borrado).",
    ),
    (
        "access denied",
        "Acceso denegado: el usuario de integracion no tiene permiso para "
        "esta operacion.",
    ),
    (
        "you are not allowed to modify",
        "El usuario de integracion no tiene permiso para modificar este "
        "registro.",
    ),
    (
        "the operation cannot be completed",
        "Odoo rechazo la operacion.",
    ),
)

# Prefijos de ruido que Odoo antepone y no aportan nada a quien lee el panel.
_RUIDO = (
    "The operation cannot be completed: ",
    "The operation cannot be completed, ",
)


def traducir(mensaje: str) -> str:
    """
    Devuelve el mensaje en castellano si se reconoce; si no, el original.

    Nunca lanza: se usa al registrar un error, y fallar aqui taparia el error
    de verdad, que es lo unico que no puede perderse.
    """
    if not mensaje:
        return mensaje
    try:
        texto = str(mensaje).strip()
        comparable = texto.lower()

        for patron, castellano in TRADUCCIONES:
            if patron in comparable:
                # El detalle original se conserva entre parentesis: sin el no
                # se podria buscar el error en la documentacion de Odoo ni
                # reportarlo a soporte.
                limpio = _sin_ruido(texto)
                if limpio and limpio.lower() != patron:
                    return f"{castellano} [Odoo: {limpio}]"
                return castellano
        return texto
    except Exception:  # noqa: BLE001 - nunca debe romper el registro del error
        return str(mensaje)


def _sin_ruido(texto: str) -> str:
    """Quita los prefijos genericos de Odoo y colapsa espacios y saltos."""
    limpio = texto
    for prefijo in _RUIDO:
        if limpio.startswith(prefijo):
            limpio = limpio[len(prefijo):]
    return re.sub(r"\s+", " ", limpio).strip()
