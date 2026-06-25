"""Modelo de estado y único escritor (``StateManager``).

Define el perfil del deudor como dataclasses + enums, y un ``StateManager`` que
es la **única** ruta de escritura del perfil. Cada mutación:

1. registra un ``EventoHistorial`` (valor anterior, nuevo, origen y turno), y
2. reescribe el JSON del perfil en disco de inmediato.

Esa combinación (historial + reescritura por turno) es lo que hace *demostrable*
que el perfil se actualiza "al vuelo" y no por extracción al final.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional


# --- Enums -----------------------------------------------------------------
class DisposicionPago(str, Enum):
    """Disposición / voluntad de pago percibida del deudor."""

    DESCONOCIDA = "DESCONOCIDA"
    ALTA = "ALTA"
    MEDIA = "MEDIA"
    BAJA = "BAJA"
    RENUENTE = "RENUENTE"


class EstadoGestion(str, Enum):
    """Resultado/estado de la gestión de cobranza."""

    EN_CURSO = "EN_CURSO"
    COMPROMISO_DE_PAGO = "COMPROMISO_DE_PAGO"
    SIN_ACUERDO = "SIN_ACUERDO"
    IDENTIDAD_NO_VALIDADA = "IDENTIDAD_NO_VALIDADA"
    ABANDONADA = "ABANDONADA"


class TipoObjecion(str, Enum):
    """Categorías de objeción típicas en cobranza."""

    SIN_LIQUIDEZ = "SIN_LIQUIDEZ"
    NO_RECONOCE_DEUDA = "NO_RECONOCE_DEUDA"
    YA_PAGO = "YA_PAGO"
    PIDE_TIEMPO = "PIDE_TIEMPO"
    DISPUTA_MONTO = "DISPUTA_MONTO"
    OTRO = "OTRO"


def _now_iso() -> str:
    """Timestamp ISO-8601 en UTC."""
    return datetime.now(timezone.utc).isoformat()


# --- Dataclasses -----------------------------------------------------------
@dataclass
class DebtSnapshot:
    """Foto de la deuda devuelta por el backend."""

    saldo: int
    dias_mora: int
    producto: str
    fecha_corte: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Objecion:
    """Una objeción planteada por el deudor durante la gestión."""

    tipo: TipoObjecion
    detalle: str
    ts: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {"tipo": self.tipo.value, "detalle": self.detalle, "ts": self.ts}


@dataclass
class Compromiso:
    """Compromiso de pago acordado (monto + fecha), sellado con la referencia."""

    monto: int
    fecha: str
    referencia_proceso: str
    ts: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EventoHistorial:
    """Evento de auditoría: un cambio puntual en el perfil."""

    ts: str
    turno: int
    campo: str
    valor_anterior: Any
    valor_nuevo: Any
    origen: str  # herramienta/turno que causó el cambio

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DebtorProfile:
    """Estado completo de la gestión sobre un deudor."""

    documento: str
    nombre: Optional[str] = None
    identidad_validada: bool = False
    deuda: Optional[DebtSnapshot] = None
    disposicion_pago: DisposicionPago = DisposicionPago.DESCONOCIDA
    objeciones: list[Objecion] = field(default_factory=list)
    compromiso: Optional[Compromiso] = None
    estado_gestion: EstadoGestion = EstadoGestion.EN_CURSO
    historial: list[EventoHistorial] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serializa el perfil a un dict JSON-serializable."""
        return {
            "documento": self.documento,
            "nombre": self.nombre,
            "identidad_validada": self.identidad_validada,
            "deuda": self.deuda.to_dict() if self.deuda else None,
            "disposicion_pago": self.disposicion_pago.value,
            "objeciones": [o.to_dict() for o in self.objeciones],
            "compromiso": self.compromiso.to_dict() if self.compromiso else None,
            "estado_gestion": self.estado_gestion.value,
            "historial": [e.to_dict() for e in self.historial],
        }


# --- StateManager: único escritor -----------------------------------------
class StateManager:
    """Único punto de escritura del perfil + persistencia + historial.

    Ninguna otra capa muta el perfil directamente. Las herramientas invocan
    métodos de intención (``marcar_identidad_validada``, ``set_deuda``, ...) y
    este gestor se encarga de auditar y persistir cada cambio.
    """

    def __init__(self, documento: str, snapshot_dir: Path) -> None:
        self.profile = DebtorProfile(documento=documento)
        self._snapshot_dir = snapshot_dir
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._path = self._snapshot_dir / f"perfil_{documento}.json"
        # Turno conversacional actual; lo fija main.py en cada entrada del
        # usuario para que el historial sea legible ("turno 2 → ...").
        self.turno_actual: int = 0
        # Persistimos el estado inicial para que el archivo exista desde ya.
        self._persistir()

    # -- auditoría + persistencia internas ---------------------------------
    def _registrar(
        self, campo: str, anterior: Any, nuevo: Any, origen: str
    ) -> None:
        """Agrega un evento al historial y reescribe el JSON en disco."""
        self.profile.historial.append(
            EventoHistorial(
                ts=_now_iso(),
                turno=self.turno_actual,
                campo=campo,
                valor_anterior=anterior,
                valor_nuevo=nuevo,
                origen=origen,
            )
        )
        self._persistir()

    def _persistir(self) -> None:
        """Reescribe el perfil completo a ``perfil_<documento>.json``."""
        tmp = self._path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(self.profile.to_dict(), fh, ensure_ascii=False, indent=2)
        tmp.replace(self._path)  # escritura atómica

    @property
    def snapshot_path(self) -> Path:
        return self._path

    # -- métodos de intención (los llaman las herramientas) ----------------
    def marcar_identidad_validada(self, nombre: str, origen: str) -> None:
        """Marca la identidad como validada y fija el nombre confirmado."""
        self._registrar(
            "identidad_validada", self.profile.identidad_validada, True, origen
        )
        self.profile.identidad_validada = True
        if self.profile.nombre != nombre:
            self._registrar("nombre", self.profile.nombre, nombre, origen)
            self.profile.nombre = nombre

    def set_deuda(self, deuda: DebtSnapshot, origen: str) -> None:
        """Cachea la foto de la deuda en el perfil."""
        anterior = self.profile.deuda.to_dict() if self.profile.deuda else None
        self.profile.deuda = deuda
        self._registrar("deuda", anterior, deuda.to_dict(), origen)

    def agregar_objecion(self, objecion: Objecion, origen: str) -> None:
        """Agrega una objeción a la lista del perfil."""
        self.profile.objeciones.append(objecion)
        self._registrar(
            "objeciones", None, objecion.to_dict(), origen
        )

    def set_disposicion(self, nivel: DisposicionPago, origen: str) -> None:
        """Actualiza la disposición de pago percibida."""
        anterior = self.profile.disposicion_pago.value
        self.profile.disposicion_pago = nivel
        self._registrar("disposicion_pago", anterior, nivel.value, origen)

    def set_compromiso(self, compromiso: Compromiso, origen: str) -> None:
        """Registra el compromiso de pago acordado."""
        anterior = (
            self.profile.compromiso.to_dict() if self.profile.compromiso else None
        )
        self.profile.compromiso = compromiso
        self._registrar("compromiso", anterior, compromiso.to_dict(), origen)

    def set_estado(self, estado: EstadoGestion, origen: str) -> None:
        """Fija el estado/resultado de la gestión."""
        anterior = self.profile.estado_gestion.value
        self.profile.estado_gestion = estado
        self._registrar("estado_gestion", anterior, estado.value, origen)
