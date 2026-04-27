import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "proto_generated"))


def load_module(relative_path: str, module_name: str):
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


periods = load_module("ms-periods/app/main.py", "ms_periods_main")
academics = load_module("ms-academics/app/main.py", "ms_academics_main")
attendance = load_module("ms-attendance/app/main.py", "ms_attendance_main")


def test_parse_schedule_pdf_groups_rows_by_nrc_and_section():
    raw = """
    Facultad de Ciencias de la Computacion
    INGENIERIA EN TECNOLOGIAS DE LA INFORMACION - CAMPUS CU San Manuel
    PROGRAMACION ACADEMICA - PRIMAVERA 2026
    50130 ITIS 260 Servicios Web 001 M 1300-1459 MENDEZ - SANCHEZ LUIS YAEL CCO3/205
    50130 ITIS 260 Servicios Web 001 J 1300-1459 MENDEZ - SANCHEZ LUIS YAEL CCO3/205
    50131 ITIS 260 Servicios Web 002 V 1500-1659 MENDOZA - OLGUIN GUSTAVO CCO3/205
    """
    parsed = periods.parse_schedule_pdf(raw)
    assert parsed["periodo_nombre"] == "Primavera 2026"
    assert len(parsed["materias"]) == 2
    first = parsed["materias"][0]
    assert first["nrc"] == "50130"
    assert len(first["horario"]) == 2


def test_parse_teacher_and_student_pdfs_extract_expected_rows():
    teachers_raw = """
    Mendez Sanchez Luis Yael luis.mendezsanchez@correo.buap.mx CCO3-205
    Guerrero Garcia Josefina josefina.guerrero@correo.buap.mx CCO3-007A 3923
    """
    students_raw = """
    Resumen de Lista de Clase
    1 AGUILAR SALDIVAR, ANGEL G. 202224429 **Inscrito por Web** Licenciatura
    2 AMADOR LAGUNES, ALEJANDRO 202213377 **Inscrito por Web** Licenciatura
    """
    teachers = academics.parse_teacher_pdf(teachers_raw)
    students = academics.parse_students_from_pdf(students_raw)
    assert len(teachers) == 2
    assert teachers[0]["email"] == "luis.mendezsanchez@correo.buap.mx"
    assert len(students) == 2
    assert students[0]["matricula"] == "202224429"


def test_qr_token_roundtrip_is_valid():
    token = attendance.build_qr_token("secret", 10, 20, 30, 123456)
    decoded = attendance.decode_qr_token("secret", token)
    assert decoded == {"alumno_id": 10, "materia_id": 20, "session_id": 30, "issued_at": 123456}
