import csv
import io
import re
import unicodedata
from pathlib import Path

import grpc
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from openpyxl import load_workbook
from sqlalchemy import Boolean, Integer, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from proto_generated import academics_pb2, academics_pb2_grpc
from proto_generated import auth_pb2, auth_pb2_grpc
from proto_generated import notifications_pb2, notifications_pb2_grpc
from proto_generated import periods_pb2, periods_pb2_grpc
from shared.app_common.auth import require_roles
from shared.app_common.config import BaseServiceSettings
from shared.app_common.database import Base, create_session_factory, session_scope
from shared.app_common.files import extract_pdf_text, resolve_input_file
from shared.app_common.grpc_runtime import start_grpc_server
from shared.app_common.responses import ok


class Settings(BaseServiceSettings):
    app_name: str = "AGM Docentes & Alumnos"
    service_slug: str = "ms-academics"
    rest_port: int = 8013
    grpc_port: int = 50053
    # ¡ADIÓS SQLITE! Apuntamos a la base de datos exclusiva de Academics
    database_url: str = "postgresql+psycopg://agm:agm_dev_password@postgres:5432/agm_academics_db"
    
    # Rutas internas para comunicarse con los demás microservicios
    auth_grpc_target: str = "ms-auth:50051"
    periods_grpc_target: str = "ms-periods:50052"
    notifications_grpc_target: str = "ms-notifications:50056"


class Teacher(Base):
    __tablename__ = "teachers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    ubicacion: Mapped[str] = mapped_column(String(80), default="")
    extension: Mapped[str] = mapped_column(String(40), default="")
    name_key: Mapped[str] = mapped_column(String(255), index=True)


class Student(Base):
    __tablename__ = "students"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    matricula: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    nombre: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(60), default="Inscrito por Web")
    nivel: Mapped[str] = mapped_column(String(60), default="Licenciatura")


class Enrollment(Base):
    __tablename__ = "enrollments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_id: Mapped[int] = mapped_column(Integer, index=True)
    materia_id: Mapped[int] = mapped_column(Integer, index=True)
    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    baja_count: Mapped[int] = mapped_column(Integer, default=0)


def normalize_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalize_key(value: str) -> str:
    clean = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    clean = clean.replace("-", " ")
    tokens = sorted(token for token in re.split(r"[^A-Za-z]+", clean.upper()) if token)
    return " ".join(tokens)


def fuzzy_teacher_score(query: str, candidate: str) -> float:
    query_tokens = [token for token in normalize_key(query).split(" ") if token]
    candidate_tokens = [token for token in normalize_key(candidate).split(" ") if token]
    if not query_tokens or not candidate_tokens:
        return 0.0
    matches = 0
    for token in query_tokens:
        if any(
            token == other
            or token in other
            or other in token
            or (len(token) >= 3 and len(other) >= 3 and (token[:3] == other[:3] or token[-3:] == other[-3:]))
            for other in candidate_tokens
        ):
            matches += 1
    return matches / max(len(query_tokens), len(candidate_tokens))


def parse_teacher_pdf(raw_text: str) -> list[dict]:
    teachers = []
    for line in raw_text.splitlines():
        if "@correo.buap.mx" not in line:
            continue
        line = normalize_spaces(line)
        email_match = re.search(r"([a-z0-9._-]+@correo\.buap\.mx)", line, flags=re.IGNORECASE)
        if not email_match:
            continue
        email = email_match.group(1).lower()
        before = normalize_spaces(line[: email_match.start()])
        after = normalize_spaces(line[email_match.end() :])
        pieces = after.split(" ")
        location = ""
        extension = ""
        if pieces:
            if pieces[0].startswith("CCO") or pieces[0].startswith("1CCO"):
                location = pieces[0]
                if len(pieces) > 1 and re.fullmatch(r"[\d, ]+", " ".join(pieces[1:])):
                    extension = normalize_spaces(" ".join(pieces[1:]))
        teachers.append(
            {
                "nombre": before.title(),
                "email": email,
                "ubicacion": location,
                "extension": extension,
                "name_key": normalize_key(before),
            }
        )
    return teachers


