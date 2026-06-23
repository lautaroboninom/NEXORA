import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Html5Qrcode, Html5QrcodeSupportedFormats } from "html5-qrcode";
import {
  getIngresoBarcodeBlob,
  lookupScan,
  postEntregarIngreso,
  postIngresoRisEmitir,
} from "../lib/api";
import { openPdfBlob } from "../lib/pdf";
import {
  isRisGenerated,
  isRisRegistered,
  openRisPrintablePdf,
  risRemitoFrom,
  waitForRisPdfBlob,
} from "../lib/ris-print";
import { useAuth } from "../context/AuthContext";
import { can, PERMISSION_CODES } from "../lib/permissions";
import { formatOS } from "../lib/ui-helpers";
import DeviceIdentifier from "./DeviceIdentifier.jsx";
import RisProgressModal, {
  waitForRisProgressMinimum,
  waitForRisProgressPaint,
} from "./RisProgressModal.jsx";

const emptyEntrega = { remito_salida: "", retira_persona: "", serial_confirm: "" };
const SCAN_RESET_MS = 120;
const SCAN_MAX_MS = 1500;
const SCAN_MIN_LEN = 3;

const MODE_CONFIG = {
  ingreso: {
    label: "Ingreso / RIS",
    title: "Ingreso con lector",
    subtitle: "Escanee N/S, interno u OS para abrir el ingreso, emitir RIS o imprimir etiqueta.",
    input: "Escanear serie, interno u OS",
  },
  egreso: {
    label: "Egreso / RSS",
    title: "Egreso con lector",
    subtitle: "Escanee la OS o el equipo y cargue el RSS ya emitido para registrar la entrega.",
    input: "Escanear OS, serie o interno",
  },
};

const safeText = (value, fallback = "-") => (value == null || value === "" ? fallback : String(value));

const estadoLabel = (value) => {
  const raw = String(value || "").trim();
  if (!raw) return "-";
  return raw.replace(/_/g, " ");
};

