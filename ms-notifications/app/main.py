import json
import smtplib
import grpc
import urllib.request
from email.message import EmailMessage
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from proto_generated import notifications_pb2, notifications_pb2_grpc
from proto_generated import academics_pb2, academics_pb2_grpc
from shared.app_common.auth import require_roles
from shared.app_common.config import BaseServiceSettings
from shared.app_common.database import Base, create_session_factory, session_scope
from shared.app_common.grpc_runtime import start_grpc_server
from shared.app_common.responses import ok


class Settings(BaseServiceSettings):
    app_name: str = "AGM Notificaciones"
    service_slug: str = "ms-notifications"
    rest_port: int = 8016
    grpc_port: int = 50056
    # ¡ADIÓS SQLITE! Apuntamos a la base de datos exclusiva de Notificaciones
    database_url: str = "postgresql+psycopg://agm:agm_dev_password@postgres:5432/agm_notifications_db"
    
    # Credenciales SMTP (vacías por defecto para desarrollo local)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "noreply@agm.local"
    smtp_tls: bool = True
    academics_grpc_target: str = "ms-academics:50053"


class NotificationLog(Base):
    __tablename__ = "notification_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(50))
    recipient: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="logged")


class WelcomePayload(BaseModel):
    alumno_id: int
    materia_id: int
    email: str
    nombre: str
    temporary_password: str = ""


class ResetPasswordPayload(BaseModel):
    email: str
    reset_token: str


class SimpleMailPayload(BaseModel):
    alumno_id: int | None = None
    docente_id: int | None = None
    materia_id: int | None = None
    materia_nombre: str = ""
    motivo: str = ""
    alumnos_emails: list[str] = [] # <-- Coincide con el JSON de Postman


def build_message(kind: str, payload: dict[str, Any]) -> tuple[str, str, str]:
    if kind == "bienvenida":
        subject = "AGM | Bienvenida al sistema"
        body = (
            f"Hola {payload['nombre']},\n\n"
            f"Ya quedaste registrado(a) en AGM para la materia {payload['materia_id']}.\n"
            f"Tu usuario es: {payload['email']}\n"
            f"Tu clave temporal es: {payload.get('temporary_password') or 'ya existente'}\n"
        )
        return payload["email"], subject, body
        
    if kind == "baja":
        subject = f"AGM | Solicitud de baja - {payload.get('materia_nombre', '')}"
        body = (
            f"Estimado docente,\n\n"
            f"El alumno {payload.get('alumno_nombre', '')} ha solicitado la baja de la materia:\n"
            f"- Materia: {payload.get('materia_nombre', '')} (ID: {payload.get('materia_id', '')})\n"
            f"- Motivo: {payload.get('motivo', '')}\n\n"
            f"El sistema ha actualizado el pase de lista automáticamente."
        )
        return payload.get("recipient", "docente@agm.local"), subject, body
        
    if kind == "cierre-materia":
        subject = f"AGM | Cierre de materia - {payload.get('materia_nombre')}"
        body = f"La materia {payload.get('materia_nombre') or payload.get('materia_id')} fue cerrada y sus calificaciones publicadas."
        
        # Unimos la lista de correos para el destinatario
        lista = payload.get("alumnos_emails", [])
        recipient = ", ".join(lista) if lista else "grupo@agm.local"
        
        return recipient, subject, body
        
    subject = "AGM | Recuperación de contraseña"
    body = f"Usa este token para restablecer tu contraseña: {payload['reset_token']}"
    return payload["email"], subject, body


def deliver_email(settings: Settings, recipient: str, subject: str, body: str) -> str:
    if not settings.smtp_host:
        return "logged"
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as server:
        if settings.smtp_tls:
            server.starttls()
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(message)
    return "sent"


def persist_notification(session: Session, *, kind: str, recipient: str, subject: str, body: str, status_value: str) -> None:
    # Truncamos el string a 250 caracteres + "..." para que no explote PostgreSQL
    safe_recipient = recipient[:250] + "..." if len(recipient) > 255 else recipient
    
    session.add(
        NotificationLog(
            kind=kind,
            recipient=safe_recipient,
            subject=subject,
            body=body,
            status=status_value,
        )
    )


def process_notification(session: Session, settings: Settings, kind: str, payload: dict[str, Any]) -> notifications_pb2.BoolReply:
    recipient, subject, body = build_message(kind, payload)
    try:
        status_value = deliver_email(settings, recipient, subject, body)
    except Exception as exc:
        status_value = f"error: {exc}"
    persist_notification(session, kind=kind, recipient=recipient, subject=subject, body=body, status_value=status_value)
    return notifications_pb2.BoolReply(ok=True, message=status_value)


