import { useEffect, useRef, useState } from "react";
import { postBajaIngreso, postAltaIngreso, postSolicitarBaja } from "../lib/api";
import { describeBejermanStockJob, lastIngresoIdOf } from "../lib/device-actions.js";
import { patrimonialEmailMessage, watchPatrimonialEmail } from "../lib/patrimonialEmail";
import {
  deviceCustomerTitle,
  deviceCustomerSubtitle,
  deviceModelTitle,
  tipoEquipoOf,
} from "../lib/ui-helpers";
import DeviceIdentifier from "./DeviceIdentifier.jsx";

// Baja / alta / solicitud de baja de un equipo, en un solo modal.
//
// La baja es un estado del INGRESO, no del equipo: siempre se aplica sobre el
// último ingreso. Por eso el modal muestra de qué OS y de qué equipo se trata —
// desde una grilla de cientos de filas, un confirm() a secas se dispara sobre la
// fila equivocada con demasiada facilidad.

const COPY = {
  baja: {
    title: "Dar de BAJA el equipo",
    explain:
      "El ingreso pasa a estado BAJA, la ubicación queda en “-” y el stock se mueve al depósito de Desguace en Bejerman.",
    confirm: "Dar de BAJA",
    saving: "Marcando baja...",
    buttonClass: "bg-red-600 hover:bg-red-700",
    accion: "Baja",
    evento: "baja",
  },
  alta: {
    title: "Dar de ALTA el equipo",
    explain:
      "El ingreso vuelve a estado INGRESADO en Taller y el stock sale del depósito de Desguace en Bejerman.",
    confirm: "Dar de ALTA",
    saving: "Marcando alta...",
    buttonClass: "bg-emerald-600 hover:bg-emerald-700",
    accion: "Alta",
    evento: "alta",
  },
  solicitar: {
    title: "Solicitar BAJA",
    explain:
      "Tu usuario no puede dar la baja directamente. La solicitud queda registrada y jefatura decide si la acepta.",
    confirm: "Enviar solicitud",
    saving: "Enviando solicitud...",
    buttonClass: "bg-red-600 hover:bg-red-700",
    accion: "Solicitud",
    evento: "",
  },
};

/**
 * @param {object} props
 *   row       — fila de equipo (listado) o detalle de ingreso (hoja de servicio)
 *   ingresoId — OS sobre la que actuar; por defecto, el último ingreso de la fila
 */
