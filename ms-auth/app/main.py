from datetime import UTC, datetime, timedelta
from typing import Annotated
import redis
import json
import grpc
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, Integer, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from proto_generated import auth_pb2, auth_pb2_grpc
from proto_generated import notifications_pb2, notifications_pb2_grpc
from shared.app_common.config import BaseServiceSettings
from shared.app_common.database import Base, create_session_factory, session_scope
from shared.app_common.grpc_runtime import start_grpc_server
from shared.app_common.responses import ok
from shared.app_common.security import (
    create_access_token,
    decode_access_token,
    generate_reset_token,
    generate_temp_password,
    hash_password,
    verify_password,
)


class Settings(BaseServiceSettings):
    app_name: str = "AGM Auth & Users"
    service_slug: str = "ms-auth"
    rest_port: int = 8011
    grpc_port: int = 50051
    # ¡ADIÓS SQLITE! Apuntamos a la base de datos exclusiva de Auth en PostgreSQL
    database_url: str = "postgresql+psycopg://agm:agm_dev_password@postgres:5432/agm_auth_db"
    jwt_secret: str = "change-me-auth-secret"
    jwt_exp_minutes: int = 120
    admin_email: str = "admin@agm.local"
    admin_password: str = "Admin123!"
    # Ruta interna para pedirle a MS-6 que envíe correos
    notifications_grpc_target: str = "ms-notifications:50056"
    redis_url: str = "redis://redis:6379/0"  # <-- NUEVO


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(40), index=True)
    display_name: Mapped[str] = mapped_column(String(255))
    profile_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class PasswordReset(Base):
    __tablename__ = "password_resets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), index=True)
    token: Mapped[str] = mapped_column(String(255), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used: Mapped[bool] = mapped_column(Boolean, default=False)


class LoginRequest(BaseModel):
    email: str
    password: str = Field(min_length=6)


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=6)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in_minutes: int
    user: dict


def get_settings() -> Settings:
    return Settings()


def get_session_factory(request: Request) -> sessionmaker[Session]:
    return request.app.state.session_factory


bearer = HTTPBearer(auto_error=False)


def current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> dict:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    settings: Settings = request.app.state.settings
    try:
        claims = decode_access_token(credentials.credentials, settings.jwt_secret)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc

    session_factory = get_session_factory(request)
    with session_scope(session_factory) as session:
        user = session.get(User, int(claims["sub"]))
        if user is None or not user.is_active:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Inactive user")
        return {
            "id": user.id,
            "email": user.email,
            "role": user.role,
            "display_name": user.display_name,
            "profile_id": user.profile_id,
        }


def create_or_get_user(
    session: Session,
    *,
    email: str,
    role: str,
    display_name: str,
    profile_id: int | None,
) -> tuple[User, str | None, bool]:
    existing = session.scalar(select(User).where(User.email == email))
    if existing:
        existing.display_name = display_name or existing.display_name
        existing.profile_id = profile_id if profile_id is not None else existing.profile_id
        existing.role = role or existing.role
        return existing, None, False

    temporary_password = generate_temp_password()
    user = User(
        email=email,
        password_hash=hash_password(temporary_password),
        role=role,
        display_name=display_name,
        profile_id=profile_id,
    )
    session.add(user)
    session.flush()
    return user, temporary_password, True


def create_token_payload(user: User, settings: Settings) -> TokenResponse:
    token = create_access_token(str(user.id), user.role, settings.jwt_exp_minutes, settings.jwt_secret)
    return TokenResponse(
        access_token=token,
        expires_in_minutes=settings.jwt_exp_minutes,
        user={
            "id": user.id,
            "email": user.email,
            "role": user.role,
            "display_name": user.display_name,
            "profile_id": user.profile_id,
        },
    )