class NotificationsGrpcService(notifications_pb2_grpc.NotificationsServiceServicer):
    def __init__(self, session_factory: sessionmaker[Session], settings: Settings):
        self.session_factory = session_factory
        self.settings = settings

    def SendBienvenida(self, request, context):
        with session_scope(self.session_factory) as session:
            return process_notification(
                session,
                self.settings,
                "bienvenida",
                {
                    "alumno_id": request.alumno_id,
                    "materia_id": request.materia_id,
                    "email": request.email,
                    "nombre": request.nombre,
                    "temporary_password": request.temporary_password,
                },
            )

    def SendBajaNotif(self, request, context):
        with session_scope(self.session_factory) as session:
            return process_notification(
                session,
                self.settings,
                "baja",
                {
                    "alumno_id": request.alumno_id, 
                    "docente_id": request.docente_id, 
                    "motivo": request.motivo,
                    "recipient": request.docente_email,
                    "alumno_nombre": request.alumno_nombre,   # <-- RECIBIMOS
                    "materia_nombre": request.materia_nombre, # <-- RECIBIMOS
                    "materia_id": request.materia_id          # <-- RECIBIMOS
                },
            )

    def SendCierreMateria(self, request, context):
        with session_scope(self.session_factory) as session:
            return process_notification(
                session,
                self.settings,
                "cierre-materia",
                {
                    "materia_id": request.materia_id, 
                    "materia_nombre": request.materia_nombre,
                    "alumnos_emails": list(request.alumnos_emails) # <-- Mapeamos la lista gRPC
                },
            )

    def SendResetPassword(self, request, context):
        with session_scope(self.session_factory) as session:
            return process_notification(
                session,
                self.settings,
                "reset-password",
                {"email": request.email, "reset_token": request.reset_token},
            )


app = FastAPI(title="AGM Notificaciones", version="0.1.0", root_path="/api/notifications")
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
    app.state.grpc_server, app.state.grpc_thread = start_grpc_server(
        settings.grpc_port,
        lambda server: notifications_pb2_grpc.add_NotificationsServiceServicer_to_server(
            NotificationsGrpcService(session_factory, settings), server
        ),
    )


@app.on_event("shutdown")
def shutdown_event() -> None:
    grpc_server = getattr(app.state, "grpc_server", None)
    if grpc_server is not None:
        grpc_server.stop(grace=1)


@app.get("/health")
def health() -> dict:
    return ok({"service": "notifications", "status": "ok"})


@app.post("/notificaciones/bienvenida")
def welcome(payload: WelcomePayload, request: Request, user=Depends(require_roles("admin", "docente"))) -> dict:
    session_factory = request.app.state.session_factory
    with session_scope(session_factory) as session:
        reply = process_notification(session, request.app.state.settings, "bienvenida", payload.model_dump())
        return ok({"status": reply.message}, "Notificación registrada")


@app.post("/notificaciones/baja")
def baja(payload: SimpleMailPayload, request: Request, user=Depends(require_roles("admin", "alumno"))) -> dict:
    session_factory = request.app.state.session_factory
    with session_scope(session_factory) as session:
        reply = process_notification(session, request.app.state.settings, "baja", payload.model_dump())
        return ok({"status": reply.message}, "Notificación registrada")


@app.post("/notificaciones/cierre-materia")
def cierre(payload: SimpleMailPayload, request: Request, user=Depends(require_roles("admin", "docente"))) -> dict:
    session_factory = request.app.state.session_factory
    
    correos_finales = payload.alumnos_emails
    
    # Magia: Si no vienen correos en el JSON, los consultamos por gRPC
    if not correos_finales and payload.materia_id:
        try:
            with grpc.insecure_channel(request.app.state.settings.academics_grpc_target) as channel:
                stub = academics_pb2_grpc.AcademicsServiceStub(channel)
                respuesta = stub.GetAlumnosByMateria(academics_pb2.MateriaIdRequest(materia_id=payload.materia_id))
                # Extraemos solo los correos de la respuesta gRPC
                correos_finales = [alumno.email for alumno in respuesta.items if alumno.email]
        except Exception as e:
            print(f"Error consultando alumnos al MS-3: {e}")
            
    # Inyectamos los correos obtenidos al payload para que build_message los use
    payload_dict = payload.model_dump()
    payload_dict["alumnos_emails"] = correos_finales
    
    with session_scope(session_factory) as session:
        reply = process_notification(session, request.app.state.settings, "cierre-materia", payload_dict)
        return ok({
            "status": reply.message, 
            "total_enviados": len(correos_finales) # Para ver cuántos atrapó
        }, "Notificación registrada")


@app.post("/notificaciones/reset-password")
def reset_password(payload: ResetPasswordPayload, request: Request) -> dict:
    session_factory = request.app.state.session_factory
    with session_scope(session_factory) as session:
        reply = process_notification(session, request.app.state.settings, "reset-password", payload.model_dump())
        return ok({"status": reply.message}, "Notificación registrada")