def parse_students_from_pdf(raw_text: str) -> list[dict]:
    flat = normalize_spaces(raw_text.replace("\n", " "))
    
    pattern = re.compile(
        r"([A-ZÁÉÍÓÚÑ ,.\-]+?)\s*(\d{9})\s*\**Inscrito por Web\**\s*(Licenciatura|Posgrado)",
        flags=re.IGNORECASE,
    )
    
    email_pattern = re.compile(r"([a-z0-9._-]+@[a-z0-9.-]+\.[a-z]{2,})", flags=re.IGNORECASE)
    all_emails = email_pattern.findall(flat)
    
    # 1. Filtrar solo correos de alumnos
    raw_student_emails = [e.lower() for e in all_emails if "alumno" in e.lower()]
    
    # 2. Deduplicar preservando el orden. ¡Esto destruye los duplicados y los botones globales!
    student_emails = []
    for e in raw_student_emails:
        if e not in student_emails:
            student_emails.append(e)
    
    rows = []
    for i, match in enumerate(pattern.finditer(flat)):
        name = normalize_spaces(match.group(1)).strip().title()
        matricula = match.group(2)
        nivel = match.group(3).title()
        
        # Emparejamiento perfecto de 1 a 1
        assigned_email = student_emails[i] if i < len(student_emails) else f"{matricula}@alumno.agm.local"
        
        rows.append(
            {
                "nombre": name,
                "matricula": matricula,
                "status": "Inscrito por Web",
                "nivel": nivel,
                "email": assigned_email
            }
        )
    return rows


def parse_students_from_csv(path: Path) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            matricula = str(row.get("matricula") or row.get("id") or "").strip()
            nombre = normalize_spaces(str(row.get("nombre") or row.get("name") or ""))
            if not matricula or not nombre:
                continue
            rows.append(
                {
                    "nombre": nombre.title(),
                    "matricula": matricula,
                    "status": normalize_spaces(str(row.get("status") or "Inscrito por Web")),
                    "nivel": normalize_spaces(str(row.get("nivel") or "Licenciatura")),
                    "email": normalize_spaces(str(row.get("email") or "")),
                }
            )
    return rows


def parse_students_from_xlsx(path: Path) -> list[dict]:
    workbook = load_workbook(path, read_only=True)
    sheet = workbook.active
    header = [str(cell.value or "").strip().lower() for cell in next(sheet.iter_rows(min_row=1, max_row=1))]
    rows = []
    for values in sheet.iter_rows(min_row=2, values_only=True):
        row = dict(zip(header, values))
        matricula = str(row.get("matricula") or row.get("id") or "").strip()
        nombre = normalize_spaces(str(row.get("nombre") or row.get("name") or ""))
        if not matricula or not nombre:
            continue
        rows.append(
            {
                "nombre": nombre.title(),
                "matricula": matricula,
                "status": normalize_spaces(str(row.get("status") or "Inscrito por Web")),
                "nivel": normalize_spaces(str(row.get("nivel") or "Licenciatura")),
                "email": normalize_spaces(str(row.get("email") or "")),
            }
        )
    return rows


def subject_exists(target: str, materia_id: int) -> bool:
    try:
        with grpc.insecure_channel(target) as channel:
            stub = periods_pb2_grpc.PeriodsServiceStub(channel)
            materia = stub.GetMateriaById(periods_pb2.MateriaIdRequest(materia_id=materia_id))
            return bool(materia.materia_id)
    except grpc.RpcError as exc:
        raise HTTPException(status_code=503, detail="Periods service unavailable") from exc


def provision_user(target: str, *, email: str, role: str, profile_id: int, display_name: str) -> tuple[int, str | None]:
    with grpc.insecure_channel(target) as channel:
        stub = auth_pb2_grpc.AuthServiceStub(channel)
        reply = stub.ProvisionUser(
            auth_pb2.ProvisionUserRequest(
                email=email,
                role=role,
                profile_id=profile_id,
                display_name=display_name,
            )
        )
        return reply.user_id, reply.temporary_password or None


def send_welcome(
    target: str,
    *,
    alumno_id: int,
    materia_id: int,
    email: str,
    nombre: str,
    temporary_password: str | None,
) -> None:
    try:
        with grpc.insecure_channel(target) as channel:
            stub = notifications_pb2_grpc.NotificationsServiceStub(channel)
            stub.SendBienvenida(
                notifications_pb2.BienvenidaRequest(
                    alumno_id=alumno_id,
                    materia_id=materia_id,
                    email=email,
                    nombre=nombre,
                    temporary_password=temporary_password or "",
                )
            )
    except grpc.RpcError:
        return


def student_to_dict(student: Student, enrollment: Enrollment | None = None) -> dict:
    data = {
        "id": student.id,
        "matricula": student.matricula,
        "nombre": student.nombre,
        "email": student.email,
        "status": student.status,
        "nivel": student.nivel,
    }
    if enrollment is not None:
        data["activo"] = enrollment.activo
        data["baja_count"] = enrollment.baja_count
    return data


