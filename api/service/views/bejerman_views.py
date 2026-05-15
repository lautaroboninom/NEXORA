from django.db import connection
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from ..bejerman_sync import (
    article_context_for_ingreso,
    reopen_jobs_for_article_mapping,
    upsert_article_mapping,
)
from ..permissions import require_any_permission, require_permission
from .helpers import q


def _as_int(value):
    try:
        return int(str(value).strip())
    except Exception:
        return None


def _build_job_filters(params, *, include_status=True):
    where = []
    sql_params = []

    if include_status:
        status = (params.get("status") or "").strip()
        if status:
            where.append("j.status = %s")
            sql_params.append(status)

    sync_type = (params.get("sync_type") or params.get("type") or "").strip()
    if sync_type:
        where.append("j.sync_type = %s")
        sql_params.append(sync_type)

    os_value = _as_int(params.get("os") or params.get("ingreso_id"))
    if os_value:
        where.append("j.ingreso_id = %s")
        sql_params.append(os_value)

    serial = (params.get("serie") or params.get("serial") or "").strip()
    if serial:
        where.append("j.numero_serie ILIKE %s")
        sql_params.append(f"%{serial}%")

    cliente = (params.get("cliente") or "").strip()
    if cliente:
        where.append("c.razon_social ILIKE %s")
        sql_params.append(f"%{cliente}%")

    article = (params.get("articulo") or params.get("article") or "").strip()
    if article:
        where.append("j.article_code ILIKE %s")
        sql_params.append(f"%{article}%")

    search = (params.get("q") or "").strip()
    if search:
        where.append(
            """
            (
              CAST(j.ingreso_id AS TEXT) ILIKE %s
              OR j.numero_serie ILIKE %s
              OR COALESCE(d.numero_interno, '') ILIKE %s
              OR COALESCE(c.razon_social, '') ILIKE %s
              OR COALESCE(j.article_code, '') ILIKE %s
              OR COALESCE(j.last_error, '') ILIKE %s
            )
            """
        )
        like = f"%{search}%"
        sql_params.extend([like, like, like, like, like, like])

    return where, sql_params


class BejermanJobsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_any_permission(request, ["page.bejerman_sync", "page.logistics"])
        where, sql_params = _build_job_filters(request.GET, include_status=True)
        where_sql = "WHERE " + " AND ".join(where) if where else ""
        rows = q(
            f"""
            SELECT j.id,
                   j.sync_type,
                   j.ingreso_id,
                   j.device_id,
                   j.ingreso_event_id,
                   j.numero_serie,
                   j.source_deposit,
                   j.target_deposit,
                   j.article_code,
                   j.status,
                   j.attempts,
                   j.next_attempt_at,
                   j.last_error,
                   j.request_payload,
                   j.response_payload,
                   j.created_at,
                   j.updated_at,
                   COALESCE(d.numero_interno, '') AS numero_interno,
                   COALESCE(c.razon_social, '') AS cliente,
                   COALESCE(b.nombre, '') AS marca,
                   COALESCE(m.nombre, '') AS modelo,
                   COALESCE(NULLIF(t.equipo_variante, ''), NULLIF(d.variante, ''), NULLIF(m.variante, ''), '') AS variante
              FROM bejerman_sync_jobs j
              JOIN ingresos t ON t.id = j.ingreso_id
              JOIN devices d ON d.id = j.device_id
              LEFT JOIN customers c ON c.id = d.customer_id
              LEFT JOIN marcas b ON b.id = d.marca_id
              LEFT JOIN models m ON m.id = d.model_id
              {where_sql}
             ORDER BY
               CASE j.status
                 WHEN 'blocked' THEN 1
                 WHEN 'failed' THEN 2
                 WHEN 'pending' THEN 3
                 WHEN 'running' THEN 4
                 ELSE 5
               END,
               j.updated_at DESC,
               j.id DESC
             LIMIT 250
            """,
            sql_params,
        ) or []

        counter_where, counter_params = _build_job_filters(request.GET, include_status=False)
        counter_sql = "WHERE " + " AND ".join(counter_where) if counter_where else ""
        counters = q(
            f"""
            SELECT j.status, COUNT(*) AS count
              FROM bejerman_sync_jobs j
              JOIN ingresos t ON t.id = j.ingreso_id
              JOIN devices d ON d.id = j.device_id
              LEFT JOIN customers c ON c.id = d.customer_id
              LEFT JOIN marcas b ON b.id = d.marca_id
              LEFT JOIN models m ON m.id = d.model_id
              {counter_sql}
             GROUP BY j.status
            """,
            counter_params,
        ) or []
        return Response(
            {
                "items": rows,
                "counters": {row["status"]: int(row["count"]) for row in counters},
            }
        )


class BejermanJobRetryView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, job_id: int):
        require_permission(request, "action.bejerman_sync.manage")
        with connection.cursor() as cur:
            cur.execute("SELECT id, status FROM bejerman_sync_jobs WHERE id=%s", [job_id])
            row = cur.fetchone()
            if not row:
                return Response({"detail": "Operación Bejerman no encontrada"}, status=404)
            status = (row[1] or "").strip()
            if status not in ("failed", "blocked"):
                return Response({"detail": "Solo se pueden reintentar operaciones fallidas o bloqueadas"}, status=409)
            cur.execute(
                """
                UPDATE bejerman_sync_jobs
                   SET status = 'pending',
                       attempts = 0,
                       last_error = NULL,
                       next_attempt_at = CURRENT_TIMESTAMP,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE id = %s
                RETURNING id, status, attempts
                """,
                [job_id],
            )
            cols = [col[0] for col in cur.description]
            updated = dict(zip(cols, cur.fetchone()))
        return Response(updated)


class BejermanArticleMappingsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        require_permission(request, "action.bejerman_sync.manage")
        data = request.data or {}
        article_code = (data.get("article_code") or data.get("codigo") or "").strip()
        article_description = (data.get("article_description") or data.get("descripcion") or "").strip()
        if not article_code:
            return Response({"detail": "Código de artículo Bejerman requerido"}, status=400)

        model_id = _as_int(data.get("model_id"))
        variante = (data.get("variante") or "").strip()
        job_id = _as_int(data.get("job_id"))
        if job_id:
            row = q("SELECT ingreso_id FROM bejerman_sync_jobs WHERE id=%s", [job_id], one=True)
            if not row:
                return Response({"detail": "Operación Bejerman no encontrada"}, status=404)
            context = article_context_for_ingreso(int(row["ingreso_id"]))
            model_id = int(context["model_id"])
            variante = context.get("variante") or ""

        if not model_id:
            return Response({"detail": "Modelo requerido para mapear el artículo"}, status=400)

        user_id = getattr(getattr(request, "user", None), "id", None) or getattr(request, "user_id", None)
        mapping = upsert_article_mapping(
            model_id=model_id,
            variante=variante,
            article_code=article_code,
            article_description=article_description,
            match_source="manual",
            confirmed_by=user_id,
        )
        reopened = reopen_jobs_for_article_mapping(model_id, variante, article_code)
        return Response({"mapping": mapping, "reopened_jobs": reopened})


__all__ = [
    "BejermanJobsView",
    "BejermanJobRetryView",
    "BejermanArticleMappingsView",
]
