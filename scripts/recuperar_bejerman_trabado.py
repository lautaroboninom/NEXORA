"""Recuperación puntual: PDFs que nunca llegaron y jobs de sincronización trabados.

Se corre desde la consola de SERVERDATA, en una línea:

    wsl -d Ubuntu-24.04 -- docker exec -i nexora-api python manage.py shell < "/mnt/e/Datos/Servicio Tecnico/1_SISTEMA REPARACIONES/NEXORA/scripts/recuperar_bejerman_trabado.py"

No necesita rebuild: usa código que ya está desplegado. Es idempotente — volver a
correrlo no hace daño, simplemente no encuentra nada para arreglar.

Los equipos de TERAPIAS MÉDICAS quedan afuera a propósito: su problema de stock
(artículo 1115001 rechazado en STC) es otro y sigue sin resolverse.

Lo que NO hace, porque no se puede hacer sin decidir por una persona:
  - Los remitos en revisión se destraban desde el panel de Bejerman.
  - OXL necesita que le carguen la situación de IVA en su ficha de Bejerman.
"""

from django.db import connection

from service.bejerman_pdf_requests import requeue_pdf_request
from service.bejerman_user_credentials import (
    BejermanUserCredentialsError,
    get_user_bejerman_credentials,
)

CLIENTE_EXCLUIDO = "%TERAPIAS MEDICAS%"
# Igual que el claim del worker: BEJERMAN_MAX_ATTEMPTS contra la ventana ampliada
# para bloqueos transitorios del SDK. Un job con más intentos que esto ya no lo
# toma nadie, por más que su estado siga diciendo "pending".
MAX_INTENTOS = 12
RUNNING_HUERFANO_MINUTOS = 30


def separador(titulo):
    print("")
    print(f"=== {titulo} ===")


# ---------------------------------------------------------------- PDFs
separador("PDFs que nunca llegaron")
with connection.cursor() as cur:
    cur.execute(
        "SELECT document_key FROM bejerman_pdf_requests WHERE state='failed' ORDER BY created_at DESC"
    )
    claves = [row[0] for row in cur.fetchall()]

print(f"Fallidos encontrados: {len(claves)}")
resumen_pdf = {}
for clave in claves:
    try:
        # force=True porque el contador de regeneraciones quedó gastado en el ciclo
        # viejo, cuando el sondeo todavía no miraba los otros nombres de archivo.
        resultado = requeue_pdf_request(clave, reason="recuperacion_manual_20260813", force=True)
        estado = resultado.get("outcome")
    except Exception as exc:
        estado = f"ERROR: {exc}"
    resumen_pdf[estado] = resumen_pdf.get(estado, 0) + 1
    print(f"  {clave}: {estado}")
print(f"Resumen PDFs: {resumen_pdf}")
print("  ('ready' = el archivo ya estaba en la carpeta del ERP con otro nombre;")
print("   'requeued' = se le vuelve a pedir a Bejerman, el worker lo sigue)")


# ------------------------------------------------- Jobs: running huérfanos
separador("Jobs 'running' sin nadie atrás")
with connection.cursor() as cur:
    cur.execute(
        """
        UPDATE bejerman_sync_jobs j
           SET status = 'pending',
               next_attempt_at = CURRENT_TIMESTAMP,
               last_error = 'Se reanudó: el proceso que lo estaba ejecutando no terminó (reinicio o caída).',
               updated_at = CURRENT_TIMESTAMP
          FROM ingresos i
          LEFT JOIN devices d ON d.id = i.device_id
          LEFT JOIN customers cu ON cu.id = d.customer_id
         WHERE i.id = j.ingreso_id
           AND j.status = 'running'
           AND j.updated_at <= CURRENT_TIMESTAMP - (%s * INTERVAL '1 minute')
           AND j.attempts < %s
           AND COALESCE(cu.razon_social, '') NOT ILIKE %s
        RETURNING j.id
        """,
        [RUNNING_HUERFANO_MINUTOS, MAX_INTENTOS, CLIENTE_EXCLUIDO],
    )
    reanudados = [row[0] for row in cur.fetchall()]
print(f"Reanudados: {reanudados or 'ninguno'}")


# --------------------------------------- Jobs: pending con intentos agotados
separador("Jobs 'pending' que ya nadie puede tomar")
with connection.cursor() as cur:
    cur.execute(
        """
        UPDATE bejerman_sync_jobs j
           SET status = 'blocked',
               last_error = COALESCE(
                 NULLIF(j.last_error, ''),
                 'Máximos reintentos agotados sin un error registrado.'
               ),
               updated_at = CURRENT_TIMESTAMP
          FROM ingresos i
          LEFT JOIN devices d ON d.id = i.device_id
          LEFT JOIN customers cu ON cu.id = d.customer_id
         WHERE i.id = j.ingreso_id
           AND j.status IN ('pending', 'running')
           AND j.attempts >= %s
           AND j.next_attempt_at <= CURRENT_TIMESTAMP
           AND COALESCE(cu.razon_social, '') NOT ILIKE %s
        RETURNING j.id
        """,
        [MAX_INTENTOS, CLIENTE_EXCLUIDO],
    )
    reclasificados = [row[0] for row in cur.fetchall()]
print(f"Pasados a 'blocked' (dejan de ser invisibles): {reclasificados or 'ninguno'}")


# ------------------------------------ Jobs bloqueados por falta de credenciales
separador("Jobs bloqueados por credenciales de Bejerman")
with connection.cursor() as cur:
    cur.execute(
        """
        SELECT j.id, j.actor_user_id, u.nombre
          FROM bejerman_sync_jobs j
          JOIN users u ON u.id = j.actor_user_id
         WHERE j.status = 'blocked'
           AND j.last_error ILIKE '%credenciales de Bejerman%'
         ORDER BY j.id
        """
    )
    candidatos = cur.fetchall()

reabiertos = []
sin_credenciales = {}
for job_id, user_id, nombre in candidatos:
    try:
        get_user_bejerman_credentials(int(user_id))
    except BejermanUserCredentialsError:
        sin_credenciales[nombre] = sin_credenciales.get(nombre, 0) + 1
        continue
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE bejerman_sync_jobs
               SET status = 'pending', attempts = 0, last_error = NULL,
                   next_attempt_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
             WHERE id = %s AND status = 'blocked'
            RETURNING id
            """,
            [job_id],
        )
        if cur.fetchone():
            reabiertos.append(job_id)

print(f"Reabiertos (su usuario ya cargó las credenciales): {reabiertos or 'ninguno'}")
if sin_credenciales:
    print("Siguen esperando a que su usuario cargue las credenciales en su perfil:")
    for nombre, cantidad in sorted(sin_credenciales.items()):
        print(f"  - {nombre}: {cantidad} equipo(s)")


separador("Estado final")
with connection.cursor() as cur:
    cur.execute("SELECT state, COUNT(*) FROM bejerman_pdf_requests GROUP BY 1 ORDER BY 2 DESC")
    print("PDFs:", dict(cur.fetchall()))
    cur.execute("SELECT status, COUNT(*) FROM bejerman_sync_jobs GROUP BY 1 ORDER BY 2 DESC")
    print("Jobs:", dict(cur.fetchall()))
