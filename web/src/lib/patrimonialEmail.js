import { getNotificacionesEmailEstado } from "./api";

// Textos de los avisos patrimoniales por correo (baja, alta, alta MG y venta MG).
// Todos los mensajes salen del estado real que informa el backend: nunca se
// muestra "enviado" si el servidor de correo no confirmó la entrega.

const EVENT_LABELS = {
  baja: "baja",
  alta: "alta",
  alta_mg: "alta de MG",
  venta_mg: "venta del MG",
};

export function patrimonialEmailMessage(info, { evento } = {}) {
  if (!info) return "";
  const label = EVENT_LABELS[String(evento || info.event || "").trim()] || "operación";
  const status = String(info.status || "").trim();
  const detail = String(info.detail || "").trim();
  const recipients = (info.recipients || []).join(", ");

  if (status === "sent") {
    return `Aviso de ${label} enviado por mail${recipients ? ` a ${recipients}` : ""}.`;
  }
  if (status === "queued") {
    return `Aviso de ${label} encolado${recipients ? ` para ${recipients}` : ""}; confirmando envío...`;
  }
  if (status === "retry") {
    return `El aviso de ${label} por mail todavía no salió: ${detail || "se está reintentando automáticamente."}`;
  }
  if (status === "failed") {
    return `No se pudo enviar el aviso de ${label} por mail. ${detail}`;
  }
  if (status === "sin_destinatarios") {
    return `No se envió el aviso de ${label} por mail: no hay destinatarios configurados.`;
  }
  if (status === "sin_envios") {
    return `No se registró ningún aviso de ${label} por mail.`;
  }
  if (detail) {
    return `Aviso de ${label} por mail: ${detail}`;
  }
  return "";
}

// El envío real lo hace el worker de la cola de correos, así que el estado
// definitivo aparece unos segundos después de la respuesta. Se consulta hasta
// que la cola confirma el envío o el fallo definitivo.
export function watchPatrimonialEmail(info, onUpdate, { evento, attempts = 6, delayMs = 2500 } = {}) {
  const ids = (info?.deliveryIds || []).map((value) => Number(value)).filter((value) => value > 0);
  const status = String(info?.status || "").trim();
  if (!ids.length || status === "sent" || status === "failed" || status === "sin_destinatarios") {
    return () => {};
  }

  let cancelled = false;
  let timer = null;
  let pending = attempts;

  async function tick() {
    if (cancelled) return;
    pending -= 1;
    try {
      const resp = await getNotificacionesEmailEstado(ids);
      if (cancelled || !resp) return;
      const next = { ...info, ...resp, deliveryIds: ids };
      const nextStatus = String(next.status || "").trim();
      onUpdate(patrimonialEmailMessage(next, { evento: evento || info?.event }), next);
      if (nextStatus === "sent" || nextStatus === "failed" || nextStatus === "sin_envios") return;
    } catch {
      // Un fallo de consulta no cambia el estado del correo: se reintenta.
    }
    if (!cancelled && pending > 0) {
      timer = setTimeout(tick, delayMs);
    }
  }

  timer = setTimeout(tick, delayMs);
  return () => {
    cancelled = true;
    if (timer) clearTimeout(timer);
  };
}
