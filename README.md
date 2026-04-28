# AGM Backend Microservices

Primera entrega funcional del backend del proyecto **AGM - Academic Grade Management** enfocada en lo evaluable del PDF: microservicios reales, gRPC entre servicios, JWT/RBAC, importacion de PDFs/CSV/XLSX, asistencias QR, notificaciones y reportes.

## Arquitectura

- `ms-auth`: login, JWT, RBAC, recuperacion de contrasena y provision automatica de usuarios.
- `ms-periods`: CRUD de periodos e importacion del PDF de programacion academica.
- `ms-academics`: importacion del directorio docente y alumnos por materia desde PDF/CSV/XLSX.
- `ms-grades`: ponderaciones, actividades, calificaciones e importacion masiva.
- `ms-attendance`: sesiones de asistencia, QR firmado y registro `Presente` / `Retardo`.
- `ms-notifications`: bitacora de correos y envio SMTP opcional.
- `ms-reports`: exportacion XLSX/PDF y estadisticas para docente y alumno.
- `proto/`: contratos gRPC compartidos.
- `proto_generated/`: codigo generado desde `.proto`.

Cada microservicio corre en su propio puerto REST y en su propio puerto gRPC. En Docker, el stack ya usa PostgreSQL con una base independiente por servicio dentro de la misma instancia (`agm_auth_db`, `agm_periods_db`, `agm_academics_db`, `agm_grades_db`, `agm_attendance_db`, `agm_notifications_db`, `agm_reports_db`). Si corres sin Docker, el codigo conserva SQLite como fallback local para desarrollo rapido.

## Puertos

- `ms-auth`: REST `8011`, gRPC `50051`
- `ms-periods`: REST `8012`, gRPC `50052`
- `ms-academics`: REST `8013`, gRPC `50053`
- `ms-grades`: REST `8014`, gRPC `50054`
- `ms-attendance`: REST `8015`, gRPC `50055`
- `ms-notifications`: REST `8016`, gRPC `50056`
- `ms-reports`: REST `8017`, gRPC `50057`

## Ejecutar local sin Docker

1. Instala dependencias:

```powershell
python -m pip install -r requirements.txt
```

2. Inicia todos los servicios:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start_local.ps1
```

3. Verifica salud:

```powershell
Invoke-RestMethod http://127.0.0.1:8011/health
Invoke-RestMethod http://127.0.0.1:8012/health
Invoke-RestMethod http://127.0.0.1:8013/health
Invoke-RestMethod http://127.0.0.1:8014/health
Invoke-RestMethod http://127.0.0.1:8015/health
Invoke-RestMethod http://127.0.0.1:8016/health
Invoke-RestMethod http://127.0.0.1:8017/health
```

4. Deten todos los servicios:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\stop_local.ps1
```

Si tu sistema no encuentra `python`, define antes:

```powershell
$env:AGM_PYTHON="C:\ruta\a\python.exe"
```

## Ejecutar con Docker

```powershell
docker compose up --build
```

Si ya habias levantado una version previa del stack, te conviene recrear limpio para que se creen correctamente las bases PostgreSQL:

```powershell
docker compose down -v
docker compose up --build
```

## PostgreSQL visual

- PostgreSQL: `localhost:5432`
- Usuario: `agm`
- Password: `agm_dev_password`
- pgAdmin: [http://127.0.0.1:5050](http://127.0.0.1:5050)
- Login pgAdmin: `admin@agm.local`
- Password pgAdmin: `Admin123!`

Desde pgAdmin puedes registrar el servidor `postgres` o `host.docker.internal` segun desde donde te conectes y revisar que cada microservicio tiene su propia base separada.

## Flujo minimo de prueba

1. Inicia sesion con el admin sembrado por defecto.

```powershell
$login = Invoke-RestMethod http://127.0.0.1:8011/auth/login -Method Post -ContentType "application/json" -Body '{"email":"admin@agm.local","password":"Admin123!"}'
$token = $login.data.access_token
$headers = @{ Authorization = "Bearer $token" }
```

2. Importa docentes desde el PDF que me compartiste.

```powershell
Invoke-RestMethod http://127.0.0.1:8013/docentes/importar -Method Post -Headers $headers -Form @{
  source_path = 'C:\Users\ferbe\Downloads\Personal Docente - FCC BUAP.pdf'
}
```

3. Importa la programacion academica.

```powershell
Invoke-RestMethod http://127.0.0.1:8012/periodos/importar -Method Post -Headers $headers -Form @{
  source_path = 'C:\Users\ferbe\Downloads\MATERIAS_PA_PRIMAVERA_2026_CU_SAN_MANUEL_ITI.pdf'
}
```

4. Consulta materias y toma el `id` de la que quieras trabajar.

```powershell
Invoke-RestMethod "http://127.0.0.1:8012/materias?page=1&limit=10" -Headers $headers
```

5. Importa alumnos a la materia elegida.

```powershell
Invoke-RestMethod http://127.0.0.1:8013/alumnos/importar/1 -Method Post -Headers $headers -Form @{
  source_path = 'C:\Users\ferbe\Downloads\ListaAlumnos_Servicios_Web.pdf'
}
```

6. Configura ponderaciones.

```powershell
Invoke-RestMethod http://127.0.0.1:8014/ponderaciones/1 -Method Post -Headers $headers -ContentType "application/json" -Body '{
  "items": [
    { "nombre": "Examenes", "porcentaje": 40 },
    { "nombre": "Tareas", "porcentaje": 30 },
    { "nombre": "Proyecto", "porcentaje": 20 },
    { "nombre": "Asistencia", "porcentaje": 10 }
  ]
}'
```

7. Genera reportes:

```powershell
Invoke-WebRequest "http://127.0.0.1:8017/reportes/calificaciones/1?formato=pdf" -Headers $headers -OutFile .\calificaciones.pdf
Invoke-WebRequest "http://127.0.0.1:8017/reportes/asistencias/1?formato=xlsx" -Headers $headers -OutFile .\asistencias.xlsx
```

## Pruebas automaticas

```powershell
pytest
```

## Estado actual

- Ya cubre los 7 microservicios obligatorios.
- Ya usa gRPC real entre servicios.
- Ya importa los tres tipos de insumo relevantes para tu proyecto.
- Ya tiene exportacion real de reportes.
- Ya tiene Dockerfiles individuales y `docker-compose.yml`.

## Siguientes mejoras recomendadas

- Migrar las bases SQLite a PostgreSQL para una defensa mas fuerte del proyecto.
- Endurecer autenticacion con refresh tokens persistentes y logout invalidando tokens.
- Agregar API Gateway y coleccion Postman.
- Afinar mas el parser del PDF de programacion para cubrir otros formatos del mismo documento.
- Agregar mas endpoints de consulta para dashboard y cierre final de materia.
