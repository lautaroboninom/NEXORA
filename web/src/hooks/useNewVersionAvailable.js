// web/src/hooks/useNewVersionAvailable.js
//
// Avisa cuando el servidor ya tiene un build distinto del que corre en esta pestaña.
// La comparación vive en `lib/app-version.js`; acá están el reloj y la consulta.
import { useEffect, useState } from "react";
import {
  buildFingerprint,
  isNewBuild,
  VERSION_CHECK_MIN_GAP_MS,
  VERSION_CHECK_MS,
} from "../lib/app-version";

// La huella propia se toma al arrancar, con el <head> tal como vino en el index.html:
// más tarde Vite puede sumarle los archivos que carga a demanda. En desarrollo no hay
// archivos con hash y queda vacía, así que nunca avisa.
const HUELLA_PROPIA =
  import.meta.env.PROD && typeof document !== "undefined"
    ? buildFingerprint(document.head.innerHTML)
    : "";

async function huellaDelServidor() {
  const res = await fetch("/index.html", { cache: "no-store" });
  if (!res.ok) return "";
  return buildFingerprint(await res.text());
}

export default function useNewVersionAvailable() {
  const [disponible, setDisponible] = useState(false);

  useEffect(() => {
    if (!HUELLA_PROPIA || disponible) return undefined;
    let cancelado = false;
    // Recién abierta, la pestaña tiene la versión del servidor: no hace falta preguntar.
    let ultimaConsulta = Date.now();

    const revisar = async () => {
      if (document.visibilityState === "hidden") return;
      const ahora = Date.now();
      if (ahora - ultimaConsulta < VERSION_CHECK_MIN_GAP_MS) return;
      ultimaConsulta = ahora;
      try {
        const servida = await huellaDelServidor();
        if (!cancelado && isNewBuild(HUELLA_PROPIA, servida)) setDisponible(true);
      } catch (_) {
        // Sin conexión o en pleno rebuild: se vuelve a probar en la próxima vuelta.
      }
    };

    const timer = window.setInterval(revisar, VERSION_CHECK_MS);
    window.addEventListener("focus", revisar);
    document.addEventListener("visibilitychange", revisar);
    return () => {
      cancelado = true;
      window.clearInterval(timer);
      window.removeEventListener("focus", revisar);
      document.removeEventListener("visibilitychange", revisar);
    };
  }, [disponible]);

  return disponible;
}
