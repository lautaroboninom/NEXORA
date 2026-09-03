import { useEffect, useRef, useState } from "react";

import { useAuth } from "../context/AuthContext";
import { can, PERMISSION_CODES } from "../lib/permissions";
import {
  deleteCambioEquipo,
  getCambioEquipoCandidatos,
  postCambioEquipo,
} from "../lib/api";
import {
  SWAP_DESTINO,
  SWAP_DESTINO_OPTIONS,
  getDeviceSwapState,
} from "../lib/device-swap.js";
import { describeBejermanStockJob } from "../lib/device-actions.js";
import { patrimonialEmailMessage, watchPatrimonialEmail } from "../lib/patrimonialEmail";
import { deviceCustomerTitle, ingresoIdOf, tipoEquipoOf } from "../lib/ui-helpers";
import DeviceIdentifier from "./DeviceIdentifier.jsx";

// Cambio de equipo (permuta / parte de pago).
//
// El cliente trae su equipo, se le entrega otro y el suyo queda para SEPID. El equipo que
// sale se ELIGE de una lista: antes era un campo de texto libre que no vinculaba nada, y
// por eso el stock y el remito terminaban con la serie del equipo equivocado.
//
// La segunda modalidad es la parte de pago **sin** entregar otro equipo: mismo movimiento
// patrimonial, sin nada que salga. Se elige arriba de todo porque decide el resto del form.
//
// El destino NO es un detalle administrativo: es lo que decide si el equipo pasa a
// **propiedad de SEPID** y a qué depósito de Bejerman va (VAL si se vende, DES si se
// desguaza). Con "se decide después" el equipo sigue siendo del cliente, que es
// exactamente el malentendido que hubo que aclarar en la pantalla.
//
// El resumen "Sale / Queda" de abajo no es decoración: es lo único que separa registrar
// bien el cambio de cargar las dos series al revés.

const DEBOUNCE_MS = 300;

function errorMessage(error, fallback) {
  const raw = error?.data?.detail || error?.message || String(error || "");
  return raw || fallback;
}

const candidateKey = (item) => `${item?.deposito || ""}|${item?.article_code || ""}|${item?.partida || ""}`;

function CandidateRow({ item, selected, onPick, disabled }) {
  const bloqueado = item.disponible === false;
  return (
    <button
      type="button"
      onClick={() => onPick(item)}
      disabled={disabled || bloqueado}
      title={item.motivo_no_disponible || ""}
      className={`w-full border-b px-3 py-2 text-left text-sm last:border-b-0 disabled:cursor-not-allowed disabled:opacity-60 ${
        selected ? "bg-blue-50" : "hover:bg-gray-50"
      }`}
    >
      <div className="font-medium">
        {item.partida}
        {item.numero_interno ? ` | ${item.numero_interno}` : ""}
      </div>
      <div className="text-xs text-gray-600">
        {[item.marca, item.modelo].filter(Boolean).join(" ") || item.descripcion || "-"}
      </div>
      <div className="text-xs text-gray-500">
        {`Art. ${item.article_code} · Depósito ${item.deposito}`}
        {Number(item.cantidad) > 1 ? ` · ${item.cantidad} disponibles` : ""}
      </div>
      {bloqueado ? (
        <div className="mt-0.5 text-xs font-medium text-amber-700">{item.motivo_no_disponible}</div>
      ) : null}
      {!item.mapeado ? (
        <div className="mt-0.5 text-xs text-amber-700">
          El artículo no está mapeado a un modelo: la ficha va a quedar sin marca ni modelo.
        </div>
      ) : null}
    </button>
  );
}

/**
 * @param {object} props
 *   open      — visibilidad
 *   row       — detalle de la OS o fila de listado (para el encabezado y el estado)
 *   ingresoId — OS sobre la que actuar; por defecto, la de `row`
 *   canSwap   — permiso `action.ingreso.device_swap`
 *   onDone    — se llama después de registrar o deshacer, para refrescar
 *
 * El permiso del **destino patrimonial** se lee acá adentro y no se pasa por props a
 * propósito: `canSwap` viaja por tres pantallas y el patrón de olvidarse el gate en uno de
 * los call sites ya nos costó varios bugs.
 */