class AuthGrpcService(auth_pb2_grpc.AuthServiceServicer):
    def __init__(self, session_factory: sessionmaker[Session], settings: Settings):
        self.session_factory = session_factory
        self.settings = settings

    def ValidateToken(self, request, context):
        try:
            claims = decode_access_token(request.token, self.settings.jwt_secret)
        except Exception:
            return auth_pb2.UserClaims(valid=False)

        with session_scope(self.session_factory) as session:
            user = session.get(User, int(claims["sub"]))
            if user is None or not user.is_active:
                return auth_pb2.UserClaims(valid=False)
            return auth_pb2.UserClaims(
                valid=True,
                user_id=user.id,
                email=user.email,
                role=user.role,
                profile_id=user.profile_id or 0,
            )

    def GetUserById(self, request, context):
        with session_scope(self.session_factory) as session:
            user = session.get(User, request.user_id)
            if user is None:
                context.set_code(grpc.StatusCode.NOT_FOUND)
                context.set_details("User not found")
                return auth_pb2.UserProfile()
            return auth_pb2.UserProfile(
                user_id=user.id,
                email=user.email,
                role=user.role,
                profile_id=user.profile_id or 0,
                is_active=user.is_active,
            )

    def CheckRole(self, request, context):
        with session_scope(self.session_factory) as session:
            user = session.get(User, request.user_id)
            if user is None:
                return auth_pb2.BoolReply(ok=False, message="User not found")
            return auth_pb2.BoolReply(ok=user.role == request.role, message="")

    def ProvisionUser(self, request, context):
        with session_scope(self.session_factory) as session:
            user, temp_password, created = create_or_get_user(
                session,
                email=request.email,
                role=request.role,
                display_name=request.display_name,
                profile_id=request.profile_id or None,
            )
            return auth_pb2.ProvisionUserResponse(
                created=created,
                user_id=user.id,
                email=user.email,
                temporary_password=temp_password or "",
            )


app = FastAPI(title="AGM Auth & Users", version="0.1.0", root_path="/api/auth")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup_event() -> None:
    settings = get_settings()
    session_factory = create_session_factory(settings.database_url)
    engine = session_factory.kw["bind"]
    Base.metadata.create_all(engine)
    with session_scope(session_factory) as session:
        admin = session.scalar(select(User).where(User.email == settings.admin_email))
        if admin is None:
            session.add(
                User(
                    email=settings.admin_email,
                    password_hash=hash_password(settings.admin_password),
                    role="admin",
                    display_name="Administrador AGM",
                )
            )
    app.state.settings = settings
    app.state.session_factory = session_factory
    import redis  # Asegúrate de importar redis arriba del archivo
    app.state.redis = redis.from_url(settings.redis_url, decode_responses=True) # <-- NUEVO
    app.state.grpc_server, app.state.grpc_thread = start_grpc_server(
        settings.grpc_port,
        lambda server: auth_pb2_grpc.add_AuthServiceServicer_to_server(
            AuthGrpcService(session_factory, settings), server
        ),
    )


@app.on_event("shutdown")
def shutdown_event() -> None:
    grpc_server = getattr(app.state, "grpc_server", None)
    if grpc_server is not None:
        grpc_server.stop(grace=1)


@app.get("/auth/health")
def health() -> dict:
    return ok({"service": "auth", "status": "ok"})


@app.post("/auth/login")
def login(payload: LoginRequest, request: Request) -> dict:
    session_factory = get_session_factory(request)
    settings: Settings = request.app.state.settings
    with session_scope(session_factory) as session:
        user = session.scalar(select(User).where(User.email == payload.email))
        if user is None or not verify_password(payload.password, user.password_hash):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
        return ok(create_token_payload(user, settings).model_dump(), "Login correcto")


@app.post("/auth/refresh-token")
def refresh_token(request: Request, user=Depends(current_user)) -> dict:
    session_factory = get_session_factory(request)
    settings: Settings = request.app.state.settings
    with session_scope(session_factory) as session:
        db_user = session.get(User, user["id"])
        return ok(create_token_payload(db_user, settings).model_dump(), "Token renovado")


@app.post("/auth/forgot-password")
def forgot_password(payload: ForgotPasswordRequest, request: Request) -> dict:
    session_factory = get_session_factory(request)
    with session_scope(session_factory) as session:
        user = session.scalar(select(User).where(User.email == payload.email))
        if user is None:
            return ok(None, "Si el correo existe, se generó un enlace de recuperación")
        token = generate_reset_token()
        session.add(
            PasswordReset(
                email=user.email,
                token=token,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        # NUEVO: Publicar evento asíncrono (Fire and Forget)
        import json
        payload_evento = {"email": user.email, "reset_token": token}
        try:
            request.app.state.redis.lpush("evento_reset", json.dumps(payload_evento))
        except Exception as e:
            print(f"Falla silenciosa del Bus de Eventos: {e}")
        return ok({"reset_token": token}, "Token de recuperación generado")


@app.post("/auth/reset-password")
def reset_password(payload: ResetPasswordRequest, request: Request) -> dict:
    session_factory = get_session_factory(request)
    with session_scope(session_factory) as session:
        reset = session.scalar(select(PasswordReset).where(PasswordReset.token == payload.token))
        if reset is None or reset.used or reset.expires_at < datetime.now(UTC):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Token inválido o expirado")
        user = session.scalar(select(User).where(User.email == reset.email))
        if user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado")
        user.password_hash = hash_password(payload.new_password)
        reset.used = True
        return ok(None, "Contraseña actualizada")


@app.get("/auth/me")
def me(user=Depends(current_user)) -> dict:
    return ok(user)
