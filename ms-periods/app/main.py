import json
import re
import unicodedata

import grpc
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship, sessionmaker

from proto_generated import academics_pb2, academics_pb2_grpc
from proto_generated import periods_pb2, periods_pb2_grpc
from shared.app_common.auth import require_roles
from shared.app_common.config import BaseServiceSettings
from shared.app_common.database import Base, create_session_factory, session_scope
from shared.app_common.files import extract_pdf_text, resolve_input_file
from shared.app_common.grpc_runtime import start_grpc_server
from shared.app_common.responses import ok


class Settings(BaseServiceSettings):
    app_name: str = "AGM Periodos & Materias"
    service_slug: str = "ms-periods"
    rest_port: int = 8012
    grpc_port: int = 50052
    database_url: str = "sqlite:///./data/periods.db"


class Period(Base):
    __tablename__ = "periods"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre: Mapped[str] = mapped_column(String(120), unique=True)
    fecha_inicio: Mapped[str] = mapped_column(String(40))
    fecha_fin: Mapped[str] = mapped_column(String(40))
    plan_estudios: Mapped[str] = mapped_column(String(120))
    activo: Mapped[bool] = mapped_column(Boolean, default=False)
    materias: Mapped[list["Subject"]] = relationship(back_populates="periodo", cascade="all, delete-orphan")


class Subject(Base):
    __tablename__ = "subjects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period_id: Mapped[int] = mapped_column(ForeignKey("periods.id"))
    docente_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    nrc: Mapped[str] = mapped_column(String(20), index=True)
    clave: Mapped[str] = mapped_column(String(30))
    nombre: Mapped[str] = mapped_column(String(255))
    seccion: Mapped[str] = mapped_column(String(20))
    docente_nombre: Mapped[str] = mapped_column(String(255))
    horario_json: Mapped[str] = mapped_column(Text, default="[]")
    salon: Mapped[str] = mapped_column(String(60), default="")
    estado: Mapped[str] = mapped_column(String(40), default="abierta")
    periodo: Mapped[Period] = relationship(back_populates="materias")


class PeriodPayload(BaseModel):
    nombre: str
    fecha_inicio: str
    fecha_fin: str
    plan_estudios: str
    activo: bool = False


def normalize_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalize_name_key(value: str) -> str:
    clean = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    clean = clean.replace("-", " ")
    tokens = sorted(token for token in re.split(r"[^A-Za-z]+", clean.upper()) if token)
    return " ".join(tokens)


def normalize_ascii(value: str) -> str:
    return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").upper()


def parse_schedule_pdf(raw_text: str) -> dict:
    text = raw_text.replace("\r", "")
    lines = [normalize_spaces(line) for line in text.splitlines() if normalize_spaces(line)]
    period_name = "Periodo importado"
    plan = "Plan no identificado"
    for line in lines:
        normalized = normalize_ascii(line)
        if "PROGRAMACION ACADEMICA -" in normalized:
            period_name = line.split("-", 1)[1].strip()
        if "INGENIERIA" in normalized or "LICENCIATURA" in normalized:
            plan = line
    subjects: dict[tuple[str, str], dict] = {}
    pattern = re.compile(
        r"^(?P<nrc>\d{5})\s+(?P<clave>[A-Z]{4}\s+\d{3})\s+(?P<nombre>.+?)\s+(?P<seccion>[A-Z0-9]{3})\s+"
        r"(?P<dia>[LMAJVSD])\s+(?P<hora>\d{4}-\d{2}\s?\d{2}|\d{4}-\d{4})\s+(?P<docente>.+?)\s+(?P<salon>[0-9A-Z/.-]+)$"
    )
    for line in lines:
        match = pattern.match(line)
        if not match:
            continue
        data = match.groupdict()
        key = (data["nrc"], data["seccion"])
        subject = subjects.setdefault(
            key,
            {
                "nrc": data["nrc"],
                "clave": data["clave"],
                "nombre": normalize_spaces(data["nombre"]),
                "seccion": data["seccion"],
                "docente_nombre": normalize_spaces(data["docente"].replace(" - ", "-")),
                "horario": [],
                "salon": data["salon"],
            },
        )
        subject["horario"].append(
            {"dia": data["dia"], "hora": data["hora"].replace(" ", ""), "salon": data["salon"]}
        )
    return {"periodo_nombre": period_name.title(), "plan_estudios": plan.title(), "materias": list(subjects.values())}


def subject_dict(subject: Subject) -> dict:
    return {
        "id": subject.id,
        "period_id": subject.period_id,
        "docente_id": subject.docente_id,
        "nrc": subject.nrc,
        "clave": subject.clave,
        "nombre": subject.nombre,
        "seccion": subject.seccion,
        "docente_nombre": subject.docente_nombre,
        "horario": json.loads(subject.horario_json),
        "salon": subject.salon,
        "estado": subject.estado,
    }


