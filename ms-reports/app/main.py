from io import BytesIO

import grpc
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle
from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from proto_generated import academics_pb2, academics_pb2_grpc
from proto_generated import attendance_pb2, attendance_pb2_grpc
from proto_generated import grades_pb2, grades_pb2_grpc
from proto_generated import periods_pb2, periods_pb2_grpc
from proto_generated import reports_pb2, reports_pb2_grpc
from shared.app_common.auth import require_roles
from shared.app_common.config import BaseServiceSettings
from shared.app_common.database import Base, create_session_factory, session_scope
from shared.app_common.grpc_runtime import start_grpc_server
from shared.app_common.responses import ok


class Settings(BaseServiceSettings):
    app_name: str = "AGM Reportes & Estadísticas"
    service_slug: str = "ms-reports"
    rest_port: int = 8017
    grpc_port: int = 50057
    # ¡ADIÓS SQLITE! Apuntamos a la base de datos exclusiva de Reportes
    database_url: str = "postgresql+psycopg://agm:agm_dev_password@postgres:5432/agm_reports_db"
    
    # El agregador necesita conocer las direcciones gRPC de casi todo el sistema
    periods_grpc_target: str = "ms-periods:50052"
    grades_grpc_target: str = "ms-grades:50054"
    attendance_grpc_target: str = "ms-attendance:50055"
    academics_grpc_target: str = "ms-academics:50053"


class ReportLog(Base):
    __tablename__ = "report_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_type: Mapped[str] = mapped_column(String(50))
    materia_id: Mapped[int] = mapped_column(Integer, index=True)
    formato: Mapped[str] = mapped_column(String(20))
    filename: Mapped[str] = mapped_column(String(255))


def get_subject(target: str, materia_id: int):
    with grpc.insecure_channel(target) as channel:
        stub = periods_pb2_grpc.PeriodsServiceStub(channel)
        return stub.GetMateriaById(periods_pb2.MateriaIdRequest(materia_id=materia_id))


def get_grade_concentrado(target: str, materia_id: int):
    with grpc.insecure_channel(target) as channel:
        stub = grades_pb2_grpc.GradesServiceStub(channel)
        return list(stub.GetConcentrado(grades_pb2.MateriaIdRequest(materia_id=materia_id)).items)


def get_attendance_history(target: str, materia_id: int):
    with grpc.insecure_channel(target) as channel:
        stub = attendance_pb2_grpc.AttendanceServiceStub(channel)
        return list(stub.GetHistorialMateria(attendance_pb2.MateriaIdRequest(materia_id=materia_id)).items)


def build_grades_xlsx(subject, rows: list) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Calificaciones"
    sheet.append(["Materia", subject.nombre])
    sheet.append(["NRC", subject.nrc])
    sheet.append([])
    sheet.append(["Alumno ID", "Nombre", "Promedio real", "Promedio redondeado"])
    for row in rows:
        sheet.append([row.alumno_id, row.nombre, row.promedio_real, row.promedio_redondeado])
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def build_attendance_xlsx(subject, rows: list) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Asistencias"
    sheet.append(["Materia", subject.nombre])
    sheet.append(["NRC", subject.nrc])
    sheet.append([])
    sheet.append(["Sesión", "Alumno ID", "Fecha", "Estado"])
    for row in rows:
        sheet.append([row.session_id, row.student_id, row.fecha, row.estado])
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def build_pdf(title: str, headers: list[str], rows: list[list]) -> bytes:
    output = BytesIO()
    doc = SimpleDocTemplate(output, pagesize=letter)
    table = Table([headers] + rows)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ]
        )
    )
    doc.build([table])
    return output.getvalue()


