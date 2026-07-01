from urllib.parse import quote
import logging

from django.conf import settings
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from django.db import connection, transaction
from django.http import FileResponse
from django.utils import timezone
from rest_framework import permissions
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .helpers import q, exec_void, exec_returning, _set_audit_user
from ..serializers import IngresoMediaItemSerializer
from ..media_utils import (
    process_upload_any,
    save_processed_media,
    delete_media_paths,
    MediaValidationError,
    MediaProcessingError,
    _make_placeholder_thumb,
    _make_pdf_thumb,
)

logger = logging.getLogger(__name__)

MEDIA_VIEW_ROLES = {"jefe", "admin", "supervisor", "jefe_veedor", "recepcion", "tecnico"}
MEDIA_MANAGE_ROLES = {"jefe", "admin", "supervisor", "jefe_veedor", "tecnico"}

MEDIA_SELECT_BASE = """
    SELECT
      im.id, im.ingreso_id, im.usuario_id,
      COALESCE(u.nombre, '') AS usuario_nombre,
      im.comentario, im.mime_type, im.size_bytes, im.width, im.height,
      im.original_name, im.storage_path, im.thumbnail_path,
      im.created_at, im.updated_at
    FROM ingreso_media im
    LEFT JOIN users u ON u.id = im.usuario_id
"""


def _fetch_media_row(ingreso_id: int, media_id: int):
    sql = MEDIA_SELECT_BASE + " WHERE im.ingreso_id=%s AND im.id=%s"
    return q(sql, [ingreso_id, media_id], one=True)


def _fetch_media_page(ingreso_id: int, limit: int, offset: int):
    sql = (
        MEDIA_SELECT_BASE
        + " WHERE im.ingreso_id=%s"
        + " ORDER BY im.created_at DESC, im.id DESC"
        + " LIMIT %s OFFSET %s"
    )
    return q(sql, [ingreso_id, limit, offset])


def _current_user_id(request):
    uid = getattr(getattr(request, "user", None), "id", None)
    if uid is not None:
        return uid
    try:
        return int(getattr(request, "user_id", None))
    except (TypeError, ValueError):
        return None


def _fetch_ingreso_assignation(ingreso_id: int):
    return q("SELECT id, asignado_a FROM ingresos WHERE id=%s", [ingreso_id], one=True)


def _rol(request):
    return getattr(request.user, "rol", None) or (getattr(request.user, "data", {}) or {}).get("rol")


def _ensure_media_view_perm(request, ingreso_row):
    rol = _rol(request)
    if rol in MEDIA_VIEW_ROLES - {"tecnico"}:
        return
    if rol == "tecnico" and ingreso_row:
        return
    from rest_framework.exceptions import PermissionDenied
    raise PermissionDenied("No autorizado para ver fotos del ingreso")


def _ensure_media_manage_perm(request, ingreso_row):
    rol = _rol(request)
    if rol in MEDIA_MANAGE_ROLES - {"tecnico"}:
        return
    if rol == "tecnico" and ingreso_row and ingreso_row.get("asignado_a") == _current_user_id(request):
        return
    from rest_framework.exceptions import PermissionDenied
    raise PermissionDenied("No autorizado para gestionar fotos del ingreso")


def _serialize_media_row(row, ingreso_id: int, request=None):
    base_path = f"/api/ingresos/{ingreso_id}/fotos/{row['id']}"
    item = {
        "id": row.get("id"),
        "ingreso_id": ingreso_id,
        "usuario_id": row.get("usuario_id"),
        "usuario_nombre": row.get("usuario_nombre", ""),
        "comentario": row.get("comentario"),
        "mime_type": row.get("mime_type"),
        "size_bytes": row.get("size_bytes"),
        "width": row.get("width"),
        "height": row.get("height"),
        "original_name": row.get("original_name"),
        "url": base_path + "/archivo/",
        "thumbnail_url": base_path + "/miniatura/",
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }
    return IngresoMediaItemSerializer(item).data


class IngresoMediaListCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def get(self, request, ingreso_id: int):
        ingreso_row = _fetch_ingreso_assignation(ingreso_id)
        if not ingreso_row:
            return Response({"detail": "Ingreso no encontrado"}, status=404)
        _ensure_media_view_perm(request, ingreso_row)

        try:
            page = int(request.GET.get("page", "1"))
        except ValueError:
            page = 1
        if page < 1:
            page = 1

        raw_page_size = request.GET.get("page_size") or request.GET.get("pageSize") or "20"
        try:
            page_size = int(raw_page_size)
        except ValueError:
            page_size = 20

        max_files = int(getattr(settings, "INGRESO_MEDIA_MAX_FILES", 50) or 50)
        max_page_size = min(max_files, 100)
        page_size = max(1, min(page_size, max_page_size))
        offset = (page - 1) * page_size

        total_row = q("SELECT COUNT(*) AS c FROM ingreso_media WHERE ingreso_id=%s", [ingreso_id], one=True) or {}
        try:
            total = int(total_row.get("c") or 0)
        except (TypeError, ValueError):
            total = 0

        rows = _fetch_media_page(ingreso_id, page_size, offset) or []
        items = [_serialize_media_row(r, ingreso_id, request) for r in rows]

        max_size_mb = int(getattr(settings, "INGRESO_MEDIA_MAX_SIZE_MB", 10) or 10)
        thumb_max = int(getattr(settings, "INGRESO_MEDIA_THUMB_MAX", 512) or 512)
        remaining = max(0, max_files - min(max_files, total))
        has_next = (page * page_size) < total
        has_prev = page > 1

        payload = {
            "page": page,
            "page_size": page_size,
            "total": total,
            "items": items,
            # Alias para compatibilidad con frontends que esperan `results`
            "results": items,
            "has_next": has_next,
            "has_prev": has_prev,
            "limits": {
                "max_files": max_files,
                "max_size_mb": max_size_mb,
                "thumb_max": thumb_max,
            },
            "remaining_slots": remaining,
        }
        return Response(payload)

    def post(self, request, ingreso_id: int):
        ingreso_row = _fetch_ingreso_assignation(ingreso_id)
        if not ingreso_row:
            return Response({"detail": "Ingreso no encontrado"}, status=404)
        _ensure_media_manage_perm(request, ingreso_row)

        uploads = []
        for key in request.FILES:
            uploads.extend(request.FILES.getlist(key) or [])
        if not uploads:
            return Response({"detail": "No se recibieron archivos"}, status=400)

        max_files = int(getattr(settings, "INGRESO_MEDIA_MAX_FILES", 50) or 50)
        existing_row = q("SELECT COUNT(*) AS c FROM ingreso_media WHERE ingreso_id=%s", [ingreso_id], one=True) or {}
        existing_count = int(existing_row.get("c") or 0)
        if existing_count >= max_files:
            return Response({"detail": "Limite de fotos alcanzado"}, status=422)
        available = max_files - existing_count
        if len(uploads) > available:
            return Response({"detail": f"Solo se pueden subir {available} fotos adicionales"}, status=422)

        max_size_mb = int(getattr(settings, "INGRESO_MEDIA_MAX_SIZE_MB", 10) or 10)
        thumb_max = int(getattr(settings, "INGRESO_MEDIA_THUMB_MAX", 512) or 512)
        max_size_bytes = max_size_mb * 1024 * 1024
        allowed_mime = getattr(settings, "INGRESO_MEDIA_ALLOWED_MIME", ["image/jpeg", "image/png"]) or ["image/jpeg", "image/png"]

        user_id = _current_user_id(request)
        if user_id is None:
            return Response({"detail": "Usuario no autenticado para subir fotos"}, status=401)
        _set_audit_user(request)

        uploaded_items = []
        errors = []

        for up in uploads:
            storage_path = None
            thumb_path = None
            try:
                processed = process_upload_any(up, max_size_bytes=max_size_bytes, thumb_max=thumb_max, allowed_mime=allowed_mime)
                storage_path, thumb_path = save_processed_media(ingreso_id, processed)
                with transaction.atomic():
                    if connection.vendor == "postgresql":
                        exec_void("SET LOCAL app.ingreso_id = %s", [ingreso_id])
                    media_id = exec_returning(
                        """
                        INSERT INTO ingreso_media (
                          ingreso_id, usuario_id, storage_path, thumbnail_path,
                          original_name, mime_type, size_bytes, width, height
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        RETURNING id
                        """,
                        [
                            ingreso_id,
                            user_id,
                            storage_path,
                            thumb_path,
                            processed.display_name,
                            processed.mime_type,
                            len(processed.content),
                            getattr(processed, "width", 0) or 0,
                            getattr(processed, "height", 0) or 0,
                        ],
                    )
                row = _fetch_media_row(ingreso_id, media_id)
                if row:
                    uploaded_items.append(_serialize_media_row(row, ingreso_id, request))
            except MediaValidationError as exc:
                delete_media_paths([storage_path, thumb_path])
                errors.append({
                    "name": getattr(up, "name", ""),
                    "detail": str(exc),
                    "status": getattr(exc, "status_code", 400),
                })
            except MediaProcessingError as exc:
                delete_media_paths([storage_path, thumb_path])
                errors.append({
                    "name": getattr(up, "name", ""),
                    "detail": str(exc),
                    "status": 422,
                })
            except Exception as exc:
                # Log detallado en server y limpiar archivos si se escribieron
                try:
                    logger.exception(
                        "Error inesperado al guardar foto (ingreso_id=%s, nombre=%s)",
                        ingreso_id, getattr(up, "name", ""), exc_info=exc
                    )
                except Exception:
                    pass
                delete_media_paths([storage_path, thumb_path])
                msg = "Error inesperado al guardar la foto"
                if getattr(settings, "DEBUG", False):
                    try:
                        msg = f"{msg}: {exc}"
                    except Exception:
                        pass
                errors.append({
                    "name": getattr(up, "name", ""),
                    "detail": msg,
                    "status": 500,
                })

        if not uploaded_items:
            status_code = errors[0].get("status", 400) if errors else 400
            detail = errors[0].get("detail", "No se pudo subir la foto") if errors else "No se pudo subir la foto"
            return Response({"detail": detail, "errors": errors}, status=status_code)

        remaining = max(0, max_files - (existing_count + len(uploaded_items)))
        status_code = 201 if not errors else 207
        return Response({
            "uploaded": uploaded_items,
            "errors": errors,
            "remaining_slots": remaining,
        }, status=status_code)


class IngresoMediaDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, ingreso_id: int, media_id: int):
        ingreso_row = _fetch_ingreso_assignation(ingreso_id)
        if not ingreso_row:
            return Response({"detail": "Ingreso no encontrado"}, status=404)
        _ensure_media_manage_perm(request, ingreso_row)
        row = _fetch_media_row(ingreso_id, media_id)
        if not row:
            return Response({"detail": "Foto no encontrada"}, status=404)

        comentario = None
        if isinstance(request.data, dict):
            comentario = request.data.get("comentario")
        comentario = (comentario or "").strip() or None

        _set_audit_user(request)
        now = timezone.now()
        with transaction.atomic():
            if connection.vendor == "postgresql":
                exec_void("SET LOCAL app.ingreso_id = %s", [ingreso_id])
            exec_void(
                "UPDATE ingreso_media SET comentario=%s, updated_at=%s WHERE ingreso_id=%s AND id=%s",
                [comentario, now, ingreso_id, media_id],
            )
        row = _fetch_media_row(ingreso_id, media_id)
        if row:
            return Response(_serialize_media_row(row, ingreso_id, request))
        return Response({"detail": "Foto no encontrada"}, status=404)

    def delete(self, request, ingreso_id: int, media_id: int):
        ingreso_row = _fetch_ingreso_assignation(ingreso_id)
        if not ingreso_row:
            return Response({"detail": "Ingreso no encontrado"}, status=404)
        _ensure_media_manage_perm(request, ingreso_row)
        row = _fetch_media_row(ingreso_id, media_id)
        if not row:
            return Response({"detail": "Foto no encontrada"}, status=404)

        storage_path = row.get("storage_path")
        thumb_path = row.get("thumbnail_path")

        _set_audit_user(request)
        with transaction.atomic():
            if connection.vendor == "postgresql":
                exec_void("SET LOCAL app.ingreso_id = %s", [ingreso_id])
            exec_void("DELETE FROM ingreso_media WHERE ingreso_id=%s AND id=%s", [ingreso_id, media_id])

        delete_media_paths([storage_path, thumb_path])
        return Response({"ok": True})


