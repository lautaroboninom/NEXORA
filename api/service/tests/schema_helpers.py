"""Preparación explícita del esquema real para pruebas de integración.

Los modelos operativos son unmanaged: migrate no crea sus tablas. Esta función
se usa exclusivamente después de que el runner creó su base descartable.
"""

from pathlib import Path
from uuid import uuid4

import sqlparse
from django.db import connection


def apply_application_schema():
    name = str(connection.settings_dict["NAME"])
    if not name.startswith("test_"):
        raise RuntimeError("El esquema de pruebas requiere una base test_ descartable")
    schema = Path(__file__).resolve().parents[3] / "sql" / "schema.sql"
    with connection.cursor() as cur:
        try:
            for statement in sqlparse.split(schema.read_text(encoding="utf-8-sig")):
                if statement.strip():
                    cur.execute(statement)
        finally:
            # El script SQL usa hora argentina en su sesión de instalación;
            # el adaptador de Django espera la zona configurada en la conexión.
            cur.execute("SET TIME ZONE %s", [connection.timezone_name])


def create_fixture_device():
    """Equipo sintético con sus relaciones obligatorias, dentro de una prueba."""
    if not str(connection.settings_dict["NAME"]).startswith("test_"):
        raise RuntimeError("Los datos de prueba requieren una base test_ descartable")
    key = uuid4().hex
    with connection.cursor() as cur:
        cur.execute("INSERT INTO customers(razon_social) VALUES (%s) RETURNING id", [f"Cliente {key}"])
        customer_id = cur.fetchone()[0]
        cur.execute("INSERT INTO marcas(nombre) VALUES (%s) RETURNING id", [f"Marca {key}"])
        marca_id = cur.fetchone()[0]
        cur.execute("INSERT INTO models(marca_id, nombre) VALUES (%s, %s) RETURNING id", [marca_id, "Modelo"])
        model_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO devices(customer_id, marca_id, model_id, numero_serie)
            VALUES (%s, %s, %s, %s) RETURNING id
        """, [customer_id, marca_id, model_id, f"AUDIT-{key}"])
        return cur.fetchone()[0]
