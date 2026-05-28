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

#from proto_generated import notifications_pb2, notifications_pb2_grpc
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
    redis_url: str = "redis://redis:6379/0"  # <-- NUEVO
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


def get_html_template(title: str, content: str) -> str:
    # Plantilla HTML con CSS en línea (obligatorio para que funcione en Gmail/Outlook)
    return f"""
    <!DOCTYPE html>
    <html>
        <body style="font-family: 'Segoe UI', Arial, sans-serif; background-color: #f4f7f6; padding: 30px 10px; margin: 0;">
            <div style="max-width: 600px; margin: 0 auto; background: #ffffff; padding: 40px; border-radius: 10px; box-shadow: 0 4px 15px rgba(0,0,0,0.05);">
                
                <!-- Encabezado -->
                <div style="text-align: center; margin-bottom: 30px;">
                    <h2 style="color: #003b5c; border-bottom: 3px solid #00b5e2; display: inline-block; padding-bottom: 10px; margin: 0;">{title}</h2>
                </div>
                
                <!-- Contenido Dinámico -->
                <div style="color: #444444; line-height: 1.8; font-size: 16px;">
                    {content}
                </div>
                
                <!-- Botón de Acción (URL del Servidor) -->
                <div style="margin-top: 40px; text-align: center;">
                    <a href="http://3.95.61.121/" style="background-color: #003b5c; color: #ffffff; padding: 14px 30px; text-decoration: none; border-radius: 6px; font-weight: bold; font-size: 16px; display: inline-block;">Ingresar a la Plataforma</a>
                </div>
                
                <!-- Pie de página -->
                <div style="margin-top: 40px; padding-top: 20px; border-top: 1px solid #eeeeee; font-size: 12px; color: #999999; text-align: center;">
                    <p>Sistema de Gestión Académica (AGM)<br>Este es un correo automático, por favor no responda a esta dirección.</p>
                </div>
            </div>
        </body>
    </html>
    """

def build_message(kind: str, payload: dict[str, Any]) -> tuple[str, str, str]:
    if kind == "bienvenida":
        subject = "AGM | Bienvenida al sistema"
        clave_mostrar = payload.get('temporary_password') or '<em>La que ya tenías asignada</em>'
        content = (
            f"<p>Hola <strong>{payload['nombre']}</strong>,</p>"
            f"<p>Has sido registrado(a) exitosamente en AGM para la materia <strong>{payload['materia_id']}</strong>.</p>"
            f"<div style='background-color: #f8f9fa; padding: 20px; border-left: 4px solid #00b5e2; margin: 25px 0; border-radius: 4px;'>"
            f"<p style='margin: 0 0 10px 0;'><strong>Usuario:</strong> {payload['email']}</p>"
            f"<p style='margin: 0;'><strong>Clave temporal:</strong> <span style='font-family: monospace; font-size: 18px; color: #d9534f;'>{clave_mostrar}</span></p>"
            f"</div>"
        )
        body = get_html_template("Bienvenida a AGM", content)
        return payload["email"], subject, body

    if kind == "bienvenida_docente":
        subject = "AGM | Acceso a plataforma Docente"
        content = (
            f"<p>Estimado(a) docente <strong>{payload['nombre']}</strong>,</p>"
            f"<p>Su perfil ha sido habilitado en la plataforma académica.</p>"
            f"<div style='background-color: #f8f9fa; padding: 20px; border-left: 4px solid #00b5e2; margin: 25px 0; border-radius: 4px;'>"
            f"<p style='margin: 0 0 10px 0;'><strong>Usuario:</strong> {payload['email']}</p>"
            f"<p style='margin: 0;'><strong>Clave temporal:</strong> <span style='font-family: monospace; font-size: 18px; color: #d9534f;'>{payload.get('temporary_password')}</span></p>"
            f"</div>"
            f"<p style='color: #666;'><em>Le recomendamos cambiar su contraseña en la sección de perfil al ingresar por primera vez.</em></p>"
        )
        body = get_html_template("Perfil Docente Habilitado", content)
        return payload["email"], subject, body

    if kind == "baja":
        subject = f"AGM | Solicitud de baja - {payload.get('materia_nombre', '')}"
        content = (
            f"<p>Estimado docente,</p>"
            f"<p>El alumno <strong>{payload.get('alumno_nombre', '')}</strong> ha solicitado formalmente la baja de su materia:</p>"
            f"<div style='background-color: #fff3cd; padding: 20px; border: 1px solid #ffeeba; margin: 25px 0; border-radius: 4px;'>"
            f"<ul style='margin: 0; padding-left: 20px;'>"
            f"<li style='margin-bottom: 10px;'><strong>Materia:</strong> {payload.get('materia_nombre', '')} (ID: {payload.get('materia_id', '')})</li>"
            f"<li><strong>Motivo reportado:</strong> <em>\"{payload.get('motivo', '')}\"</em></li>"
            f"</ul>"
            f"</div>"
            f"<p>El sistema ha actualizado el pase de lista de manera automática.</p>"
        )
        body = get_html_template("Notificación de Baja", content)
        return payload.get("recipient", "docente@agm.local"), subject, body

    if kind == "cierre-materia":
        subject = f"AGM | Cierre de materia - {payload.get('materia_nombre')}"
        content = (
            f"<p>Estimado(a) estudiante,</p>"
            f"<p>Te notificamos que la materia <strong>{payload.get('materia_nombre') or payload.get('materia_id')}</strong> ha finalizado formalmente.</p>"
            f"<p>Las calificaciones finales ya han sido procesadas, guardadas y publicadas por tu docente.</p>"
            f"<p>Puedes consultar tu promedio final ingresando a tu portal de calificaciones.</p>"
        )
        body = get_html_template("Cierre de Evaluación", content)
        lista = payload.get("alumnos_emails", [])
        recipient = ", ".join(lista) if lista else "grupo@agm.local"
        return recipient, subject, body

    # kind == "reset-password"
    subject = "AGM | Recuperación de contraseña"
    content = (
        f"<p>Hola,</p>"
        f"<p>Hemos recibido una solicitud para recuperar el acceso a tu cuenta.</p>"
        f"<p>Tu código de recuperación seguro es:</p>"
        f"<div style='background-color: #e2e3e5; text-align: center; padding: 20px; margin: 25px 0; border-radius: 6px;'>"
        f"<span style='font-family: monospace; font-size: 24px; font-weight: bold; letter-spacing: 2px; color: #383d41;'>{payload['reset_token']}</span>"
        f"</div>"
        f"<p>Copia y pega este código en la ventana de recuperación para elegir una nueva contraseña. Si tú no solicitaste esto, puedes ignorar este correo de forma segura.</p>"
    )
    body = get_html_template("Restablecer Contraseña", content)
    return payload["email"], subject, body