class IngresoMediaFileView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, ingreso_id: int, media_id: int):
        ingreso_row = _fetch_ingreso_assignation(ingreso_id)
        if not ingreso_row:
            return Response({"detail": "Ingreso no encontrado"}, status=404)
        _ensure_media_view_perm(request, ingreso_row)
        row = _fetch_media_row(ingreso_id, media_id)
        if not row:
            return Response({"detail": "Foto no encontrada"}, status=404)

        storage_path = row.get("storage_path")
        if not storage_path:
            return Response({"detail": "Archivo no disponible"}, status=404)

        try:
            file_obj = default_storage.open(storage_path, "rb")
        except FileNotFoundError:
            return Response({"detail": "Archivo no disponible"}, status=410)
        except Exception:
            return Response({"detail": "No se pudo abrir el archivo"}, status=500)

        mime = row.get("mime_type") or "application/octet-stream"
        response = FileResponse(file_obj, content_type=mime)
        size_bytes = row.get("size_bytes")
        if size_bytes:
            response["Content-Length"] = str(size_bytes)
        filename = row.get("original_name") or f"ingreso-{ingreso_id}-foto-{media_id}"
        try:
            quoted = quote(filename)
        except Exception:
            quoted = filename
        response["Content-Disposition"] = f'attachment; filename="{filename}"; filename*=UTF-8''{quoted}'
        response["Cache-Control"] = "private, max-age=86400"
        return response


class IngresoMediaThumbnailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, ingreso_id: int, media_id: int):
        ingreso_row = _fetch_ingreso_assignation(ingreso_id)
        if not ingreso_row:
            return Response({"detail": "Ingreso no encontrado"}, status=404)
        _ensure_media_view_perm(request, ingreso_row)
        row = _fetch_media_row(ingreso_id, media_id)
        if not row:
            return Response({"detail": "Foto no encontrada"}, status=404)
        thumb_path = row.get("thumbnail_path")
        if not thumb_path:
            # Generar miniatura on-demand: intentar PDF real, si no placeholder
            try:
                mime = (row.get("mime_type") or "").lower()
                max_size = int(getattr(settings, "INGRESO_MEDIA_THUMB_MAX", 512) or 512)
                content = None
                # Intentar thumb de PDF desde el archivo original
                if mime == "application/pdf":
                    orig_path = row.get("storage_path")
                    if orig_path:
                        try:
                            with default_storage.open(orig_path, "rb") as f:
                                pdf_bytes = f.read()
                            tb, tm = _make_pdf_thumb(pdf_bytes, min(max_size, 512))
                            if tb:
                                content = tb
                        except Exception:
                            content = None
                if content is None:
                    label = "PDF" if mime == "application/pdf" else ("VIDEO" if mime.startswith("video/") else "FILE")
                    content, _ = _make_placeholder_thumb(label, size=min(max_size, 512))
                # Reusar carpeta del original
                orig_path = (row.get("storage_path") or "").strip().strip("/")
                folder = "/".join(orig_path.split("/")[:-1]) if "/" in orig_path else orig_path
                name = f"media-{media_id}-thumb.jpg"
                new_thumb_path = (folder + "/" + name).strip("/") if folder else name
                default_storage.save(new_thumb_path, ContentFile(content))
                # Persistir en DB
                exec_void(
                    "UPDATE ingreso_media SET thumbnail_path=%s WHERE ingreso_id=%s AND id=%s",
                    [new_thumb_path, ingreso_id, media_id],
                )
                thumb_path = new_thumb_path
            except Exception:
                return Response({"detail": "Miniatura no disponible"}, status=404)
        try:
            file_obj = default_storage.open(thumb_path, "rb")
        except FileNotFoundError:
            # Intentar regenerar si fue borrada
            try:
                label = "PDF" if (row.get("mime_type") or "").lower() == "application/pdf" else (
                    "VIDEO" if (row.get("mime_type") or "").lower().startswith("video/") else "FILE"
                )
                max_size = int(getattr(settings, "INGRESO_MEDIA_THUMB_MAX", 512) or 512)
                content, _ = _make_placeholder_thumb(label, size=min(max_size, 512))
                default_storage.save(thumb_path, ContentFile(content))
                file_obj = default_storage.open(thumb_path, "rb")
            except Exception:
                return Response({"detail": "Miniatura no disponible"}, status=410)
        except Exception:
            return Response({"detail": "No se pudo abrir la miniatura"}, status=500)
        response = FileResponse(file_obj, content_type="image/jpeg")
        response["Content-Disposition"] = 'inline; filename="thumbnail.jpg"'
        response["Cache-Control"] = "private, max-age=86400"
        return response


__all__ = [
    'IngresoMediaListCreateView',
    'IngresoMediaDetailView',
    'IngresoMediaFileView',
    'IngresoMediaThumbnailView',
]
