import base64
import json
from datetime import UTC, datetime, timedelta

import grpc
import redis
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import DateTime, Integer, String, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from proto_generated import academics_pb2, academics_pb2_grpc
from proto_generated import attendance_pb2, attendance_pb2_grpc
from shared.app_common.auth import require_roles
from shared.app_common.config import BaseServiceSettings
from shared.app_common.database import Base, create_session_factory, session_scope
from shared.app_common.grpc_runtime import start_grpc_server
from shared.app_common.responses import ok
from shared.app_common.security import qr_png_base64, sign_qr_payload


class Settings(BaseServiceSettings):
    app_name: str = "AGM Asistencias QR"
    service_slug: str = "ms-attendance"
    rest_port: int = 8015
    grpc_port: int = 50055
    database_url: str = "postgresql+psycopg://agm:agm_dev_password@postgres:5432/agm_attendance_db"
    
    qr_secret: str = "tu_super_secreto_para_buap_agm_2026"
    academics_grpc_target: str = "ms-academics:50053"
    redis_url: str = "redis://redis:6379/0"


class AttendanceSession(Base):
    __tablename__ = "attendance_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    materia_id: Mapped[int] = mapped_column(Integer, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    closes_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30), default="abierta")


class AttendanceRecord(Base):
    __tablename__ = "attendance_records"
    __table_args__ = (UniqueConstraint("session_id", "student_id", name="uq_session_student"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(Integer, index=True)
    materia_id: Mapped[int] = mapped_column(Integer, index=True)
    student_id: Mapped[int] = mapped_column(Integer, index=True)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    estado: Mapped[str] = mapped_column(String(30))


class StartSessionPayload(BaseModel):
    materia_id: int


class RegisterAttendancePayload(BaseModel):
    token: str


def now_utc() -> datetime:
    return datetime.now(UTC)


def build_qr_token(secret: str, alumno_id: int, materia_id: int, session_id: int, issued_at: int) -> str:
    payload = f"{alumno_id}:{materia_id}:{session_id}:{issued_at}"
    signature = sign_qr_payload(secret, payload)
    raw = json.dumps({"p": payload, "s": signature}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("utf-8")


def decode_qr_token(secret: str, token: str) -> dict:
    try:
        raw = base64.urlsafe_b64decode(token.encode("utf-8")).decode("utf-8")
        data = json.loads(raw)
        payload = data["p"]
        signature = data["s"]
    except Exception as exc:
        raise HTTPException(status_code=400, detail="QR inválido") from exc
    if sign_qr_payload(secret, payload) != signature:
        raise HTTPException(status_code=400, detail="Firma inválida")
    alumno_id, materia_id, session_id, issued_at = payload.split(":")
    return {
        "alumno_id": int(alumno_id),
        "materia_id": int(materia_id),
        "session_id": int(session_id),
        "issued_at": int(issued_at),
    }


def get_students_map(target: str, materia_id: int) -> dict[int, academics_pb2.AlumnoInfo]:
    with grpc.insecure_channel(target) as channel:
        stub = academics_pb2_grpc.AcademicsServiceStub(channel)
        reply = stub.GetAlumnosByMateria(academics_pb2.MateriaIdRequest(materia_id=materia_id))
        return {item.alumno_id: item for item in reply.items}


class AttendanceGrpcService(attendance_pb2_grpc.AttendanceServiceServicer):
    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def GetAsistenciaAlumno(self, request, context):
        with session_scope(self.session_factory) as session:
            rows = session.scalars(
                select(AttendanceRecord).where(
                    AttendanceRecord.student_id == request.alumno_id,
                    AttendanceRecord.materia_id == request.materia_id,
                )
            ).all()
            return attendance_pb2.AsistenciaReply(
                items=[
                    attendance_pb2.AsistenciaItem(
                        sesion_id=row.session_id,
                        fecha=row.registered_at.isoformat(),
                        estado=row.estado,
                    )
                    for row in rows
                ]
            )

    def GetEstadisticasAsistencia(self, request, context):
        with session_scope(self.session_factory) as session:
            sessions = session.scalars(select(AttendanceSession).where(AttendanceSession.materia_id == request.materia_id)).all()
            records = session.scalars(select(AttendanceRecord).where(AttendanceRecord.materia_id == request.materia_id)).all()
            asistencias = len([row for row in records if row.estado == "Presente"])
            retardos = len([row for row in records if row.estado == "Retardo"])
            total_slots = max(len(sessions), 1)
            porcentaje = round(((asistencias + retardos) / total_slots) * 100, 2) if sessions else 0
            return attendance_pb2.StatsReply(
                total_sesiones=len(sessions),
                asistencias=asistencias,
                retardos=retardos,
                porcentaje=porcentaje,
            )

    def GetHistorialMateria(self, request, context):
        with session_scope(self.session_factory) as session:
            rows = session.scalars(select(AttendanceRecord).where(AttendanceRecord.materia_id == request.materia_id)).all()
            return attendance_pb2.MateriaAsistenciaReply(
                items=[
                    attendance_pb2.MateriaAsistenciaItem(
                        session_id=row.session_id,
                        student_id=row.student_id,
                        fecha=row.registered_at.isoformat(),
                        estado=row.estado,
                    )
                    for row in rows
                ]
            )


app = FastAPI(title="AGM Asistencias QR", version="0.1.0", root_path="/api/attendance")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup_event() -> None:
    settings = Settings()
    session_factory = create_session_factory(settings.database_url)
    Base.metadata.create_all(session_factory.kw["bind"])
    app.state.settings = settings
    app.state.session_factory = session_factory
    
    # NUEVO: Inicializar cliente Redis
    app.state.redis = redis.from_url(settings.redis_url, decode_responses=True)
    
    app.state.grpc_server, app.state.grpc_thread = start_grpc_server(
        settings.grpc_port,
        lambda server: attendance_pb2_grpc.add_AttendanceServiceServicer_to_server(
            AttendanceGrpcService(session_factory), server
        ),
    )


@app.on_event("shutdown")
def shutdown_event() -> None:
    grpc_server = getattr(app.state, "grpc_server", None)
    if grpc_server is not None:
        grpc_server.stop(grace=1)


@app.get("/health")
def health() -> dict:
    return ok({"service": "attendance", "status": "ok"})


@app.post("/sesiones/iniciar")
def start_session(payload: StartSessionPayload, request: Request, user=Depends(require_roles("admin", "docente"))) -> dict:
    session_factory = request.app.state.session_factory
    with session_scope(session_factory) as session:
        started = now_utc()
        attendance_session = AttendanceSession(
            materia_id=payload.materia_id,
            started_at=started,
            closes_at=started + timedelta(minutes=10),
            status="abierta",
        )
        session.add(attendance_session)
        session.flush()
        
        # NUEVO: Guardar en Redis con expiración de 10 minutos (600 segundos)
        request.app.state.redis.setex(f"session:{attendance_session.id}", 600, str(payload.materia_id))

        return ok(
            {
                "session_id": attendance_session.id,
                "materia_id": payload.materia_id,
                "started_at": attendance_session.started_at.isoformat(),
                "closes_at": attendance_session.closes_at.isoformat(),
            },
            "Sesión iniciada",
        )


@app.get("/qr/{materia_id}")
def generate_qr(
    materia_id: int,
    request: Request,
    session_id: int = Query(...),
    user=Depends(require_roles("alumno")),
) -> dict:
    # NUEVO: Validación en microsegundos directo desde la memoria caché
    active_materia = request.app.state.redis.get(f"session:{session_id}")
    if not active_materia or int(active_materia) != materia_id:
        raise HTTPException(status_code=400, detail="La sesión no existe o ya expiró")

    payload_time = int(now_utc().timestamp())
    token = build_qr_token(request.app.state.settings.qr_secret, user["profile_id"], materia_id, session_id, payload_time)
    return ok({"token": token, "qr_png_base64": qr_png_base64(token), "issued_at": payload_time})


@app.post("/asistencias/registrar")
def register_attendance(payload: RegisterAttendancePayload, request: Request, user=Depends(require_roles("admin", "docente"))) -> dict:
    data = decode_qr_token(request.app.state.settings.qr_secret, payload.token)
    
    # 1. Validar en Redis que la sesión siga viva
    active_materia = request.app.state.redis.get(f"session:{data['session_id']}")
    if not active_materia:
        raise HTTPException(status_code=400, detail="La sesión expiró o está cerrada")
        
    # 2. Control anti-duplicados ultrarrápido con Redis
    duplicate_key = f"attendance:{data['session_id']}:{data['alumno_id']}"
    if request.app.state.redis.get(duplicate_key):
        raise HTTPException(status_code=400, detail="El QR ya fue utilizado en esta sesión")

    students = get_students_map(request.app.state.settings.academics_grpc_target, data["materia_id"])
    if data["alumno_id"] not in students:
        raise HTTPException(status_code=400, detail="Alumno no inscrito en la materia")

    session_factory = request.app.state.session_factory
    with session_scope(session_factory) as session:
        attendance_session = session.get(AttendanceSession, data["session_id"])
        minutes = (now_utc() - attendance_session.started_at).total_seconds() / 60
        estado = "Presente" if minutes <= 5 else "Retardo"
        record = AttendanceRecord(
            session_id=data["session_id"],
            materia_id=data["materia_id"],
            student_id=data["alumno_id"],
            estado=estado,
        )
        session.add(record)
        
        # 3. Registrar el "candado" temporal en Redis para evitar doble escaneo
        request.app.state.redis.setex(duplicate_key, 600, "1")

        return ok({"estado": estado, "alumno_id": data["alumno_id"]}, "Asistencia registrada")


@app.delete("/sesiones/{session_id}/cerrar")
def close_session(session_id: int, request: Request, user=Depends(require_roles("admin", "docente"))) -> dict:
    # NUEVO: Borrar la sesión activa de la memoria inmediatamente
    request.app.state.redis.delete(f"session:{session_id}")
    
    session_factory = request.app.state.session_factory
    with session_scope(session_factory) as session:
        attendance_session = session.get(AttendanceSession, session_id)
        if attendance_session is not None:
            attendance_session.status = "cerrada"
    return ok(None, "Sesión cerrada")


@app.get("/asistencias/{materia_id}/hoy")
def attendance_today(materia_id: int, request: Request, user=Depends(require_roles("admin", "docente", "alumno"))) -> dict:
    session_factory = request.app.state.session_factory
    today = now_utc().date()
    with session_scope(session_factory) as session:
        rows = session.scalars(select(AttendanceRecord).where(AttendanceRecord.materia_id == materia_id)).all()
        result = [
            {
                "session_id": row.session_id,
                "student_id": row.student_id,
                "estado": row.estado,
                "registered_at": row.registered_at.isoformat(),
            }
            for row in rows
            if row.registered_at.date() == today
        ]
        return ok(result)


@app.get("/asistencias/{materia_id}/historial")
def attendance_history(materia_id: int, request: Request, user=Depends(require_roles("admin", "docente", "alumno"))) -> dict:
    session_factory = request.app.state.session_factory
    with session_scope(session_factory) as session:
        rows = session.scalars(select(AttendanceRecord).where(AttendanceRecord.materia_id == materia_id)).all()
        return ok(
            [
                {
                    "session_id": row.session_id,
                    "student_id": row.student_id,
                    "estado": row.estado,
                    "registered_at": row.registered_at.isoformat(),
                }
                for row in rows
            ]
        )