const formatFecha = (value) => {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return safeText(value);
  return date.toLocaleString("es-AR", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
};

function isEditableTarget(target) {
  return (
    target instanceof HTMLInputElement ||
    target instanceof HTMLTextAreaElement ||
    target instanceof HTMLSelectElement ||
    !!target?.isContentEditable
  );
}

export default function QrScanCard({
  receptionMode = false,
  title,
  subtitle,
  onDelivered,
}) {
  const nav = useNavigate();
  const { user } = useAuth();
  const inputRef = useRef(null);
  const scannerRef = useRef(null);
  const scanLockRef = useRef(false);
  const startLockRef = useRef(false);
  const fileInputRef = useRef(null);
  const remitoInputRef = useRef(null);
  const retiraInputRef = useRef(null);
  const serialConfirmInputRef = useRef(null);
  const scanBufferRef = useRef("");
  const scanStartedAtRef = useRef(0);
  const scanLastKeyAtRef = useRef(0);
  const scanResetTimerRef = useRef(null);
  const [open, setOpen] = useState(false);
  const [code, setCode] = useState("");
  const [pendingScannedCode, setPendingScannedCode] = useState("");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [cameraError, setCameraError] = useState("");
  const [cameraSupported, setCameraSupported] = useState(false);
  const [mediaSupported, setMediaSupported] = useState(false);
  const [secureContext, setSecureContext] = useState(true);
  const [cameraActive, setCameraActive] = useState(false);
  const [fileDecoding, setFileDecoding] = useState(false);
  const [result, setResult] = useState(null);
  const [entrega, setEntrega] = useState(emptyEntrega);
  const [saving, setSaving] = useState(false);
  const [deliverErr, setDeliverErr] = useState("");
  const [deliverOk, setDeliverOk] = useState("");
  const [scanMode, setScanMode] = useState("ingreso");
  const [risBusy, setRisBusy] = useState(false);
  const [risProgressStatus, setRisProgressStatus] = useState("");
  const [barcodeBusy, setBarcodeBusy] = useState(false);
  const [actionOk, setActionOk] = useState("");
  const [manualRisPrint, setManualRisPrint] = useState(null);

  const config = MODE_CONFIG[scanMode] || MODE_CONFIG.ingreso;
  const canCreateIngreso = can(user, PERMISSION_CODES.ACTION_INGRESO_CREATE) || can(user, PERMISSION_CODES.PAGE_NEW_INGRESO);
  const canEditDelivery = receptionMode || can(user, PERMISSION_CODES.ACTION_INGRESO_EDIT_DELIVERY);
  const canEmitRis = can(user, PERMISSION_CODES.ACTION_INGRESO_EMIT_INGRESS_ORDER);
  const canPrintBarcode = can(user, PERMISSION_CODES.ACTION_INGRESO_PRINT_BARCODE);
  const cardTitle = title || (receptionMode ? "Lector de recepción" : "Lectura de QR");
  const cardSubtitle = subtitle || (
    receptionMode
      ? "Ingreso RIS y egreso RSS con lector de código de barras."
      : "Escanear código QR o de barras."
  );

  const resetState = () => {
    setCode("");
    setPendingScannedCode("");
    setErr("");
    setCameraError("");
    setFileDecoding(false);
    setResult(null);
    setEntrega(emptyEntrega);
    setSaving(false);
    setDeliverErr("");
    setDeliverOk("");
    setRisBusy(false);
    setBarcodeBusy(false);
    setActionOk("");
    setManualRisPrint(null);
  };

  const openModal = (mode = scanMode) => {
    setScanMode(mode);
    resetState();
    setOpen(true);
  };

  const closeModal = () => {
    void stopCamera();
    setPendingScannedCode("");
    setOpen(false);
  };

  const clearScanBuffer = () => {
    scanBufferRef.current = "";
    scanStartedAtRef.current = 0;
    scanLastKeyAtRef.current = 0;
    if (scanResetTimerRef.current) {
      clearTimeout(scanResetTimerRef.current);
      scanResetTimerRef.current = null;
    }
  };

  const armScanReset = () => {
    if (scanResetTimerRef.current) {
      clearTimeout(scanResetTimerRef.current);
    }
    scanResetTimerRef.current = setTimeout(() => {
      clearScanBuffer();
    }, SCAN_RESET_MS * 2);
  };

  const openQrCapture = () => {
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
      fileInputRef.current.click();
    }
  };

  const readerId = "qr-reader";
  const ensureScanner = () => {
    if (!scannerRef.current) {
      scannerRef.current = new Html5Qrcode(readerId);
    }
    return scannerRef.current;
  };

  const evaluateCameraSupport = () => {
    const supportsMedia = typeof navigator !== "undefined" && !!navigator.mediaDevices?.getUserMedia;
    const secure = typeof window !== "undefined" ? window.isSecureContext : false;
    setMediaSupported(supportsMedia);
    setSecureContext(secure);
    const canLive = supportsMedia && secure;
    setCameraSupported(canLive);
    return { supportsMedia, secure, canLive };
  };

  useEffect(() => {
    if (!open) {
      void stopCamera();
      return;
    }
    const isCoarse = typeof window !== "undefined" && !!window.matchMedia?.("(pointer: coarse)")?.matches;
    const ua = typeof navigator !== "undefined" ? navigator.userAgent || "" : "";
    const isMobileUA = /Android|iPhone|iPad|iPod/i.test(ua);
    const shouldAuto = isCoarse || isMobileUA;
    const { canLive } = evaluateCameraSupport();
    if (shouldAuto && canLive) {
      void startCamera();
    } else {
      const id = setTimeout(() => {
        inputRef.current?.focus();
        inputRef.current?.select();
      }, 50);
      return () => clearTimeout(id);
    }
  }, [open]);

  useEffect(() => {
    return () => {
      void stopCamera();
    };
  }, []);

  const lookupCode = async (value) => {
    const trimmed = (value || "").trim();
    if (!trimmed || loading) return;
    setLoading(true);
    setErr("");
    setResult(null);
    setDeliverErr("");
    setDeliverOk("");
    setActionOk("");
    try {
      const res = await lookupScan(trimmed);
      setResult(res);
      setEntrega(emptyEntrega);
    } catch (e2) {
      setErr(e2?.message || "No se pudo leer el código.");
    } finally {
      setLoading(false);
      setTimeout(() => inputRef.current?.select(), 50);
    }
  };

  const onLookup = async (e) => {
    e.preventDefault();
    await lookupCode(code);
  };

  useEffect(() => {
    if (!open || !pendingScannedCode || loading) return;
    const scanned = pendingScannedCode;
    setPendingScannedCode("");
    setCode(scanned);
    void lookupCode(scanned);
  }, [open, pendingScannedCode, loading]);

  useEffect(() => {
    const onWindowKeyDown = (event) => {
      if (event.defaultPrevented || event.repeat || event.isComposing) return;
      if (event.ctrlKey || event.altKey || event.metaKey) return;

      const target = event.target;
      if (isEditableTarget(target)) return;

      if (event.key === "Enter") {
        const scanned = scanBufferRef.current.trim();
        const elapsed = scanStartedAtRef.current ? Date.now() - scanStartedAtRef.current : 0;
        if (scanned && scanned.length >= SCAN_MIN_LEN && elapsed <= SCAN_MAX_MS) {
          event.preventDefault();
          if (!open) {
            resetState();
            setScanMode(receptionMode ? "ingreso" : scanMode);
            setOpen(true);
          }
          setPendingScannedCode(scanned);
        }
        clearScanBuffer();
        return;
      }

      if (event.key.length !== 1) return;
      const now = Date.now();
      if (!scanLastKeyAtRef.current || now - scanLastKeyAtRef.current > SCAN_RESET_MS) {
        scanBufferRef.current = event.key;
        scanStartedAtRef.current = now;
      } else {
        scanBufferRef.current += event.key;
      }
      scanLastKeyAtRef.current = now;
      armScanReset();
    };

    window.addEventListener("keydown", onWindowKeyDown);
    return () => {
      window.removeEventListener("keydown", onWindowKeyDown);
      clearScanBuffer();
    };
  }, [open]);

  const stopCamera = async () => {
    startLockRef.current = false;
    scanLockRef.current = false;
    const scanner = scannerRef.current;
    if (scanner) {
      try {
        await scanner.stop();
      } catch (e) {
        // ignore stop errors when not running
      }
      try {
        scanner.clear();
      } catch (e) {
        // ignore clear errors
      }
    }
    setCameraActive(false);
  };

  const handleScanSuccess = async (decodedText) => {
    if (scanLockRef.current) return;
    scanLockRef.current = true;
    setCode(decodedText);
    await lookupCode(decodedText);
    await stopCamera();
    scanLockRef.current = false;
  };

  const startCamera = async () => {
    if (cameraActive || startLockRef.current) return;
    const { canLive, secure } = evaluateCameraSupport();
    if (!canLive) {
      setCameraError(
        secure
          ? "La cámara no está disponible en este dispositivo."
          : "La lectura automática requiere HTTPS."
      );
      return;
    }
    setCameraError("");
    startLockRef.current = true;
    try {
      const scanner = ensureScanner();
      await scanner.start(
        { facingMode: "environment" },
        {
          fps: 10,
          qrbox: { width: 240, height: 240 },
          formatsToSupport: [Html5QrcodeSupportedFormats.QR_CODE],
        },
        (decodedText) => {
          void handleScanSuccess(decodedText);
        },
        () => {}
      );
      setCameraActive(true);
    } catch (e2) {
      setCameraError(e2?.message || "No se pudo abrir la cámara.");
      setCameraActive(false);
    } finally {
      startLockRef.current = false;
    }
  };

  const decodeQrFromFile = async (file) => {
    if (!file) return;
    setFileDecoding(true);
    setCameraError("");
    try {
      await stopCamera();
      const scanner = ensureScanner();
      const decodedText = await scanner.scanFile(file, true);
      setCode(decodedText);
      await lookupCode(decodedText);
    } catch (e2) {
      setCameraError(e2?.message || "No se pudo leer el QR.");
    } finally {
      setFileDecoding(false);
    }
  };

  const onQrFileChange = (event) => {
    const file = event.target?.files?.[0];
    if (file) decodeQrFromFile(file);
  };

  const ingreso = result?.ingreso || null;
  const device = result?.device || null;
  const flags = result?.flags || {};
  const estado = String(ingreso?.estado || "").toLowerCase();
  const ingresoEnCurso = Boolean(ingreso?.ingreso_en_curso);
  const isLiberado = estado === "liberado" || estado === "vendido_pendiente_entrega";
  const isEntregado = estado === "entregado" || estado === "vendido_entregado";
  const requiereSerial = String(ingreso?.resolucion || "").toLowerCase() === "cambio";
  const hoyLabel = new Date().toLocaleDateString("es-AR");
  const canRegisterDelivery = !!ingreso?.id && isLiberado && !isEntregado && canEditDelivery;
  const canPrintIngreso = !!ingreso?.id && canPrintBarcode;
  const canCreateFromDevice = canCreateIngreso && !ingresoEnCurso;
  const shouldShowIngresoCard = !!ingreso && (ingresoEnCurso || !device);
  const canViewLastIngresoFromDevice = !!ingreso?.id && !!device && !ingresoEnCurso;

  useEffect(() => {
    if (!open || scanMode !== "egreso" || !canRegisterDelivery || deliverOk) return;
    const id = setTimeout(() => {
      remitoInputRef.current?.focus();
      remitoInputRef.current?.select();
    }, 80);
    return () => clearTimeout(id);
  }, [open, scanMode, canRegisterDelivery, deliverOk, ingreso?.id]);

  const propiedadLabel = () => {
    if (flags?.mg_inactivo_venta) return "MG histórico inactivo por venta";
    if (flags?.vendido) return "Cliente";
    if (flags?.es_propietario_mg) {
      return device?.alquilado ? "Propio (alquilado)" : "Propio";
    }
    return "Cliente";
  };

  const alquilerCliente = (() => {
    const name = (ingreso?.alquiler_a || "").trim();
    return ingreso?.alquilado && name ? name : "";
  })();

  const goNuevoIngreso = (prefill) => {
    if (prefill) {
      nav("/ingresos/nuevo", { state: { prefill } });
    } else {
      const serie = (result?.normalized || code || "").trim();
      nav(`/ingresos/nuevo?serie=${encodeURIComponent(serie)}`);
    }
    closeModal();
  };

  const onEntregar = async () => {
    if (!ingreso?.id) return;
    const remito = (entrega.remito_salida || "").trim();
    const retira = (entrega.retira_persona || "").trim();
    const serialConfirm = (entrega.serial_confirm || "").trim();
    if (!remito) {
      setDeliverErr("Remito requerido.");
      return;
    }
    if (!retira) {
      setDeliverErr("Persona que retira requerida.");
      return;
    }
    if (requiereSerial && !serialConfirm) {
      setDeliverErr("Serie requerida para cambio.");
      return;
    }
    setSaving(true);
    setDeliverErr("");
    setDeliverOk("");
    setActionOk("");
    try {
      await postEntregarIngreso(ingreso.id, {
        remito_salida: remito,
        retira_persona: retira,
        ...(requiereSerial ? { serial_confirm: serialConfirm } : {}),
      });
      setDeliverOk("Entrega registrada con RSS.");
      onDelivered?.();
      setResult((prev) => {
        if (!prev || !prev.ingreso) return prev;
        const nextEstado = String(prev.ingreso.estado || "").toLowerCase() === "vendido_pendiente_entrega"
          ? "vendido_entregado"
          : "entregado";
        return {
          ...prev,
          ingreso: {
            ...prev.ingreso,
            estado: nextEstado,
            fecha_entrega: new Date().toISOString(),
          },
        };
      });
    } catch (e2) {
      setDeliverErr(e2?.message || "No se pudo marcar la entrega.");
    } finally {
      setSaving(false);
    }
  };

  const imprimirRis = async () => {
    if (!ingreso?.id || risBusy || !canEmitRis) return;
    setErr("");
    setActionOk("");
    setManualRisPrint(null);
    if (isRisRegistered(ingreso)) {
      const remito = risRemitoFrom(ingreso);
      setActionOk(remito ? `Remito ${remito} registrado en Bejerman.` : "Remito registrado en Bejerman.");
      return;
    }
    const progressStartedAt = Date.now();
    setRisBusy(true);
    setRisProgressStatus(isRisGenerated(ingreso) ? "Buscando PDF del RIS..." : "Emitiendo RIS en Bejerman...");
    try {
      await waitForRisProgressPaint();
      const risSource = isRisGenerated(ingreso) ? ingreso : await postIngresoRisEmitir(ingreso.id);
      const remito = risRemitoFrom(risSource);
      const blob = await waitForRisPdfBlob(ingreso.id, risSource, {
        onProgress: (progress) => setRisProgressStatus(progress?.status || "Preparando PDF del RIS..."),
      });
      setRisProgressStatus("Abriendo impresión...");
      const opened = openRisPrintablePdf(blob, risSource).opened;
      if (opened) {
        setActionOk(remito ? `RIS ${remito} listo para imprimir.` : "RIS listo para imprimir.");
      } else {
        setManualRisPrint({ blob, source: risSource, remito });
        setErr("El PDF del RIS ya está listo, pero el navegador bloqueó la ventana automática. Use Abrir e imprimir.");
      }
    } catch (e2) {
      setErr(e2?.message || "No se pudo emitir o reimprimir el RIS.");
    } finally {
      await waitForRisProgressMinimum(progressStartedAt);
      setRisBusy(false);
      setRisProgressStatus("");
      setTimeout(() => inputRef.current?.focus(), 50);
    }
  };

  const abrirRisManualPendiente = () => {
    if (!manualRisPrint?.blob) return;
    const opened = openRisPrintablePdf(manualRisPrint.blob, manualRisPrint.source || ingreso).opened;
    if (opened) {
      setManualRisPrint(null);
      setErr("");
      setActionOk(manualRisPrint.remito ? `RIS ${manualRisPrint.remito} listo para imprimir.` : "RIS listo para imprimir.");
    } else {
      setErr("El navegador volvió a bloquear la ventana. Habilite ventanas emergentes para NEXORA y reintente.");
    }
    setTimeout(() => inputRef.current?.focus(), 50);
  };

  const imprimirEtiqueta = async () => {
    if (!ingreso?.id || barcodeBusy || !canPrintBarcode) return;
    setBarcodeBusy(true);
    setErr("");
    setActionOk("");
    try {
      const blob = await getIngresoBarcodeBlob(ingreso.id);
      openPdfBlob(blob);
      setActionOk("Etiqueta lista para imprimir.");
    } catch (e2) {
      setErr(e2?.message || "No se pudo imprimir el código de barras.");
    } finally {
      setBarcodeBusy(false);
      setTimeout(() => inputRef.current?.focus(), 50);
    }
  };

  return (
    <div className="rounded border bg-white p-4">
      <RisProgressModal
        open={risBusy}
        title="Emitiendo RIS"
        status={risProgressStatus || "Emitiendo RIS en Bejerman..."}
      />
      <div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-center">
        <div>
          <div className="font-semibold">{cardTitle}</div>
          <div className="text-xs text-gray-500">{cardSubtitle}</div>
        </div>
        {receptionMode ? (
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              className="rounded bg-blue-600 px-3 py-2 text-sm text-white hover:bg-blue-700"
              onClick={() => openModal("ingreso")}
            >
              Ingreso / RIS
            </button>
            <button
              type="button"
              className="rounded bg-emerald-600 px-3 py-2 text-sm text-white hover:bg-emerald-700"
              onClick={() => openModal("egreso")}
            >
              Egreso / RSS
            </button>
          </div>
        ) : (
          <button
            type="button"
            className="rounded bg-emerald-600 px-3 py-2 text-sm text-white hover:bg-emerald-700"
            onClick={() => openModal(scanMode)}
          >
            Abrir
          </button>
        )}
      </div>

      {open && (
        <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/40 p-3 sm:p-4">
          <div className="my-3 max-h-[calc(100dvh-1.5rem)] w-full max-w-2xl overflow-y-auto overscroll-contain rounded bg-white p-4 shadow-lg sm:my-4 sm:max-h-[calc(100dvh-2rem)]">
            <div className="mb-3 flex items-start justify-between gap-3">
              <div>
                <div className="text-lg font-semibold">{config.title}</div>
                <div className="text-xs text-gray-500">{config.subtitle}</div>
              </div>
              <button className="px-2 py-1 rounded border" onClick={closeModal}>
                Cerrar
              </button>
            </div>

            {receptionMode && (
              <div className="mb-3 inline-flex overflow-hidden rounded border bg-white text-sm">
                {Object.entries(MODE_CONFIG).map(([key, item]) => (
                  <button
                    key={key}
                    type="button"
                    className={`px-3 py-2 ${
                      scanMode === key ? "bg-blue-600 text-white" : "text-gray-700 hover:bg-gray-50"
                    }`}
                    onClick={() => setScanMode(key)}
                  >
                    {item.label}
                  </button>
                ))}
              </div>
            )}

            <input
              ref={fileInputRef}
              type="file"
              accept="image/*"
              capture="environment"
              onChange={onQrFileChange}
              className="hidden"
            />

            <div className={mediaSupported ? "mb-3" : "mb-3 hidden"}>
              <div className="flex items-center justify-between gap-2">
                <div className="text-sm text-gray-600">
                  {cameraActive ? "Cámara activa (QR)" : "Cámara"}
                </div>
                <button
                  type="button"
                  className="px-3 py-1.5 rounded border text-sm"
                  onClick={cameraActive ? () => void stopCamera() : () => void startCamera()}
                  disabled={!cameraSupported}
                >
                  {cameraActive ? "Cerrar cámara" : "Abrir cámara"}
                </button>
              </div>
              <div className="mt-2 border rounded overflow-hidden bg-black/5">
                <div id={readerId} className="w-full h-48 md:h-64"></div>
              </div>
            </div>

            {!mediaSupported && (
              <div className="mb-3 text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded p-2">
                Tu navegador no permite acceso a cámara. Puede cargar una imagen con QR.
                <div className="mt-2">
                  <button
                    type="button"
                    className="px-3 py-1.5 rounded border text-xs"
                    onClick={openQrCapture}
                    disabled={fileDecoding}
                  >
                    {fileDecoding ? "Leyendo QR..." : "Abrir cámara (QR)"}
                  </button>
                </div>
              </div>
            )}
            {fileDecoding && !cameraError && (
              <div className="mb-3 text-xs text-gray-500">
                Procesando imagen...
              </div>
            )}
            {cameraError && (
              <div className="mb-3 text-xs text-red-700 bg-red-50 border border-red-200 rounded p-2">
                {cameraError}
              </div>
            )}

            <form onSubmit={onLookup} className="flex flex-col md:flex-row gap-2">
              <input
                ref={inputRef}
                className="w-full rounded border p-3 text-base"
                placeholder={config.input}
                value={code}
                onChange={(e) => setCode(e.target.value)}
                aria-label="Lectura de código"
                autoComplete="off"
              />
              <button className="px-3 py-2 rounded bg-blue-600 text-white hover:bg-blue-700">
                Buscar
              </button>
              <button
                type="button"
                className="px-3 py-2 rounded border"
                onClick={resetState}
              >
                Limpiar
              </button>
            </form>

            {loading && <div className="text-sm text-gray-500 mt-3">Buscando...</div>}
            {err && <div className="bg-red-100 text-red-800 border border-red-300 rounded p-2 mt-3">{err}</div>}
            {manualRisPrint?.blob && (
              <div className="mt-3 rounded border border-amber-300 bg-amber-50 p-2 text-sm text-amber-900">
                <div>El PDF del RIS está listo. Si no se abrió automáticamente, imprímalo desde este botón.</div>
                <button
                  type="button"
                  onClick={abrirRisManualPendiente}
                  className="mt-2 rounded bg-amber-700 px-3 py-1.5 font-medium text-white hover:bg-amber-800"
                >
                  Abrir e imprimir RIS
                </button>
              </div>
            )}
            {actionOk && (
              <div className="mt-3 rounded border border-emerald-200 bg-emerald-50 p-2 text-sm text-emerald-800">
                {actionOk}
              </div>
            )}

            {!loading && result && (
              <div className="mt-4 space-y-4">
                {shouldShowIngresoCard && (
                  <div className={ingresoEnCurso ? "rounded border border-amber-300 bg-amber-50 p-3" : "rounded border p-3"}>
                    <div className="mb-2">
                      <div className={ingresoEnCurso ? "font-semibold text-amber-900" : "font-semibold"}>
                        {ingresoEnCurso ? "Servicio en curso" : "Ingreso encontrado"}
                      </div>
                      {ingresoEnCurso && (
                        <div className="mt-1 text-sm text-amber-800">
                          Este equipo ya tiene un ingreso activo. No se sugiere crear otro ingreso porque el alta será bloqueada.
                        </div>
                      )}
                    </div>
                    <div className="text-sm text-gray-700 grid grid-cols-1 md:grid-cols-2 gap-2">
                      <div>OS: {formatOS(ingreso)}</div>
                      <div>Estado: {estadoLabel(ingreso.estado)}</div>
                      <div>Fecha de ingreso: {formatFecha(ingreso.fecha_ingreso)}</div>
                      <div>Cliente: {safeText(ingreso.razon_social)}</div>
                      <div>Equipo: {safeText(ingreso.marca)} {safeText(ingreso.modelo)}</div>
                      <div>Serie: <DeviceIdentifier row={ingreso} /></div>
                      <div>Tipo: {safeText(ingreso.tipo_equipo)}</div>
                    </div>
                    <div className="mt-3 flex flex-wrap items-center gap-2">
                      <button
                        className="px-3 py-1.5 rounded border"
                        onClick={() => {
                          nav(`/ingresos/${ingreso.id}`);
                          closeModal();
                        }}
                      >
                        Ver hoja de servicio
                      </button>
                      {canPrintIngreso && (
                        <button
                          type="button"
                          className="rounded border px-3 py-1.5 text-sm hover:bg-gray-50 disabled:opacity-50"
                          onClick={imprimirEtiqueta}
                          disabled={barcodeBusy}
                        >
                          {barcodeBusy ? "Preparando etiqueta..." : "Imprimir etiqueta"}
                        </button>
                      )}
                      {canEmitRis && ingreso?.id && (
                        <button
                          type="button"
                          className="rounded border px-3 py-1.5 text-sm text-blue-700 hover:bg-blue-50 disabled:opacity-50"
                          onClick={imprimirRis}
                          disabled={risBusy}
                        >
                          {risBusy
                            ? "Preparando RIS..."
                            : isRisRegistered(ingreso)
                              ? "Remito registrado"
                              : isRisGenerated(ingreso)
                                ? "Ver RIS"
                                : "Emitir RIS"}
                        </button>
                      )}
                    </div>

                    {scanMode === "egreso" && !canRegisterDelivery && !isEntregado && (
                      <div className="mt-3 rounded border border-amber-200 bg-amber-50 p-2 text-sm text-amber-800">
                        El equipo todavía no está liberado para registrar egreso.
                      </div>
                    )}

                    {canRegisterDelivery && (
                      <div className="mt-4 border-t pt-4">
                        <div className="font-semibold text-emerald-700">
                          {estado === "vendido_pendiente_entrega" ? "Venta pendiente de entrega" : "Egreso / RSS"}
                        </div>
                        <div className="text-xs text-gray-500">
                          Fecha de entrega: {hoyLabel} (auto). El RSS se genera en Portal/Bejerman y acá se registra la entrega.
                        </div>
                        {deliverOk && (
                          <div className="bg-emerald-100 text-emerald-800 border border-emerald-300 rounded p-2 mt-2">
                            {deliverOk}
                          </div>
                        )}
                        {deliverErr && (
                          <div className="bg-red-100 text-red-800 border border-red-300 rounded p-2 mt-2">
                            {deliverErr}
                          </div>
                        )}
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mt-3">
                          <label className="block">
                            <div className="text-sm text-gray-700 mb-1">Remito/RSS</div>
                            <input
                              ref={remitoInputRef}
                              className="border rounded p-2 w-full"
                              value={entrega.remito_salida}
                              onChange={(e) => setEntrega((s) => ({ ...s, remito_salida: e.target.value }))}
                              onKeyDown={(e) => {
                                if (e.key === "Enter") {
                                  e.preventDefault();
                                  retiraInputRef.current?.focus();
                                  retiraInputRef.current?.select();
                                }
                              }}
                            />
                          </label>
                          <label className="block">
                            <div className="text-sm text-gray-700 mb-1">Persona que retira</div>
                            <input
                              ref={retiraInputRef}
                              className="border rounded p-2 w-full"
                              value={entrega.retira_persona}
                              onChange={(e) => setEntrega((s) => ({ ...s, retira_persona: e.target.value }))}
                              onKeyDown={(e) => {
                                if (e.key === "Enter") {
                                  e.preventDefault();
                                  if (requiereSerial) {
                                    serialConfirmInputRef.current?.focus();
                                    serialConfirmInputRef.current?.select();
                                  } else {
                                    void onEntregar();
                                  }
                                }
                              }}
                            />
                          </label>
                          {requiereSerial && (
                            <label className="block md:col-span-2">
                              <div className="text-sm text-gray-700 mb-1">Serie confirmación (cambio)</div>
                              <input
                                ref={serialConfirmInputRef}
                                className="border rounded p-2 w-full"
                                value={entrega.serial_confirm}
                                onChange={(e) => setEntrega((s) => ({ ...s, serial_confirm: e.target.value }))}
                                onKeyDown={(e) => {
                                  if (e.key === "Enter") {
                                    e.preventDefault();
                                    void onEntregar();
                                  }
                                }}
                              />
                            </label>
                          )}
                        </div>
                        <div className="mt-3 flex items-center gap-2">
                          <button
                            className="px-3 py-2 rounded bg-emerald-600 text-white hover:bg-emerald-700 disabled:opacity-50"
                            disabled={saving}
                            onClick={onEntregar}
                          >
                            {saving ? "Registrando..." : "Registrar egreso"}
                          </button>
                        </div>
                      </div>
                    )}
                  </div>
                )}

                {device && (
                  <div className="border rounded p-3">
                    <div className="font-semibold mb-2">Equipo encontrado</div>
                    <div className="text-sm text-gray-700 grid grid-cols-1 md:grid-cols-2 gap-2">
                      <div>Propiedad: {propiedadLabel()}</div>
                      <div>Cliente: {safeText(device.customer_nombre)}</div>
                      <div>Serie: <DeviceIdentifier row={{ ...device, ...flags }} /></div>
                      <div>Equipo: {safeText(device.marca)} {safeText(device.modelo)}</div>
                      <div>Alquilado: {device.alquilado ? "Sí" : "No"}</div>
                      <div>Alquiler a: {safeText(device.alquiler_a)}</div>
                      {flags?.mg_inactivo_venta && (
                        <div className="md:col-span-2 text-amber-700">
                          MG histórico inactivo por venta. No operativo para Nuevo Ingreso.
                        </div>
                      )}
                    </div>
                    {ingresoEnCurso && (
                      <div className="mt-3 rounded border border-amber-200 bg-amber-50 p-2 text-sm text-amber-800">
                        Para continuar, abrí la hoja de servicio existente o reimprimí RIS/etiqueta desde este resultado.
                      </div>
                    )}
                    {(canViewLastIngresoFromDevice || canCreateFromDevice) && (
                      <div className="mt-3 flex flex-wrap items-center gap-2">
                        {canViewLastIngresoFromDevice && (
                          <button
                            type="button"
                            className="rounded border px-3 py-1.5 text-sm hover:bg-gray-50"
                            onClick={() => {
                              nav(`/ingresos/${ingreso.id}`);
                              closeModal();
                            }}
                          >
                            Ver último ingreso
                          </button>
                        )}
                        {canCreateFromDevice && (
                          <button
                            className="px-3 py-1.5 rounded bg-blue-600 text-white hover:bg-blue-700"
                            onClick={() =>
                              goNuevoIngreso({
                            numero_serie: device.numero_serie || result?.normalized || code,
                            numero_interno: flags?.mg_inactivo_venta ? "" : (device.numero_interno || ""),
                            marca_id: device.marca_id,
                            marca: device.marca,
                            marca_nombre: device.marca,
                            model_id: device.model_id,
                            modelo_id: device.model_id,
                            modelo: device.modelo,
                            modelo_nombre: device.modelo,
                            tipo_equipo: device.tipo_equipo || ingreso?.tipo_equipo || "",
                            variante: device.variante || ingreso?.equipo_variante || "",
                            ...(alquilerCliente
                              ? {
                                  customer_id: null,
                                  customer_nombre: alquilerCliente,
                                  customer_cod: "",
                                  customer_telefono: "",
                                }
                              : {
                                  customer_id: device.customer_id,
                                  customer_nombre: device.customer_nombre,
                                  customer_cod: device.customer_cod,
                                  customer_telefono: device.customer_telefono,
                                }),
                            propietario_nombre: device.propietario_nombre,
                            propietario_contacto: device.propietario_contacto,
                            propietario_doc: device.propietario_doc,
                            alquilado: device.alquilado,
                            alquiler_a: device.alquiler_a,
                            es_propietario_mg: flags?.es_propietario_mg,
                            vendido: flags?.vendido,
                            mg_estado: flags?.mg_estado,
                            mg_inactivo_venta: !!flags?.mg_inactivo_venta,
                            mg_context_msg: flags?.mg_inactivo_venta
                              ? "MG histórico inactivo por venta; no operativo para nuevos ingresos."
                              : "",
                              })
                            }
                          >
                            Crear ingreso RIS con datos
                          </button>
                        )}
                      </div>
                    )}
                  </div>
                )}

                {!ingreso && !device && (
                  <div className="border rounded p-3">
                    <div className="font-semibold mb-2">Sin coincidencias</div>
                    <div className="text-sm text-gray-600">
                      {scanMode === "egreso"
                        ? "No se encontró una hoja de servicio para registrar el egreso."
                        : "No se encontró un equipo con ese código."}
                    </div>
                    <div className="mt-3">
                      {scanMode === "ingreso" && canCreateIngreso && (
                        <button
                          className="px-3 py-1.5 rounded bg-blue-600 text-white hover:bg-blue-700"
                          onClick={() => goNuevoIngreso(null)}
                        >
                          Crear nuevo ingreso
                        </button>
                      )}
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
