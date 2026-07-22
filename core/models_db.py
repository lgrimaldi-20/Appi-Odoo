"""
Modelos de la base de datos de CONTROL del middleware.

Esta base de datos es propia del middleware (NO es Odoo). Guarda la correlacion
entre los IDs de la base de datos de origen y los IDs asignados por Odoo, mas el
estado de cada sincronizacion. Es el cimiento de la idempotencia, los reintentos
y la auditoria.

Dos tablas:
  - sync_map: correlacion (entidad, id_origen) <-> (model_odoo, id_odoo) + estado.
  - sync_log: bitacora append-only de cada accion realizada.
"""

from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class EstadoSync(str, Enum):
    """Estados posibles de una sincronizacion en sync_map."""
    PENDIENTE = "PENDIENTE"
    PROCESANDO = "PROCESANDO"
    PROCESADO = "PROCESADO"
    ERROR = "ERROR"


def _ahora() -> datetime:
    """Timestamp UTC (evita ambiguedades de zona horaria en la DB de control)."""
    return datetime.now(timezone.utc)


class SyncMap(Base):
    """
    Correlacion entre un registro de la DB de origen y su equivalente en Odoo.

    La restriccion unica (entidad, id_origen) es lo que garantiza idempotencia:
    un mismo registro de origen no puede mapearse dos veces.
    """
    __tablename__ = "sync_map"

    id = Column(Integer, primary_key=True, autoincrement=True)
    entidad = Column(String(50), nullable=False)          # p.ej. "factura", "pago"
    id_origen = Column(String(100), nullable=False)        # ID en la DB de origen
    model_odoo = Column(String(100), nullable=True)        # p.ej. "account.move"
    id_odoo = Column(Integer, nullable=True)               # ID asignado por Odoo
    estado = Column(String(20), nullable=False, default=EstadoSync.PENDIENTE.value)
    hash_payload = Column(String(64), nullable=True)       # sha256 del payload origen
    error = Column(Text, nullable=True)                    # ultimo error, si estado=ERROR
    creado = Column(DateTime(timezone=True), default=_ahora, nullable=False)
    actualizado = Column(
        DateTime(timezone=True), default=_ahora, onupdate=_ahora, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("entidad", "id_origen", name="uq_entidad_id_origen"),
        Index("ix_sync_map_estado", "estado"),
    )

    def __repr__(self) -> str:
        return (
            f"<SyncMap {self.entidad}/{self.id_origen} "
            f"-> {self.model_odoo}#{self.id_odoo} [{self.estado}]>"
        )


class SyncLog(Base):
    """
    Bitacora append-only. Una fila por cada accion relevante (crear, postear,
    conciliar, error...). Sirve para auditoria e intervencion humana.
    """
    __tablename__ = "sync_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    entidad = Column(String(50), nullable=False)
    id_origen = Column(String(100), nullable=True)
    accion = Column(String(50), nullable=False)            # p.ej. "crear", "postear"
    resultado = Column(String(20), nullable=False)         # "OK" | "ERROR"
    detalle = Column(Text, nullable=True)                  # mensaje o error
    timestamp = Column(DateTime(timezone=True), default=_ahora, nullable=False)

    __table_args__ = (
        Index("ix_sync_log_entidad_origen", "entidad", "id_origen"),
    )

    def __repr__(self) -> str:
        return f"<SyncLog {self.entidad}/{self.id_origen} {self.accion}={self.resultado}>"