export default function DeviceBajaModal({ row, ingresoId: ingresoIdProp, mode = "baja", onClose, onDone }) {
  const [motivo, setMotivo] = useState("");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");
  const [notice, setNotice] = useState("");
  const [emailStatus, setEmailStatus] = useState("");
  const emailWatchRef = useRef(null);

  const ingresoId = Number(ingresoIdProp || 0) || lastIngresoIdOf(row);
  const copy = COPY[mode] || COPY.baja;
  const isSolicitud = mode === "solicitar";

  useEffect(() => {
    setMotivo("");
    setErr("");
    setNotice("");
    setEmailStatus("");
  }, [row?.id, mode]);

  useEffect(
    () => () => {
      if (emailWatchRef.current) emailWatchRef.current();
    },
    [],
  );

  if (!row) return null;

  // Un aviso patrimonial que sigue en cola se resuelve unos segundos después de
  // la respuesta: se deja el modal abierto hasta saber si el mail salió de verdad.
  const anunciarPatrimonial = (base, info) => {
    setEmailStatus(String(info?.status || ""));
    setNotice(`${base} ${patrimonialEmailMessage(info, { evento: copy.evento })}`.trim());
    if (emailWatchRef.current) emailWatchRef.current();
    emailWatchRef.current = watchPatrimonialEmail(
      info,
      (msg, next) => {
        setEmailStatus(String(next?.status || ""));
        setNotice(`${base} ${msg}`.trim());
      },
      { evento: copy.evento },
    );
  };

  async function confirmar() {
    if (saving || !ingresoId) return;
    const motivoTexto = motivo.trim();
    if (isSolicitud && !motivoTexto) {
      setErr("Debe indicar el motivo para solicitar la BAJA.");
      return;
    }
    setErr("");
    try {
      setSaving(true);
      if (isSolicitud) {
        const resp = await postSolicitarBaja(ingresoId, { motivo: motivoTexto });
        if (resp?.already_pending) {
          setNotice("Ya existe una solicitud de BAJA pendiente para este ingreso.");
        } else if (resp?.already_baja) {
          setNotice("El ingreso ya está en estado BAJA.");
        } else if (resp?.email_sent === false) {
          setNotice("Solicitud de BAJA registrada; no se pudo enviar el correo a jefatura.");
        } else {
          setNotice("Solicitud de BAJA enviada a jefatura.");
        }
        onDone?.();
        return;
      }
      const resp = mode === "alta"
        ? await postAltaIngreso(ingresoId)
        : await postBajaIngreso(ingresoId);
      onDone?.();
      anunciarPatrimonial(
        describeBejermanStockJob(resp?.bejerman_sync_job, copy.accion),
        resp?.patrimonial_email,
      );
    } catch (e) {
      setErr(e?.message || `No se pudo completar la ${copy.accion.toLowerCase()}.`);
    } finally {
      setSaving(false);
    }
  }

  const subtitle = deviceCustomerSubtitle(row);
  const tipo = tipoEquipoOf(row, "");
  const noticeClass = emailStatus === "sent"
    ? "bg-emerald-50 text-emerald-800 border-emerald-300"
    : emailStatus === "failed" || emailStatus === "sin_destinatarios"
      ? "bg-red-100 text-red-800 border-red-300"
      : "bg-amber-50 text-amber-800 border-amber-300";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
      <div className="w-full max-w-lg rounded bg-white p-4 shadow-lg">
        <div className="mb-3 text-lg font-semibold">{copy.title}</div>

        <div className="mb-3 rounded border bg-gray-50 p-3 text-sm">
          <div className="font-medium">
            {ingresoId ? `OS #${ingresoId}` : "Sin ingreso registrado"} · {deviceCustomerTitle(row)}
          </div>
          {subtitle ? <div className="text-xs text-gray-500">{subtitle}</div> : null}
          <div className="mt-1">
            {[row?.marca, deviceModelTitle(row)].filter((v) => v && v !== "-").join(" ") || "-"}
          </div>
          {tipo ? <div className="text-xs text-gray-500">{tipo}</div> : null}
          <div className="mt-1 text-xs text-gray-600">
            <DeviceIdentifier row={row} />
          </div>
        </div>

        <div className="mb-3 text-sm text-gray-600">{copy.explain}</div>

        {isSolicitud && !notice && (
          <label className="mb-3 block">
            <div className="mb-1 text-sm text-gray-700">Motivo (obligatorio)</div>
            <textarea
              className="w-full rounded border p-2 text-sm"
              rows={3}
              value={motivo}
              onChange={(e) => setMotivo(e.target.value)}
              disabled={saving}
              placeholder="Por qué corresponde dar de baja este equipo"
            />
          </label>
        )}

        {!ingresoId && (
          <div className="mb-3 rounded border border-amber-300 bg-amber-50 p-2 text-sm text-amber-800">
            El equipo no tiene ingresos registrados, así que no hay ninguna OS sobre la que actuar.
          </div>
        )}

        {err && (
          <div className="mb-3 rounded border border-red-300 bg-red-100 p-2 text-sm text-red-800">{err}</div>
        )}

        {notice && (
          <div className={`mb-3 rounded border p-2 text-sm ${noticeClass}`}>{notice}</div>
        )}

        <div className="mt-4 flex justify-end gap-2">
          <button type="button" className="rounded border px-3 py-1.5" onClick={onClose} disabled={saving}>
            {notice ? "Cerrar" : "Cancelar"}
          </button>
          {!notice && (
            <button
              type="button"
              className={`rounded px-3 py-1.5 text-white disabled:opacity-50 ${copy.buttonClass}`}
              onClick={confirmar}
              disabled={saving || !ingresoId || (isSolicitud && !motivo.trim())}
            >
              {saving ? copy.saving : copy.confirm}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