export default function DeviceSwapModal({
  open,
  row,
  ingresoId: ingresoIdProp,
  canSwap = false,
  onClose,
  onDone,
}) {
  const ingresoId = Number(ingresoIdProp || 0) || Number(ingresoIdOf(row) || 0);
  const { user } = useAuth();
  // 'pasa a MG BIO' y 'desguace' mueven patrimonio (y stock real, en desguace). El backend
  // los exige con estos mismos dos permisos; recepción registra el cambio pero no el destino.
  const canSetDestino =
    can(user, PERMISSION_CODES.ACTION_INGRESO_BAJA_ALTA) ||
    can(user, PERMISSION_CODES.ACTION_DEVICES_PREVENTIVOS_MANAGE);

  const [term, setTerm] = useState("");
  const [candidatos, setCandidatos] = useState([]);
  const [depositos, setDepositos] = useState([]);
  const [deposito, setDeposito] = useState("");
  const [buscando, setBuscando] = useState(false);
  const [elegido, setElegido] = useState(null);
  const [conEntrega, setConEntrega] = useState(true);
  const [destino, setDestino] = useState(SWAP_DESTINO.PENDIENTE);
  const [numeroMg, setNumeroMg] = useState("");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");
  const [notice, setNotice] = useState("");
  const [emailStatus, setEmailStatus] = useState("");
  const emailWatchRef = useRef(null);

  const state = getDeviceSwapState(row || {}, { canSwap });

  useEffect(() => {
    if (!open) return;
    setTerm("");
    setCandidatos([]);
    // Los depósitos también: son de la empresa Bejerman de la OS, y sin esto el selector
    // arrancaba con la lista de la OS anterior hasta que volviera el primer fetch.
    setDepositos([]);
    setDeposito("");
    setElegido(null);
    setConEntrega(!state.sinEntrega);
    setDestino(state.destino || SWAP_DESTINO.PENDIENTE);
    setNumeroMg("");
    setErr("");
    setNotice("");
    setEmailStatus("");
    // Sólo al abrir o al cambiar de OS: no se reinicia mientras el operador tipea.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, ingresoId]);

  useEffect(
    () => () => {
      if (emailWatchRef.current) emailWatchRef.current();
    },
    []
  );

  // Debounce: el catálogo de equipos es grande y sin esto se dispara una consulta por tecla.
  // Sin entrega no hay nada que buscar: se evita el viaje a Bejerman.
  useEffect(() => {
    if (!open || !ingresoId || !state.canRegister || !conEntrega) return undefined;
    let cancelado = false;
    const timer = setTimeout(async () => {
      try {
        setBuscando(true);
        const data = await getCambioEquipoCandidatos(ingresoId, term, deposito);
        if (cancelado) return;
        const items = Array.isArray(data?.items) ? data.items : [];
        setCandidatos(items);
        if (Array.isArray(data?.depositos)) setDepositos(data.depositos);
        // La unidad elegida sólo sobrevive si sigue estando en el resultado: si no, se
        // confirmaba una partida que ya no estaba en la lista que el operador ve.
        setElegido((prev) =>
          prev && items.some((item) => candidateKey(item) === candidateKey(prev)) ? prev : null
        );
        setErr("");
      } catch (e) {
        if (!cancelado) setErr(errorMessage(e, "No se pudo consultar el stock de Bejerman."));
      } finally {
        if (!cancelado) setBuscando(false);
      }
    }, DEBOUNCE_MS);
    return () => {
      cancelado = true;
      clearTimeout(timer);
    };
  }, [open, ingresoId, term, deposito, state.canRegister, conEntrega]);

  if (!open || !row) return null;

  const anunciarPatrimonial = (base, info) => {
    if (!info) {
      setNotice(base);
      return;
    }
    setEmailStatus(String(info?.status || ""));
    setNotice(`${base} ${patrimonialEmailMessage(info, { evento: "alta_mg" })}`.trim());
    if (emailWatchRef.current) emailWatchRef.current();
    emailWatchRef.current = watchPatrimonialEmail(
      info,
      (msg, next) => {
        setEmailStatus(String(next?.status || ""));
        setNotice(`${base} ${msg}`.trim());
      },
      { evento: "alta_mg" }
    );
  };

  const necesitaMg = destino === SWAP_DESTINO.MG;
  // El número interno es opcional: lo que hace propio al equipo es pasarlo al cliente
  // MG BIO, no el código. Sin MG igual habilita el circuito de baja a Desguace.
  const puedeConfirmar = Boolean(state.canRegister && (elegido || !conEntrega) && !saving);

  async function confirmar() {
    if (!puedeConfirmar) return;
    try {
      setSaving(true);
      setErr("");
      // Si la unidad ya tiene ficha en NEXORA se manda su id; si no, la partida y el
      // artículo, y el backend la ficha. Sin entrega no viaja ningún equipo.
      let payload;
      if (!conEntrega) {
        payload = { sin_entrega: true, destino_viejo: destino };
      } else if (elegido.device_id) {
        payload = { device_id: elegido.device_id, destino_viejo: destino };
      } else {
        payload = {
          partida: elegido.partida,
          article_code: elegido.article_code,
          destino_viejo: destino,
        };
      }
      if (necesitaMg && numeroMg.trim()) payload.numero_interno_mg = numeroMg.trim();
      let resp;
      try {
        resp = await postCambioEquipo(ingresoId, payload);
      } catch (e) {
        setErr(errorMessage(e, "No se pudo registrar el cambio de equipo."));
        return;
      }
      // A partir de acá el cambio YA está registrado en el backend. Si algo de lo que sigue
      // falla no se puede decir "no se pudo registrar": eso mandaba a reintentar un registro
      // hecho, y se perdían los avisos del pase a Desguace y del mail patrimonial.
      try {
        // Si fue a Desguace hay un movimiento de stock real detrás: se reporta su estado,
        // igual que en la baja, para que nadie lo dé por hecho si quedó trabado.
        const titulo = conEntrega ? "Cambio de equipo registrado." : "Parte de pago registrada.";
        const base = resp?.bejerman_sync_job
          ? `${titulo} ${describeBejermanStockJob(
              resp.bejerman_sync_job,
              resp?.bejerman_sync_label || "Movimiento de stock"
            )}`
          : titulo;
        anunciarPatrimonial(base, resp?.patrimonial_email);
        // Se pasa el cambio ya registrado para que quien abrió el modal pueda reflejarlo sin
        // esperar a que vuelva el refresco (el modal de salida lo usa para no quedar viejo).
        await onDone?.({ cambio: resp?.cambio ?? null });
      } catch {
        setErr("El cambio quedó registrado, pero no se pudo refrescar la pantalla.");
      }
    } finally {
      setSaving(false);
    }
  }

  async function deshacer() {
    if (saving || !state.canUndo) return;
    try {
      setSaving(true);
      setErr("");
      await deleteCambioEquipo(ingresoId);
      await onDone?.({ cambio: null });
      setNotice(
        "Cambio de equipo deshecho. La resolución quedó sin definir. Si el equipo ya había " +
          "pasado a propiedad de SEPID, o ya se movió a Ventas o a Desguace en Bejerman, eso " +
          "se revierte desde Equipos."
      );
    } catch (e) {
      setErr(errorMessage(e, "No se pudo deshacer el cambio."));
    } finally {
      setSaving(false);
    }
  }

  const recibido =
    [row?.numero_serie, row?.numero_interno].filter(Boolean).join(" | ") || "el equipo del cliente";
  const noticeClass =
    emailStatus === "sent"
      ? "border-emerald-300 bg-emerald-50 text-emerald-800"
      : emailStatus === "failed" || emailStatus === "sin_destinatarios"
        ? "border-red-300 bg-red-100 text-red-800"
        : "border-amber-300 bg-amber-50 text-amber-800";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4">
      <div className="flex max-h-full w-full max-w-2xl flex-col overflow-hidden rounded bg-white shadow-lg">
        <div className="border-b px-4 py-3">
          <div className="text-lg font-semibold">Cambio de equipo / parte de pago</div>
          <div className="text-sm text-gray-600">
            {ingresoId ? `OS #${ingresoId}` : "Sin OS"} · {deviceCustomerTitle(row)}
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
          <div className="mb-3 rounded border bg-gray-50 p-3 text-sm">
            <div className="text-xs font-semibold uppercase text-gray-500">Equipo recibido</div>
            <div className="mt-0.5">
              <DeviceIdentifier row={row} />
            </div>
            <div className="text-xs text-gray-500">
              {[row?.marca, row?.modelo, tipoEquipoOf(row, "")].filter((v) => v && v !== "-").join(" · ")}
            </div>
          </div>

          {state.hasSwap ? (
            <div className="mb-3 rounded border border-blue-300 bg-blue-50 p-3 text-sm text-blue-900">
              <div className="font-medium">
                {state.sinEntrega
                  ? "Esta OS ya tiene una parte de pago registrada."
                  : "Esta OS ya tiene un cambio registrado."}
              </div>
              <div className="mt-0.5">
                {state.sinEntrega
                  ? "No se entrega ningún equipo a cambio."
                  : `Se entrega: ${state.deliveredLabel}`}
              </div>
              {state.destinoResumen ? (
                <div className="text-xs">Equipo recibido: {state.destinoResumen}.</div>
              ) : null}
              {!state.isLinked && !state.sinEntrega ? (
                <div className="mt-1 text-xs text-amber-800">
                  Es un cambio viejo, cargado como texto: no está vinculado a un equipo. Elegí el
                  equipo abajo para vincularlo.
                </div>
              ) : null}
            </div>
          ) : null}

          {state.blockedReason ? (
            <div className="mb-3 rounded border border-amber-300 bg-amber-50 p-2 text-sm text-amber-800">
              {state.blockedReason}
            </div>
          ) : null}

          {state.canRegister ? (
            <>
              {/* Dos operaciones con el mismo movimiento patrimonial: lo que cambia es si
                  el cliente se lleva algo. Se elige primero porque decide el resto del form. */}
              <div className="mb-3 rounded border p-3 text-sm">
                <div className="mb-2 text-xs font-semibold uppercase text-gray-500">
                  Qué se está registrando
                </div>
                <label className="flex items-start gap-2">
                  <input
                    type="radio"
                    className="mt-1"
                    checked={conEntrega}
                    onChange={() => setConEntrega(true)}
                    disabled={saving}
                  />
                  <span>
                    <span className="font-medium">Cambio de equipo</span>
                    <span className="block text-xs text-gray-600">
                      El cliente se lleva otro equipo, que se elige del stock.
                    </span>
                  </span>
                </label>
                <label className="mt-2 flex items-start gap-2">
                  <input
                    type="radio"
                    className="mt-1"
                    checked={!conEntrega}
                    onChange={() => setConEntrega(false)}
                    disabled={saving}
                  />
                  <span>
                    <span className="font-medium">Parte de pago, sin entregar otro equipo</span>
                    <span className="block text-xs text-gray-600">
                      Tomamos el equipo del cliente y no sale ninguno a cambio.
                    </span>
                  </span>
                </label>
              </div>

              {conEntrega ? (
              <>
              <div className="mb-2 flex flex-col gap-2 sm:flex-row">
                <label className="block flex-1">
                  <div className="mb-1 text-sm text-gray-700">Equipo que se entrega</div>
                  <input
                    className="w-full rounded border p-2 text-sm"
                    value={term}
                    onChange={(e) => setTerm(e.target.value)}
                    disabled={saving}
                    placeholder="Buscar por serie, artículo o modelo"
                  />
                </label>
                {depositos.length ? (
                  <label className="block sm:w-40">
                    <div className="mb-1 text-sm text-gray-700">Depósito</div>
                    <select
                      className="w-full rounded border p-2 text-sm"
                      value={deposito}
                      onChange={(e) => setDeposito(e.target.value)}
                      disabled={saving}
                    >
                      <option value="">{depositos.join(" + ")}</option>
                      {depositos.map((d) => (
                        <option key={d} value={d}>
                          {d}
                        </option>
                      ))}
                    </select>
                  </label>
                ) : null}
              </div>

              <div className="mb-3 max-h-56 overflow-y-auto rounded border">
                {buscando && !candidatos.length ? (
                  <div className="px-3 py-2 text-sm text-gray-500">Consultando stock...</div>
                ) : candidatos.length ? (
                  candidatos.map((item) => (
                    <CandidateRow
                      key={candidateKey(item)}
                      item={item}
                      selected={candidateKey(elegido) === candidateKey(item)}
                      onPick={setElegido}
                      disabled={saving}
                    />
                  ))
                ) : (
                  <div className="px-3 py-2 text-sm text-gray-500">
                    No hay unidades con stock disponible que coincidan. Se listan sólo artículos de
                    equipo en los depósitos de venta.
                  </div>
                )}
              </div>
              </>
              ) : null}

              <label className="mb-2 block">
                <div className="mb-1 text-sm text-gray-700">
                  Qué pasa con el equipo recibido
                </div>
                <select
                  className="w-full rounded border p-2 text-sm"
                  value={destino}
                  onChange={(e) => setDestino(e.target.value)}
                  disabled={saving}
                >
                  {SWAP_DESTINO_OPTIONS.map((opt) => (
                    <option
                      key={opt.value}
                      value={opt.value}
                      disabled={!canSetDestino && opt.patrimonial}
                    >
                      {opt.label}
                    </option>
                  ))}
                </select>
                <div className="mt-1 text-xs text-gray-600">
                  Los tres destinos de “Propiedad de SEPID” pasan el equipo a nuestro nombre;
                  “se decide después” lo deja como del cliente.
                </div>
                {!canSetDestino ? (
                  <div className="mt-1 text-xs text-gray-500">
                    El destino del equipo recibido lo define jefatura: vos registrás el cambio y
                    queda como “se decide después”.
                  </div>
                ) : null}
              </label>

              {necesitaMg ? (
                <label className="mb-2 block">
                  <div className="mb-1 text-sm text-gray-700">
                    Número interno MG <span className="text-gray-500">(opcional)</span>
                  </div>
                  <input
                    className="w-full rounded border p-2 text-sm"
                    value={numeroMg}
                    onChange={(e) => setNumeroMg(e.target.value)}
                    disabled={saving}
                    placeholder="MG 0001"
                  />
                  <div className="mt-1 text-xs text-gray-600">
                    Si lo dejás vacío el equipo queda igual como propio de MG BIO y se le
                    puede dar de baja a Desguace. El número se puede asignar después.
                  </div>
                </label>
              ) : null}

              {elegido || !conEntrega ? (
                <div className="mt-3 rounded border border-emerald-300 bg-emerald-50 p-3 text-sm text-emerald-900">
                  <div className="font-semibold">Revisá antes de confirmar</div>
                  {conEntrega ? (
                    <div className="mt-1">
                      <span className="font-medium">Sale al cliente:</span> {elegido.partida}
                      {[elegido.marca, elegido.modelo].filter(Boolean).length
                        ? ` (${[elegido.marca, elegido.modelo].filter(Boolean).join(" ")})`
                        : ""}
                    </div>
                  ) : (
                    <div className="mt-1">
                      <span className="font-medium">Sale al cliente:</span> nada. Es una parte de
                      pago sin entrega de equipo.
                    </div>
                  )}
                  <div>
                    <span className="font-medium">Queda en SEPID:</span> {recibido}
                  </div>
                  {destino === SWAP_DESTINO.DESGUACE ? (
                    <div className="mt-1 text-xs font-medium text-amber-800">
                      Pasa a propiedad de SEPID y se mueve al depósito de Desguace (DES) en
                      Bejerman, igual que una baja.
                    </div>
                  ) : null}
                  {destino === SWAP_DESTINO.VENTA ? (
                    <div className="mt-1 text-xs font-medium text-amber-800">
                      Pasa a propiedad de SEPID y se transfiere al depósito de ventas (VAL) en
                      Bejerman, para poder venderlo.
                    </div>
                  ) : null}
                  {destino === SWAP_DESTINO.MG ? (
                    <div className="mt-1 text-xs">
                      Pasa a propiedad de SEPID y queda en el parque propio
                      {numeroMg.trim() ? ` como ${numeroMg.trim()}` : ""}. No se mueve de depósito.
                    </div>
                  ) : null}
                  {destino === SWAP_DESTINO.PENDIENTE ? (
                    <div className="mt-1 text-xs font-medium text-amber-800">
                      Ojo: sin destino el equipo NO pasa a propiedad de SEPID. Sigue figurando
                      como del cliente y no se mueve el stock hasta que alguien lo defina.
                    </div>
                  ) : null}
                  <div className="mt-1 text-xs">
                    {conEntrega
                      ? "El equipo que sale se factura por su propia orden de entrega (remito RT). Esta OS no genera remito ni mueve stock al cliente."
                      : "Esta OS no genera remito ni mueve stock al cliente: el equipo se queda acá."}
                  </div>
                </div>
              ) : null}
            </>
          ) : null}

          {err ? (
            <div className="mt-3 rounded border border-red-300 bg-red-100 p-2 text-sm text-red-800">
              {err}
            </div>
          ) : null}

          {notice ? (
            <div className={`mt-3 rounded border p-2 text-sm ${noticeClass}`}>{notice}</div>
          ) : null}
        </div>

        <div className="flex justify-end gap-2 border-t px-4 py-3">
          {state.canUndo && !notice ? (
            <button
              type="button"
              className="mr-auto rounded border border-red-300 px-3 py-1.5 text-sm text-red-700 hover:bg-red-50 disabled:opacity-50"
              onClick={deshacer}
              disabled={saving}
            >
              {state.sinEntrega ? "Deshacer parte de pago" : "Deshacer cambio"}
            </button>
          ) : null}
          <button
            type="button"
            className="rounded border px-3 py-1.5 text-sm"
            onClick={onClose}
            disabled={saving}
          >
            {notice ? "Cerrar" : "Cancelar"}
          </button>
          {!notice ? (
            <button
              type="button"
              className="rounded bg-blue-600 px-3 py-1.5 text-sm text-white hover:bg-blue-700 disabled:opacity-50"
              onClick={confirmar}
              disabled={!puedeConfirmar}
            >
              {saving ? "Guardando..." : conEntrega ? "Registrar cambio" : "Registrar parte de pago"}
            </button>
          ) : null}
        </div>
      </div>
    </div>
  );
}
