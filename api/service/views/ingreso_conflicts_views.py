"""Buscar la prueba y cerrar la orden anterior de un equipo que volvió a entrar.

Dos endpoints. Uno busca en Bejerman el RSS/RTA que demuestra que el equipo sí había salido;
el otro cierra la orden vieja apoyándose en esa prueba.

El permiso se parte según la prueba, a propósito. Con un comprobante posterior a la vista no
hay criterio que aplicar —el equipo salió, punto—, así que recepción puede cerrarlo desde el
mostrador y seguir atendiendo. Sin comprobante hay que decidir qué pasó, y eso vuelve a ser
una corrección histórica de jefatura.
"""

from __future__ import annotations

import logging
from datetime import date

from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from ..bejerman_ingreso_conflicts import (
    find_document_by_number,
    find_exit_evidence,
    get_conflict,
    mark_resolved,
)
from ..permissions import require_permission, user_has_permission
from .helpers import os_label, q
from .ingresos_views import IngresoCorreccionesHistoricasView, _child_ingreso_request

logger = logging.getLogger(__name__)


def _actor_id(request):
    return getattr(getattr(request, "user", None), "id", None) or getattr(request, "user_id", None)


def _open_ingreso(ingreso_id):
    return q(
        """
        SELECT t.id,
               COALESCE(t.fecha_ingreso, t.fecha_creacion) AS fecha_ingreso,
               COALESCE(t.estado::text, '') AS estado,
               COALESCE(d.numero_serie, '') AS numero_serie,
               COALESCE(NULLIF(t.empresa_bejerman, ''), 'SEPID') AS empresa_bejerman
          FROM ingresos t
          JOIN devices d ON d.id = t.device_id
         WHERE t.id = %s
        """,
        [ingreso_id],
        one=True,
    )