def build_response(content: bytes, filename: str, mime_type: str) -> StreamingResponse:
    return StreamingResponse(
        BytesIO(content),
        media_type=mime_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class ReportsGrpcService(reports_pb2_grpc.ReportsServiceServicer):
    def __init__(self, session_factory: sessionmaker[Session], settings: Settings):
        self.session_factory = session_factory
        self.settings = settings

    def GenerateReport(self, request, context):
        if request.report_type == "calificaciones":
            subject = get_subject(self.settings.periods_grpc_target, request.materia_id)
            rows = get_grade_concentrado(self.settings.grades_grpc_target, request.materia_id)
            if request.format == "pdf":
                content = build_pdf(
                    f"Calificaciones {subject.nombre}",
                    ["Alumno ID", "Nombre", "Promedio real", "Promedio redondeado"],
                    [[row.alumno_id, row.nombre, row.promedio_real, row.promedio_redondeado] for row in rows],
                )
                return reports_pb2.FileBytes(content=content, filename=f"calificaciones_{request.materia_id}.pdf", mime_type="application/pdf")
            content = build_grades_xlsx(subject, rows)
            return reports_pb2.FileBytes(
                content=content,
                filename=f"calificaciones_{request.materia_id}.xlsx",
                mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        subject = get_subject(self.settings.periods_grpc_target, request.materia_id)
        rows = get_attendance_history(self.settings.attendance_grpc_target, request.materia_id)
        if request.format == "pdf":
            content = build_pdf(
                f"Asistencias {subject.nombre}",
                ["Sesión", "Alumno ID", "Fecha", "Estado"],
                [[row.session_id, row.student_id, row.fecha, row.estado] for row in rows],
            )
            return reports_pb2.FileBytes(content=content, filename=f"asistencias_{request.materia_id}.pdf", mime_type="application/pdf")
        content = build_attendance_xlsx(subject, rows)
        return reports_pb2.FileBytes(
            content=content,
            filename=f"asistencias_{request.materia_id}.xlsx",
            mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def GetHistorialDocente(self, request, context):
        with grpc.insecure_channel(self.settings.periods_grpc_target) as channel:
            periods_stub = periods_pb2_grpc.PeriodsServiceStub(channel)
            materias = list(periods_stub.GetMateriasByDocente(periods_pb2.DocenteIdRequest(docente_id=request.docente_id)).items)
        items = []
        for materia in materias:
            with grpc.insecure_channel(self.settings.grades_grpc_target) as channel:
                grades_stub = grades_pb2_grpc.GradesServiceStub(channel)
                stats = grades_stub.GetEstadisticasMateria(grades_pb2.MateriaIdRequest(materia_id=materia.materia_id))
            items.append(
                reports_pb2.StatsPeriodo(
                    periodo=f"Periodo {materia.periodo_id}",
                    materias=1,
                    promedio=stats.promedio_grupal,
                )
            )
        return reports_pb2.StatsPeriodoReply(items=items)


app = FastAPI(title="AGM Reportes & Estadísticas", version="0.1.0")
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
        lambda server: reports_pb2_grpc.add_ReportsServiceServicer_to_server(ReportsGrpcService(session_factory, settings), server),
    )


@app.on_event("shutdown")
def shutdown_event() -> None:
    grpc_server = getattr(app.state, "grpc_server", None)
    if grpc_server is not None:
        grpc_server.stop(grace=1)


@app.get("/health")
def health() -> dict:
    return ok({"service": "reports", "status": "ok"})


@app.get("/reportes/calificaciones/{materia_id}")
def report_grades(
    materia_id: int,
    request: Request,
    formato: str = Query("pdf"),
    user=Depends(require_roles("admin", "docente")),
):
    if formato not in {"pdf", "xls", "xlsx"}:
        raise HTTPException(status_code=400, detail="Formato no soportado")
    grpc_format = "xlsx" if formato in {"xls", "xlsx"} else "pdf"
    reply = ReportsGrpcService(request.app.state.session_factory, request.app.state.settings).GenerateReport(
        reports_pb2.GenerateReportRequest(report_type="calificaciones", materia_id=materia_id, format=grpc_format),
        None,
    )
    with session_scope(request.app.state.session_factory) as session:
        session.add(
            ReportLog(
                report_type="calificaciones",
                materia_id=materia_id,
                formato=grpc_format,
                filename=reply.filename,
            )
        )
    return build_response(reply.content, reply.filename, reply.mime_type)


@app.get("/reportes/asistencias/{materia_id}")
def report_attendance(
    materia_id: int,
    request: Request,
    formato: str = Query("pdf"),
    user=Depends(require_roles("admin", "docente")),
):
    if formato not in {"pdf", "xls", "xlsx"}:
        raise HTTPException(status_code=400, detail="Formato no soportado")
    grpc_format = "xlsx" if formato in {"xls", "xlsx"} else "pdf"
    reply = ReportsGrpcService(request.app.state.session_factory, request.app.state.settings).GenerateReport(
        reports_pb2.GenerateReportRequest(report_type="asistencias", materia_id=materia_id, format=grpc_format),
        None,
    )
    with session_scope(request.app.state.session_factory) as session:
        session.add(
            ReportLog(
                report_type="asistencias",
                materia_id=materia_id,
                formato=grpc_format,
                filename=reply.filename,
            )
        )
    return build_response(reply.content, reply.filename, reply.mime_type)


@app.get("/estadisticas/docente/{docente_id}")
def teacher_stats(docente_id: int, request: Request, user=Depends(require_roles("admin", "docente"))) -> dict:
    with grpc.insecure_channel(request.app.state.settings.periods_grpc_target) as channel:
        periods_stub = periods_pb2_grpc.PeriodsServiceStub(channel)
        materias = list(periods_stub.GetMateriasByDocente(periods_pb2.DocenteIdRequest(docente_id=docente_id)).items)
    rows = []
    for materia in materias:
        with grpc.insecure_channel(request.app.state.settings.grades_grpc_target) as channel:
            grades_stub = grades_pb2_grpc.GradesServiceStub(channel)
            grade_stats = grades_stub.GetEstadisticasMateria(grades_pb2.MateriaIdRequest(materia_id=materia.materia_id))
        with grpc.insecure_channel(request.app.state.settings.attendance_grpc_target) as channel:
            attendance_stub = attendance_pb2_grpc.AttendanceServiceStub(channel)
            attendance_stats = attendance_stub.GetEstadisticasAsistencia(
                attendance_pb2.MateriaIdRequest(materia_id=materia.materia_id)
            )
        rows.append(
            {
                "materia_id": materia.materia_id,
                "materia": materia.nombre,
                "promedio_grupal": grade_stats.promedio_grupal,
                "aprobacion": grade_stats.aprobacion,
                "sesiones": attendance_stats.total_sesiones,
                "asistencias": attendance_stats.asistencias,
                "retardos": attendance_stats.retardos,
            }
        )
    return ok(rows)


@app.get("/estadisticas/alumno/{alumno_id}")
def student_stats(alumno_id: int, request: Request, user=Depends(require_roles("admin", "alumno", "docente"))) -> dict:
    if user["role"] == "alumno" and user["profile_id"] != alumno_id:
        raise HTTPException(status_code=403, detail="Solo puedes consultar tus estadísticas")
    with grpc.insecure_channel(request.app.state.settings.periods_grpc_target) as channel:
        periods_stub = periods_pb2_grpc.PeriodsServiceStub(channel)
        active = periods_stub.GetPeriodoActivo(periods_pb2.Empty())
        materias = list(periods_stub.GetMateriasByPeriodo(periods_pb2.PeriodoIdRequest(periodo_id=active.periodo_id)).items)
    rows = []
    for materia in materias:
        with grpc.insecure_channel(request.app.state.settings.academics_grpc_target) as channel:
            academics_stub = academics_pb2_grpc.AcademicsServiceStub(channel)
            enrolled = academics_stub.IsAlumnoEnMateria(
                academics_pb2.AlumnoMateriaRequest(alumno_id=alumno_id, materia_id=materia.materia_id)
            )
        if not enrolled.ok:
            continue
        with grpc.insecure_channel(request.app.state.settings.grades_grpc_target) as channel:
            grades_stub = grades_pb2_grpc.GradesServiceStub(channel)
            promedio = grades_stub.GetPromedioAlumno(
                grades_pb2.AlumnoMateriaRequest(alumno_id=alumno_id, materia_id=materia.materia_id)
            )
        with grpc.insecure_channel(request.app.state.settings.attendance_grpc_target) as channel:
            attendance_stub = attendance_pb2_grpc.AttendanceServiceStub(channel)
            asistencias = attendance_stub.GetAsistenciaAlumno(
                attendance_pb2.AlumnoMateriaRequest(alumno_id=alumno_id, materia_id=materia.materia_id)
            )
        rows.append(
            {
                "materia_id": materia.materia_id,
                "materia": materia.nombre,
                "promedio_actual": promedio.value,
                "asistencias": len(asistencias.items),
            }
        )
    return ok(rows)