def deliver_email(settings: Settings, recipient: str, subject: str, body: str) -> str:
    if not settings.smtp_host:
        return "logged"
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = recipient
    message["Subject"] = subject

    # EL CAMBIO ESTÁ AQUÍ: Agregamos subtype='html'
    message.set_content(body, subtype='html')
    
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

# NUEVA VERSIÓN: Ya no usa notifications_pb2
def process_notification(session: Session, settings: Settings, kind: str, payload: dict) -> str:
    recipient, subject, body = build_message(kind, payload)
    try:
        status_value = deliver_email(settings, recipient, subject, body)
    except Exception as exc:
        status_value = f"error: {exc}"
    persist_notification(session, kind=kind, recipient=recipient, subject=subject, body=body, status_value=status_value)
    
    return status_value  # Solo devolvemos el texto
#def process_notification(session: Session, settings: Settings, kind: str, payload: dict[str, Any]) -> notifications_pb2.BoolReply:
#    recipient, subject, body = build_message(kind, payload)
#    try:
#        status_value = deliver_email(settings, recipient, subject, body)
#    except Exception as exc:
#        status_value = f"error: {exc}"
#    persist_notification(session, kind=kind, recipient=recipient, subject=subject, body=body, status_value=status_value)
#    return notifications_pb2.BoolReply(ok=True, message=status_value)


#class NotificationsGrpcService(notifications_pb2_grpc.NotificationsServiceServicer):
#    def __init__(self, session_factory: sessionmaker[Session], settings: Settings):
#        self.session_factory = session_factory
#        self.settings = settings
#
#    def SendBienvenida(self, request, context):
#        with session_scope(self.session_factory) as session:
#            return process_notification(
#                session,
#                self.settings,
#                "bienvenida",
#                {
#                    "alumno_id": request.alumno_id,
#                    "materia_id": request.materia_id,
#                    "email": request.email,
#                    "nombre": request.nombre,
#                    "temporary_password": request.temporary_password,
#                },
#            )
#
#    def SendBajaNotif(self, request, context):
#        with session_scope(self.session_factory) as session:
#            return process_notification(
#                session,
#                self.settings,
#                "baja",
#                {
#                    "alumno_id": request.alumno_id, 
#                    "docente_id": request.docente_id, 
#                    "motivo": request.motivo,
#                    "recipient": request.docente_email,
#                    "alumno_nombre": request.alumno_nombre,   # <-- RECIBIMOS
#                    "materia_nombre": request.materia_nombre, # <-- RECIBIMOS
#                    "materia_id": request.materia_id          # <-- RECIBIMOS
#                },
#            )
#
#    def SendCierreMateria(self, request, context):
#        with session_scope(self.session_factory) as session:
#            return process_notification(
#                session,
#                self.settings,
#                "cierre-materia",
#                {
#                    "materia_id": request.materia_id, 
#                    "materia_nombre": request.materia_nombre,
#                    "alumnos_emails": list(request.alumnos_emails) # <-- Mapeamos la lista gRPC
#                },
#            )
#
#    def SendResetPassword(self, request, context):
#        with session_scope(self.session_factory) as session:
#            return process_notification(
#                session,
#                self.settings,
#                "reset-password",
#                {"email": request.email, "reset_token": request.reset_token},
#            )

