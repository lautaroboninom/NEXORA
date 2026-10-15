FROM postgres:16
COPY sql/schema.sql /docker-entrypoint-initdb.d/001-schema.sql