class AcademicsGrpcService(academics_pb2_grpc.AcademicsServiceServicer):
    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def GetAlumnosByMateria(self, request, context):
        with session_scope(self.session_factory) as session:
            enrollments = session.scalars(
                select(Enrollment).where(Enrollment.materia_id == request.materia_id, Enrollment.activo.is_(True))
            ).all()
            students = [session.get(Student, item.student_id) for item in enrollments]
            return academics_pb2.AlumnoList(
                items=[
                    academics_pb2.AlumnoInfo(
                        alumno_id=student.id,
                        matricula=student.matricula,
                        nombre=student.nombre,
                        email=student.email,
                        status=student.status,
                        nivel=student.nivel,
                        activo=True,
                    )
                    for student in students
                    if student is not None
                ]
            )

    def GetAlumnoById(self, request, context):
        with session_scope(self.session_factory) as session:
            student = session.get(Student, request.alumno_id)
            if student is None:
                context.set_code(grpc.StatusCode.NOT_FOUND)
                context.set_details("Alumno no encontrado")
                return academics_pb2.AlumnoInfo()
            return academics_pb2.AlumnoInfo(
                alumno_id=student.id,
                matricula=student.matricula,
                nombre=student.nombre,
                email=student.email,
                status=student.status,
                nivel=student.nivel,
                activo=True,
            )

    def IsAlumnoEnMateria(self, request, context):
        with session_scope(self.session_factory) as session:
            enrollment = session.scalar(
                select(Enrollment).where(
                    Enrollment.student_id == request.alumno_id,
                    Enrollment.materia_id == request.materia_id,
                    Enrollment.activo.is_(True),
                )
            )
            return academics_pb2.BoolReply(ok=enrollment is not None, message="")

    def GetDocenteById(self, request, context):
        with session_scope(self.session_factory) as session:
            teacher = session.get(Teacher, request.docente_id)
            if teacher is None:
                context.set_code(grpc.StatusCode.NOT_FOUND)
                context.set_details("Docente no encontrado")
                return academics_pb2.DocenteInfo()
            return academics_pb2.DocenteInfo(
                docente_id=teacher.id,
                nombre=teacher.nombre,
                email=teacher.email,
                ubicacion=teacher.ubicacion,
                extension=teacher.extension,
            )

    def FindDocenteByName(self, request, context):
        with session_scope(self.session_factory) as session:
            key = normalize_key(request.name)
            teacher = session.scalar(select(Teacher).where(Teacher.name_key == key))
            if teacher is None:
                best_score = 0.0
                best_teacher = None
                for candidate in session.scalars(select(Teacher)).all():
                    score = fuzzy_teacher_score(request.name, candidate.nombre)
                    if score > best_score:
                        best_score = score
                        best_teacher = candidate
                teacher = best_teacher if best_score >= 0.6 else None
            if teacher is None:
                return academics_pb2.DocenteInfo()
            return academics_pb2.DocenteInfo(
                docente_id=teacher.id,
                nombre=teacher.nombre,
                email=teacher.email,
                ubicacion=teacher.ubicacion,
                extension=teacher.extension,
            )


app = FastAPI(title="AGM Docentes & Alumnos", version="0.1.0")
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
        lambda server: academics_pb2_grpc.add_AcademicsServiceServicer_to_server(
            AcademicsGrpcService(session_factory), server
        ),
    )


@app.on_event("shutdown")
def shutdown_event() -> None:
    grpc_server = getattr(app.state, "grpc_server", None)
    if grpc_server is not None:
        grpc_server.stop(grace=1)


@app.get("/health")
def health() -> dict:
    return ok({"service": "academics", "status": "ok"})


@app.post("/docentes/importar")
async def import_teachers(
    request: Request,
    source_path: str | None = Form(None),
    file: UploadFile | None = File(None),
    user=Depends(require_roles("admin")),
) -> dict:
    path = await resolve_input_file(upload=file, source_path=source_path, suffix=".pdf")
    teachers = parse_teacher_pdf(extract_pdf_text(path))
    session_factory = request.app.state.session_factory
    created = 0
    with session_scope(session_factory) as session:
        for item in teachers:
            teacher = session.scalar(select(Teacher).where(Teacher.email == item["email"]))
            if teacher is None:
                teacher = Teacher(**item)
                session.add(teacher)
                session.flush()
                created += 1
            else:
                for key, value in item.items():
                    setattr(teacher, key, value)
            provision_user(
                request.app.state.settings.auth_grpc_target,
                email=teacher.email,
                role="docente",
                profile_id=teacher.id,
                display_name=teacher.nombre,
            )
    return ok({"detectados": len(teachers), "creados": created, "preview": teachers[:8]}, "Docentes importados")


@app.get("/docentes")
def list_teachers(
    request: Request,
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    user=Depends(require_roles("admin", "docente")),
) -> dict:
    session_factory = request.app.state.session_factory
    with session_scope(session_factory) as session:
        items = session.scalars(select(Teacher).offset((page - 1) * limit).limit(limit)).all()
        return ok(
            [
                {
                    "id": item.id,
                    "nombre": item.nombre,
                    "email": item.email,
                    "ubicacion": item.ubicacion,
                    "extension": item.extension,
                }
                for item in items
            ]
        )