import time
import threading
import json
import redis
import grpc

def listen_to_redis(app_state):
    # Lista de buzones (colas) que vamos a revisar constantemente
    colas = ["evento_bienvenida", "evento_bienvenida_docente", "evento_baja", "evento_cierre", "evento_reset"]
    print("MS-Notifications: Conectado a Redis. Esperando mensajes en la cola...", flush=True)
    
    while True:
        try:
            # brpop (Blocking Right Pop) se queda esperando (bloqueado) hasta que aparezca un mensaje.
            # Retorna una tupla: (nombre_de_la_cola, datos)
            resultado = app_state.redis.brpop(colas, timeout=0)
            
            if resultado:
                canal = resultado[0]
                datos = resultado[1]
                payload = json.loads(datos)
                
                print(f"\n--> [COLA DE EVENTOS] Desencolando evento pendiente: {canal}", flush=True)
                print(f"Datos recibidos: {payload}", flush=True)
                
                with session_scope(app_state.session_factory) as session:
                    if canal == "evento_bienvenida":
                        process_notification(session, app_state.settings, "bienvenida", payload)
                    elif canal == "evento_bienvenida_docente":
                        process_notification(session, app_state.settings, "bienvenida_docente", payload)
                    elif canal == "evento_baja":
                        process_notification(session, app_state.settings, "baja", payload)
                    elif canal == "evento_cierre":
                        correos_finales = payload.get("alumnos_emails", [])
                        if not correos_finales and payload.get("materia_id"):
                            try:
                                with grpc.insecure_channel(app_state.settings.academics_grpc_target) as channel:
                                    stub = academics_pb2_grpc.AcademicsServiceStub(channel)
                                    respuesta = stub.GetAlumnosByMateria(academics_pb2.MateriaIdRequest(materia_id=payload["materia_id"]))
                                    correos_finales = [a.email for a in respuesta.items if a.email]
                            except Exception as e:
                                print(f"Error gRPC obteniendo alumnos: {e}", flush=True)
                        payload["alumnos_emails"] = correos_finales
                        process_notification(session, app_state.settings, "cierre-materia", payload)
                    elif canal == "evento_reset":
                        process_notification(session, app_state.settings, "reset-password", payload)
                        
                print(f"--> [COLA DE EVENTOS] Evento {canal} resuelto y eliminado de la cola.\n", flush=True)
                
        except Exception as e:
            print(f"Error revisando la cola o desconexión de Redis. Reintentando... Detalle: {e}", flush=True)
            time.sleep(5)

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
    # NUEVO: Conectar a Redis y arrancar el Bus de Eventos
    app.state.redis = redis.from_url(settings.redis_url, decode_responses=True)
    app.state.redis_thread = threading.Thread(target=listen_to_redis, args=(app.state,), daemon=True)
    app.state.redis_thread.start()

    # (Deja tu start_grpc_server intacto abajo)
    #app.state.grpc_server, app.state.grpc_thread = start_grpc_server(
    #    settings.grpc_port,
    #    lambda server: notifications_pb2_grpc.add_NotificationsServiceServicer_to_server(
    #        NotificationsGrpcService(session_factory, settings), server
    #    ),
    #)


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
        status_msg = process_notification(session, request.app.state.settings, "bienvenida", payload.model_dump())
        return ok({"status": status_msg}, "Notificación registrada")


@app.post("/notificaciones/baja")
def baja(payload: SimpleMailPayload, request: Request, user=Depends(require_roles("admin", "alumno"))) -> dict:
    session_factory = request.app.state.session_factory
    with session_scope(session_factory) as session:
        status_msg = process_notification(session, request.app.state.settings, "baja", payload.model_dump())
        return ok({"status": status_msg}, "Notificación registrada")


@app.post("/notificaciones/cierre-materia")
def cierre(payload: SimpleMailPayload, request: Request, user=Depends(require_roles("admin", "docente"))) -> dict:
    import json
    
    # Simplemente empaquetamos la orden del administrador
    payload_dict = payload.model_dump()
    
    try:
        # Publicamos el evento en el Bus de Eventos. 
        # El hilo 'listen_to_redis' (que agregamos antes) se encargará de consultar 
        # los alumnos por gRPC y enviar los correos en segundo plano.
        request.app.state.redis.lpush("evento_cierre", json.dumps(payload_dict))
    except Exception as e:
        print(f"Error publicando en bus de eventos: {e}")
        
    # El Frontend recibe un OK inmediato, sin quedarse trabado esperando el SMTP
    return ok(
        {"status": "Encolado", "info": "Los correos se procesarán de forma asíncrona"}, 
        "Cierre de materia iniciado"
    )


@app.post("/notificaciones/reset-password")
def reset_password(payload: ResetPasswordPayload, request: Request) -> dict:
    session_factory = request.app.state.session_factory
    with session_scope(session_factory) as session:
        status_msg = process_notification(session, request.app.state.settings, "reset-password", payload.model_dump())
        return ok({"status": status_msg}, "Notificación registrada")