def resolve_teacher_id(app: FastAPI, teacher_name: str) -> int | None:
    settings: Settings = app.state.settings
    try:
        with grpc.insecure_channel(settings.academics_grpc_target) as channel:
            stub = academics_pb2_grpc.AcademicsServiceStub(channel)
            docente = stub.FindDocenteByName(academics_pb2.NameRequest(name=teacher_name))
            return docente.docente_id or None
    except grpc.RpcError:
        return None


class PeriodsGrpcService(periods_pb2_grpc.PeriodsServiceServicer):
    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def GetMateriaById(self, request, context):
        with session_scope(self.session_factory) as session:
            subject = session.get(Subject, request.materia_id)
            if subject is None:
                context.set_code(grpc.StatusCode.NOT_FOUND)
                context.set_details("Materia no encontrada")
                return periods_pb2.MateriaInfo()
            return periods_pb2.MateriaInfo(
                materia_id=subject.id,
                periodo_id=subject.period_id,
                docente_id=subject.docente_id or 0,
                nrc=subject.nrc,
                clave=subject.clave,
                nombre=subject.nombre,
                seccion=subject.seccion,
                docente=subject.docente_nombre,
                horario_json=subject.horario_json,
                salon=subject.salon,
                estado=subject.estado,
            )

    def GetMateriasByDocente(self, request, context):
        with session_scope(self.session_factory) as session:
            items = session.scalars(select(Subject).where(Subject.docente_id == request.docente_id)).all()
            return periods_pb2.MateriaList(
                items=[
                    periods_pb2.MateriaInfo(
                        materia_id=item.id,
                        periodo_id=item.period_id,
                        docente_id=item.docente_id or 0,
                        nrc=item.nrc,
                        clave=item.clave,
                        nombre=item.nombre,
                        seccion=item.seccion,
                        docente=item.docente_nombre,
                        horario_json=item.horario_json,
                        salon=item.salon,
                        estado=item.estado,
                    )
                    for item in items
                ]
            )

    def GetPeriodoActivo(self, request, context):
        with session_scope(self.session_factory) as session:
            period = session.scalar(select(Period).where(Period.activo.is_(True)))
            if period is None:
                return periods_pb2.PeriodoInfo()
            return periods_pb2.PeriodoInfo(
                periodo_id=period.id,
                nombre=period.nombre,
                fecha_inicio=period.fecha_inicio,
                fecha_fin=period.fecha_fin,
                plan_estudios=period.plan_estudios,
                activo=period.activo,
            )

    def GetMateriasByPeriodo(self, request, context):
        with session_scope(self.session_factory) as session:
            items = session.scalars(select(Subject).where(Subject.period_id == request.periodo_id)).all()
            return periods_pb2.MateriaList(
                items=[
                    periods_pb2.MateriaInfo(
                        materia_id=item.id,
                        periodo_id=item.period_id,
                        docente_id=item.docente_id or 0,
                        nrc=item.nrc,
                        clave=item.clave,
                        nombre=item.nombre,
                        seccion=item.seccion,
                        docente=item.docente_nombre,
                        horario_json=item.horario_json,
                        salon=item.salon,
                        estado=item.estado,
                    )
                    for item in items
                ]
            )


app = FastAPI(title="AGM Periodos & Materias", version="0.1.0")
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
        lambda server: periods_pb2_grpc.add_PeriodsServiceServicer_to_server(PeriodsGrpcService(session_factory), server),
    )


@app.on_event("shutdown")
def shutdown_event() -> None:
    grpc_server = getattr(app.state, "grpc_server", None)
    if grpc_server is not None:
        grpc_server.stop(grace=1)


@app.get("/health")
def health() -> dict:
    return ok({"service": "periods", "status": "ok"})


@app.get("/periodos")
def list_periods(
    request: Request,
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    user=Depends(require_roles("admin", "docente", "alumno")),
) -> dict:
    session_factory = request.app.state.session_factory
    with session_scope(session_factory) as session:
        items = session.scalars(select(Period).offset((page - 1) * limit).limit(limit)).all()
        return ok(
            [
                {
                    "id": item.id,
                    "nombre": item.nombre,
                    "fecha_inicio": item.fecha_inicio,
                    "fecha_fin": item.fecha_fin,
                    "plan_estudios": item.plan_estudios,
                    "activo": item.activo,
                }
                for item in items
            ]
        )


