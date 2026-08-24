"""
Configuracion de Celery (Fase 5).

La cola desacopla la sincronizacion contable del ciclo request/response: los
endpoints encolan una tarea y responden 202, y un worker la procesa en segundo
plano con reintentos.

Modo EAGER automatico: si no hay CELERY_BROKER_URL configurado (p.ej. en tests o
en desarrollo sin Redis), las tareas se ejecutan de forma sincrona en el mismo
proceso (task.delay() corre inline). Asi el codigo es identico con y sin broker.
"""

import os

from celery import Celery

BROKER_URL = os.getenv("CELERY_BROKER_URL", "")
RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", BROKER_URL)

# Sin broker -> modo eager (ejecucion sincrona inline, util para tests/dev).
_EAGER = not BROKER_URL

celery_app = Celery(
    "api_odoo",
    broker=BROKER_URL or None,
    backend=RESULT_BACKEND or None,
)

celery_app.conf.update(
    task_always_eager=_EAGER,
    task_eager_propagates=_EAGER,   # en eager, las excepciones se propagan
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    task_acks_late=True,            # re-encola si el worker muere a mitad
)

# ---------------------------------------------------------------------------
# Celery Beat: poller periodico (modo pull desde la DB del cliente).
#
# Solo se programa si hay SOURCE_DATABASE_URL (modo polling activo); si no, no
# se registra ninguna tarea periodica. El intervalo (segundos) es configurable
# con POLLER_INTERVALO_SEG (por defecto 30). Requiere arrancar Celery Beat:
#   celery -A core.celery_app.celery_app beat --loglevel=info
# ---------------------------------------------------------------------------
if os.getenv("SOURCE_DATABASE_URL"):
    _POLLER_INTERVALO = float(os.getenv("POLLER_INTERVALO_SEG", "30"))
    _POLLER_TENANT = os.getenv("POLLER_TENANT", "default")
    _POLLER_LIMITE = int(os.getenv("POLLER_LIMITE", "50"))
    celery_app.conf.beat_schedule = {
        "poller-cola-cliente": {
            "task": "core.tasks.poller_task",
            "schedule": _POLLER_INTERVALO,
            "kwargs": {"tenant": _POLLER_TENANT, "limite": _POLLER_LIMITE},
        }
    }

# Ingesta periodica desde Smartier hacia la cola (mitad izquierda del flujo).
# Solo se programa si hay configuracion de Smartier; intervalo propio, porque
# leer la API externa suele espaciarse mas que vaciar la cola hacia Odoo.
if os.getenv("SMARTIER_BASE_URL") and os.getenv("SMARTIER_API_KEY"):
    _INGESTA_INTERVALO = float(os.getenv("SMARTIER_INTERVALO_SEG", "300"))
    _INGESTA_LIMITE = int(os.getenv("SMARTIER_LIMITE", "200"))
    schedule = dict(getattr(celery_app.conf, "beat_schedule", None) or {})
    schedule["ingesta-smartier"] = {
        "task": "core.tasks.ingesta_smartier_task",
        "schedule": _INGESTA_INTERVALO,
        "kwargs": {"limite": _INGESTA_LIMITE},
    }
    celery_app.conf.beat_schedule = schedule



def en_modo_eager() -> bool:
    """True si Celery corre inline (sin broker). Util para la respuesta HTTP."""
    return _EAGER