def _int_or_none(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _resolve_target(data):
    """Ubica la orden vieja a cerrar, venga por conflicto anotado o suelta desde el preflight.

    En el alta el conflicto todavía no está anotado (los ingresos no existen), así que el
    panel de validación manda directamente el id de la orden vieja.
    """

    conflict_id = _int_or_none(data.get("conflict_id") or data.get("conflictId"))
    conflict = get_conflict(conflict_id) if conflict_id else None
    if conflict:
        return conflict, _int_or_none(conflict.get("conflicting_ingreso_id"))
    return None, _int_or_none(data.get("conflicting_ingreso_id") or data.get("conflictingIngresoId"))


def _serials_for(data, conflict, previo):
    """Todas las escrituras conocidas de la serie del equipo, sin repetir.

    La que tipeó recepción al ingresar puede traer un espacio de más ("MZJ5S 121220"); la del
    equipo en NEXORA suele ser la que coincide con la partida de Bejerman. Se prueban las dos:
    buscar por una sola era lo que dejaba el conflicto sin salida cuando el remito sí existía.
    """

    out = []
    for value in (
        str(data.get("numero_serie") or data.get("numeroSerie") or "").strip(),
        str((conflict or {}).get("numero_serie") or "").strip(),
        str((previo or {}).get("numero_serie") or "").strip(),
    ):
        if value and value not in out:
            out.append(value)
    return out


def _iso_date(value):
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return ""


class IngresoConflictoEvidenciaView(APIView):
    """Busca en Bejerman un RSS/RTA de ese equipo posterior a la orden abierta."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        require_permission(request, "action.ingreso.resolve_open_conflict")
        data = request.data or {}
        conflict, conflicting_ingreso_id = _resolve_target(data)
        if not conflicting_ingreso_id:
            return Response({"detail": "Falta indicar la orden anterior a revisar."}, status=400)
        previo = _open_ingreso(conflicting_ingreso_id)
        if not previo:
            return Response({"detail": "No se encontró la orden anterior."}, status=404)

        serials = _serials_for(data, conflict, previo)
        company_key = (
            str(data.get("company_key") or data.get("companyKey") or "").strip()
            or previo.get("empresa_bejerman")
            or "SEPID"
        )
        evidence = find_exit_evidence(serials, previo.get("fecha_ingreso"), company_key=company_key)
        return Response(
            {
                "ok": True,
                "conflicting_ingreso_id": conflicting_ingreso_id,
                "conflicting_os": os_label(conflicting_ingreso_id),
                "conflicting_fecha_ingreso": previo.get("fecha_ingreso"),
                "numero_serie": serials[0] if serials else "",
                **evidence,
            }
        )


class IngresoConflictoResolverView(APIView):
    """Cierra la orden anterior usando el comprobante de Bejerman como respaldo."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        data = request.data or {}
        conflict, conflicting_ingreso_id = _resolve_target(data)
        if not conflicting_ingreso_id:
            return Response({"detail": "Falta indicar la orden anterior a cerrar."}, status=400)
        previo = _open_ingreso(conflicting_ingreso_id)
        if not previo:
            return Response({"detail": "No se encontró la orden anterior."}, status=404)

        serials = _serials_for(data, conflict, previo)
        serial = serials[0] if serials else ""
        company_key = (
            str(data.get("company_key") or data.get("companyKey") or "").strip()
            or previo.get("empresa_bejerman")
            or "SEPID"
        )
        fecha_previa = previo.get("fecha_ingreso")
        manual_number = str(data.get("remito_salida") or data.get("remitoSalida") or "").strip()
        nota = str(data.get("motivo") or data.get("nota") or "").strip()

        # La evidencia se vuelve a buscar del lado del servidor: el cliente puede mandar
        # cualquier cosa, y de esta consulta depende quién está autorizado a cerrar.
        evidence = find_exit_evidence(serials, fecha_previa, company_key=company_key)
        documents = evidence.get("documents") or []
        comprobante = documents[0] if documents else {}

        manual_check = None
        if not comprobante and manual_number:
            # El número lo trae una persona que tiene el papel a la vista, pero igual se
            # verifica: lo que respalda el cierre es el comprobante en Bejerman, no el
            # formulario. Con la serie mal escrita en NEXORA, éste es el único camino que
            # queda para cerrar la orden con su remito de verdad.
            manual_check = find_document_by_number(
                manual_number, company_key=company_key, since=fecha_previa
            )
            if manual_check.get("status") == "found":
                comprobante = (manual_check.get("documents") or [{}])[0]
                evidence = {
                    **manual_check,
                    "source": "manual_number",
                    "typedNumber": manual_number,
                    "bySerial": evidence,
                }

        if not comprobante:
            # Sin prueba, cerrar la orden es una decisión, no un trámite: vuelve a jefatura.
            if not user_has_permission(request, "action.ingreso.force_historical"):
                motivo_rechazo = (manual_check or {}).get("reason") or (
                    "Bejerman no muestra ningún remito de salida posterior para este equipo."
                )
                return Response(
                    {
                        "detail": (
                            f"{motivo_rechazo} No se puede cerrar la orden anterior desde acá. "
                            "El aviso ya le llegó a jefatura para que lo resuelva."
                        ),
                        "evidence": evidence,
                        "manual": manual_check,
                    },
                    status=403,
                )
            require_permission(request, "action.ingreso.force_historical")
        else:
            require_permission(request, "action.ingreso.resolve_open_conflict")

        verificado = bool(comprobante)
        remito = str(comprobante.get("remitoNumber") or "").strip() or (manual_number if not verificado else "")
        fecha_salida = str(comprobante.get("issueDate") or "").strip() or _iso_date(
            data.get("fecha_salida") or data.get("fechaSalida")
        )
        if verificado:
            respaldo = f"Bejerman registra {remito} del {fecha_salida}, posterior al ingreso: el equipo ya había salido."
            if evidence.get("source") == "manual_number":
                respaldo += " Número cargado a mano y verificado en Bejerman."
        elif remito:
            respaldo = (
                f"Cierre forzado por jefatura con el remito {remito} cargado a mano, "
                "sin respaldo verificable en Bejerman."
            )
        else:
            respaldo = "Sin comprobante de salida en Bejerman; cierre forzado por jefatura."
        motivo = f"Cierre de orden abierta al reingresar el equipo {serial}. {respaldo}"
        if nota:
            motivo = f"{motivo} {nota}"

        correccion = {
            "accion": "entrega",
            "motivo": motivo,
            "fecha_efectiva": fecha_salida or None,
            "fecha_entrega": fecha_salida or None,
            "remito_salida": remito,
            "notificar": False,
        }
        if not correccion["fecha_efectiva"]:
            correccion.pop("fecha_efectiva")
            correccion.pop("fecha_entrega")

        # Se delega en la corrección histórica que ya existe: deja el mismo rastro auditado
        # que cualquier otro cierre forzado, en vez de tocar `ingresos` por un camino paralelo.
        child = _child_ingreso_request(request, correccion, _historical_correction_authorized=True)
        respuesta = IngresoCorreccionesHistoricasView().post(child, conflicting_ingreso_id)
        if getattr(respuesta, "status_code", 500) >= 400:
            return respuesta

        if conflict:
            mark_resolved(
                conflict["id"],
                note=motivo,
                evidence=evidence,
                user_id=_actor_id(request),
            )

        return Response(
            {
                "ok": True,
                "conflicting_ingreso_id": conflicting_ingreso_id,
                "conflicting_os": os_label(conflicting_ingreso_id),
                "remito_salida": remito,
                "fecha_entrega": fecha_salida,
                "evidence": evidence,
                "detail": (
                    f"La OS {os_label(conflicting_ingreso_id)} quedó cerrada con el remito {remito}."
                    if remito
                    else f"La OS {os_label(conflicting_ingreso_id)} quedó cerrada."
                ),
            }
        )
