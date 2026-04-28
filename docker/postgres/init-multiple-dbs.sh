#!/bin/bash
set -e

dbs=(
  "agm_auth_db"
  "agm_periods_db"
  "agm_academics_db"
  "agm_grades_db"
  "agm_attendance_db"
  "agm_notifications_db"
  "agm_reports_db"
)

for db in "${dbs[@]}"; do
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    SELECT 'CREATE DATABASE $db'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '$db')\gexec
EOSQL
done