@app.post("/periodos")
def create_period(payload: PeriodPayload, request: Request, user=Depends(require_roles("admin"))) -> dict:
    session_factory = request.app.state.session_factory
    with session_scope(session_factory) as session:
        if payload.activo:
            for period in session.scalars(select(Period).where(Period.activo.is_(True))).all():
                period.activo = False
        period = Period(**payload.model_dump())
        session.add(period)
        session.flush()
        return ok({"id": period.id}, "Periodo creado")


@app.put("/periodos/{period_id}")
def update_period(period_id: int, payload: PeriodPayload, request: Request, user=Depends(require_roles("admin"))) -> dict:
    session_factory = request.app.state.session_factory
    with session_scope(session_factory) as session:
        period = session.get(Period, period_id)
        if period is None:
            raise HTTPException(status_code=404, detail="Periodo no encontrado")
        if payload.activo:
            for active in session.scalars(select(Period).where(Period.activo.is_(True), Period.id != period_id)).all():
                active.activo = False
        for key, value in payload.model_dump().items():
            setattr(period, key, value)
        return ok({"id": period.id}, "Periodo actualizado")


@app.delete("/periodos/{period_id}")
def delete_period(period_id: int, request: Request, user=Depends(require_roles("admin"))) -> dict:
    session_factory = request.app.state.session_factory
    with session_scope(session_factory) as session:
        period = session.get(Period, period_id)
        if period is None:
            raise HTTPException(status_code=404, detail="Periodo no encontrado")
        session.delete(period)
        return ok(None, "Periodo eliminado")


@app.post("/periodos/importar")
async def import_period(
    request: Request,
    periodo_id: int | None = Form(None),
    source_path: str | None = Form(None),
    file: UploadFile | None = File(None),
    user=Depends(require_roles("admin")),
) -> dict:
    path = await resolve_input_file(upload=file, source_path=source_path, suffix=".pdf")
    parsed = parse_schedule_pdf(extract_pdf_text(path))
    session_factory = request.app.state.session_factory
    with session_scope(session_factory) as session:
        if periodo_id:
            period = session.get(Period, periodo_id)
            if period is None:
                raise HTTPException(status_code=404, detail="Periodo no encontrado")
        else:
            period = session.scalar(select(Period).where(Period.nombre == parsed["periodo_nombre"]))
            if period is None:
                period = Period(
                    nombre=parsed["periodo_nombre"],
                    fecha_inicio="Por definir",
                    fecha_fin="Por definir",
                    plan_estudios=parsed["plan_estudios"],
                    activo=False,
                )
                session.add(period)
                session.flush()

        imported = 0
        preview = []
        for item in parsed["materias"]:
            subject = session.scalar(
                select(Subject).where(
                    Subject.period_id == period.id,
                    Subject.nrc == item["nrc"],
                    Subject.seccion == item["seccion"],
                )
            )
            docente_id = resolve_teacher_id(request.app, item["docente_nombre"])
            if subject is None:
                subject = Subject(
                    period_id=period.id,
                    docente_id=docente_id,
                    nrc=item["nrc"],
                    clave=item["clave"],
                    nombre=item["nombre"],
                    seccion=item["seccion"],
                    docente_nombre=item["docente_nombre"],
                    horario_json=json.dumps(item["horario"], ensure_ascii=False),
                    salon=item["salon"],
                    estado="abierta",
                )
                session.add(subject)
                imported += 1
            else:
                subject.docente_id = docente_id
                subject.docente_nombre = item["docente_nombre"]
                subject.horario_json = json.dumps(item["horario"], ensure_ascii=False)
                subject.salon = item["salon"]
            if len(preview) < 8:
                preview.append(item)
        return ok(
            {
                "periodo_id": period.id,
                "periodo_nombre": period.nombre,
                "plan_estudios": period.plan_estudios,
                "materias_detectadas": len(parsed["materias"]),
                "materias_nuevas": imported,
                "preview": preview,
            },
            "Programación académica importada",
        )


@app.get("/materias")
def list_subjects(
    request: Request,
    periodo: int | None = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    user=Depends(require_roles("admin", "docente", "alumno")),
) -> dict:
    session_factory = request.app.state.session_factory
    with session_scope(session_factory) as session:
        stmt = select(Subject)
        if periodo is not None:
            stmt = stmt.where(Subject.period_id == periodo)
        items = session.scalars(stmt.offset((page - 1) * limit).limit(limit)).all()
        return ok([subject_dict(item) for item in items])


@app.get("/materias/{materia_id}")
def get_subject(materia_id: int, request: Request, user=Depends(require_roles("admin", "docente", "alumno"))) -> dict:
    session_factory = request.app.state.session_factory
    with session_scope(session_factory) as session:
        subject = session.get(Subject, materia_id)
        if subject is None:
            raise HTTPException(status_code=404, detail="Materia no encontrada")
        return ok(subject_dict(subject))
