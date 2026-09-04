"""
Acceso a la base de datos de ORIGEN del cliente (modo polling).

En este modo el cliente NO llama a la API del middleware: deja sus registros en
SU propia base de datos (p.ej. un RDS PostgreSQL de su cuenta AWS) y el
middleware la sondea. El cliente es dueno de sus datos; a este modulo solo se le
concede lectura sobre la cola y escritura sobre las columnas de resultado
(estado / error / procesado_en).

Contrato de tabla acordado con el cliente (una sola tabla generica):

    CREATE TABLE cola_sincronizacion (
        id             BIGSERIAL PRIMARY KEY,
        entidad        VARCHAR   NOT NULL,     -- clave en mappings.yaml (factura, pago...)
        id_origen      VARCHAR   NOT NULL,     -- id de negocio (idempotencia)
        payload        JSONB     NOT NULL,     -- registro en el formato de mappings.yaml
        estado         VARCHAR   NOT NULL DEFAULT 'PENDIENTE',  -- PENDIENTE|PROCESADO|ERROR
        error_detalle  TEXT,
        creado_en      TIMESTAMP NOT NULL DEFAULT now(),
        procesado_en   TIMESTAMP
    );

La verdad de la idempotencia sigue siendo el sync_map del middleware; esta tabla
es solo la BANDEJA DE ENTRADA (que hay que enviar) y el ESPEJO DEL RESULTADO
(que el cliente lee sin llamarnos). Un registro en 'PENDIENTE' aqui pero ya
PROCESADO en sync_map no se duplica: sincronizar_entidad lo detecta.

La conexion se toma de SOURCE_DATABASE_URL (independiente de DATABASE_URL, que
es la base de CONTROL del middleware).
"""

import os
from contextlib import contextmanager
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import Session, declarative_base, sessionmaker

# JSON portable: JSONB en PostgreSQL, JSON/TEXT en SQLite (para tests).
from sqlalchemy.types import JSON

from core.models_db import _ahora
from core.traduccion_errores import traducir

# Base propia: estas tablas viven en la DB del CLIENTE, no en la de control.
SourceBase = declarative_base()


class ColaSincronizacion(SourceBase):
    """
    Fila de la bandeja de entrada del cliente. El middleware la lee y escribe de
    vuelta el resultado (estado/error/procesado_en) para que el cliente lo vea.
    """
    __tablename__ = "cola_sincronizacion"

    # BIGINT en PostgreSQL; INTEGER (autoincrement por ROWID) en SQLite (tests).
    id = Column(
        Integer().with_variant(BigInteger, "postgresql"),
        primary_key=True,
        autoincrement=True,
    )
    entidad = Column(String(50), nullable=False)        # clave en mappings.yaml
    id_origen = Column(String(100), nullable=False)      # id de negocio (idempotencia)
    payload = Column(JSON, nullable=False)               # registro para el mapper
    estado = Column(String(20), nullable=False, default="PENDIENTE")
    error_detalle = Column(Text, nullable=True)
    creado_en = Column(DateTime(timezone=True), default=_ahora, nullable=False)
    procesado_en = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<ColaSincronizacion {self.entidad}/{self.id_origen} "
            f"[{self.estado}] #{self.id}>"
        )


# ---------------------------------------------------------------------------
# Engine / Session (separado del engine de la base de CONTROL)
# ---------------------------------------------------------------------------

SOURCE_DATABASE_URL = os.getenv("SOURCE_DATABASE_URL", "")

# El engine se crea de forma perezosa: si no hay SOURCE_DATABASE_URL, el modo
# polling esta desactivado y no se toca ninguna conexion.
_engine = None
_SourceSession = None


def _init_engine():
    """Inicializa (una vez) el engine hacia la DB del cliente. Idempotente."""
    global _engine, _SourceSession
    if _engine is not None:
        return
    if not SOURCE_DATABASE_URL:
        raise RuntimeError(
            "Modo polling no configurado: define SOURCE_DATABASE_URL con la "
            "cadena de conexion a la base de datos del cliente."
        )
    connect_args = (
        {"check_same_thread": False}
        if SOURCE_DATABASE_URL.startswith("sqlite")
        else {}
    )
    _engine = create_engine(SOURCE_DATABASE_URL, connect_args=connect_args, future=True)
    _SourceSession = sessionmaker(
        bind=_engine, autoflush=False, expire_on_commit=False, future=True
    )


def polling_habilitado() -> bool:
    """True si hay una DB de origen configurada (modo polling activo)."""
    return bool(SOURCE_DATABASE_URL)


@contextmanager
def get_source_session() -> Session:
    """Context manager transaccional hacia la DB del cliente."""
    _init_engine()
    session = _SourceSession()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_source_db() -> None:
    """
    Crea la tabla de cola si no existe. Util para tests y para el arranque
    inicial en entornos donde el middleware provisiona el esquema; en produccion
    la tabla suele crearla el cliente.
    """
    _init_engine()
    SourceBase.metadata.create_all(bind=_engine)


# ---------------------------------------------------------------------------
# Lectura del lote pendiente
# ---------------------------------------------------------------------------

# SKIP LOCKED evita que dos workers cojan la misma fila; solo lo soportan
# PostgreSQL/MySQL. En SQLite (tests) se degrada a un SELECT normal.
def _soporta_skip_locked() -> bool:
    return not SOURCE_DATABASE_URL.startswith("sqlite")


def tomar_lote(limite: int = 50) -> list[dict]:
    """
    Lee hasta 'limite' filas PENDIENTE y las bloquea con FOR UPDATE SKIP LOCKED
    (donde el motor lo soporte), devolviendo una copia desacoplada de la sesion.

    Devuelve una lista de dicts {id, entidad, id_origen, payload} para procesar
    fuera de la transaccion de bloqueo. El resultado se escribe luego con
    marcar_resultado().
    """
    with get_source_session() as session:
        consulta = (
            session.query(ColaSincronizacion)
            .filter(ColaSincronizacion.estado == "PENDIENTE")
            .order_by(ColaSincronizacion.id)
            .limit(limite)
        )
        if _soporta_skip_locked():
            consulta = consulta.with_for_update(skip_locked=True)

        filas = consulta.all()
        # Copiamos los datos antes de cerrar la sesion (expire_on_commit=False
        # ya evita el detach, pero devolver dicts desacopla al llamador de la ORM).
        return [
            {
                "id": f.id,
                "entidad": f.entidad,
                "id_origen": f.id_origen,
                "payload": f.payload,
            }
            for f in filas
        ]


def marcar_resultado(
    fila_id: int,
    estado: str,
    error_detalle: Optional[str] = None,
) -> None:
    """
    Escribe el resultado de una fila de vuelta en la DB del cliente: estado
    (PROCESADO|ERROR), error si aplica y la marca de tiempo de procesado.
    Es el unico permiso de ESCRITURA que necesita el middleware en la DB origen.
    """
    with get_source_session() as session:
        fila = session.get(ColaSincronizacion, fila_id)
        if fila is None:
            # La fila desaparecio (el cliente la borro): nada que marcar.
            return
        fila.estado = estado
        # Traducido tambien aqui: esta columna la lee el cliente en SU base de
        # datos, sin pasar por el panel, y es la unica explicacion que recibe
        # de por que su registro no llego a Odoo.
        fila.error_detalle = traducir(error_detalle) if estado == "ERROR" else None
        fila.procesado_en = _ahora()