@app.post("/alumnos/importar/{materia_id}")
async def import_students(
    materia_id: int,
    request: Request,
    source_path: str | None = Form(None),
    file: UploadFile | None = File(None),
    user=Depends(require_roles("admin", "docente")),
) -> dict:
    if not subject_exists(request.app.state.settings.periods_grpc_target, materia_id):
        raise HTTPException(status_code=404, detail="Materia no encontrada")

    path = await resolve_input_file(upload=file, source_path=source_path)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        students = parse_students_from_pdf(extract_pdf_text(path))
    elif suffix == ".csv":
        students = parse_students_from_csv(path)
    elif suffix in {".xlsx", ".xlsm", ".xltx", ".xltm"}:
        students = parse_students_from_xlsx(path)
    else:
        raise HTTPException(status_code=400, detail="Formato no soportado")

    session_factory = request.app.state.session_factory
    imported = 0
    with session_scope(session_factory) as session:
        for item in students:
            email = item.get("email") or f"{item['matricula']}@alumno.agm.local"
            student = session.scalar(select(Student).where(Student.matricula == item["matricula"]))
            if student is None:
                student = Student(
                    matricula=item["matricula"],
                    nombre=item["nombre"],
                    email=email,
                    status=item.get("status", "Inscrito por Web"),
                    nivel=item.get("nivel", "Licenciatura"),
                )
                session.add(student)
                session.flush()
                imported += 1
            enrollment = session.scalar(
                select(Enrollment).where(Enrollment.student_id == student.id, Enrollment.materia_id == materia_id)
            )
            if enrollment is None:
                enrollment = Enrollment(student_id=student.id, materia_id=materia_id, activo=True, baja_count=0)
                session.add(enrollment)
            else:
                enrollment.activo = True
            _, temp_password = provision_user(
                request.app.state.settings.auth_grpc_target,
                email=student.email,
                role="alumno",
                profile_id=student.id,
                display_name=student.nombre,
            )
            send_welcome(
                request.app.state.settings.notifications_grpc_target,
                alumno_id=student.id,
                materia_id=materia_id,
                email=student.email,
                nombre=student.nombre,
                temporary_password=temp_password,
            )
    return ok(
        {
            "materia_id": materia_id,
            "alumnos_detectados": len(students),
            "alumnos_nuevos": imported,
            "preview": students[:10],
        },
        "Alumnos importados",
    )


@app.get("/alumnos/materia/{materia_id}")
def list_students_by_subject(
    materia_id: int,
    request: Request,
    user=Depends(require_roles("admin", "docente", "alumno")),
) -> dict:
    session_factory = request.app.state.session_factory
    with session_scope(session_factory) as session:
        enrollments = session.scalars(select(Enrollment).where(Enrollment.materia_id == materia_id)).all()
        rows = []
        for enrollment in enrollments:
            student = session.get(Student, enrollment.student_id)
            if student is not None:
                rows.append(student_to_dict(student, enrollment))
        return ok(rows)


@app.delete("/alumnos/{alumno_id}/baja")
def baja_student(
    alumno_id: int,
    request: Request,
    materia_id: int = Query(...),
    motivo: str = Query("Baja solicitada por el alumno"),
    user=Depends(require_roles("admin", "alumno")),
) -> dict:
    if user["role"] == "alumno" and user["profile_id"] != alumno_id:
        raise HTTPException(status_code=403, detail="Solo puedes solicitar tu propia baja")
    session_factory = request.app.state.session_factory
    with session_scope(session_factory) as session:
        enrollment = session.scalar(
            select(Enrollment).where(Enrollment.student_id == alumno_id, Enrollment.materia_id == materia_id)
        )
        if enrollment is None:
            raise HTTPException(status_code=404, detail="Inscripción no encontrada")
        if enrollment.baja_count >= 1:
            raise HTTPException(status_code=400, detail="La baja ya fue utilizada")
        enrollment.activo = False
        enrollment.baja_count += 1
        try:
            with grpc.insecure_channel(request.app.state.settings.periods_grpc_target) as channel:
                stub = periods_pb2_grpc.PeriodsServiceStub(channel)
                materia = stub.GetMateriaById(periods_pb2.MateriaIdRequest(materia_id=materia_id))
            with grpc.insecure_channel(request.app.state.settings.notifications_grpc_target) as channel:
                stub = notifications_pb2_grpc.NotificationsServiceStub(channel)
                stub.SendBajaNotif(
                    notifications_pb2.BajaRequest(
                        alumno_id=alumno_id,
                        docente_id=materia.docente_id,
                        motivo=motivo,
                    )
                )
        except grpc.RpcError:
            pass
        return ok(None, "Baja registrada")
