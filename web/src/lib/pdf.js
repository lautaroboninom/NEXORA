export function openPdfBlob(blob, reservedWindow = null) {
  const url = URL.createObjectURL(blob);
  let opened = false;

  try {
    if (reservedWindow && !reservedWindow.closed) {
      reservedWindow.location.href = url;
      opened = true;
    } else {
      opened = !!window.open(url, "_blank", "noopener,noreferrer");
    }
  } catch (_) {
    opened = false;
  }

  setTimeout(() => URL.revokeObjectURL(url), 60_000);
  return opened;
}

export function reservePdfWindow({
  title = "REMITO",
  message = "Preparando PDF...",
} = {}) {
  let target = null;

  try {
    target = window.open("", "_blank");
    if (target) {
      try {
        target.opener = null;
      } catch (_) {}
      try {
        target.document.write(`<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <title>${title}</title>
  <style>
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; font-family: system-ui, sans-serif; color: #111827; background: #f9fafb; }
    div { text-align: center; }
    span { display: inline-block; width: 28px; height: 28px; margin-bottom: 12px; border: 3px solid #bae6fd; border-top-color: #0369a1; border-radius: 999px; animation: spin 0.8s linear infinite; }
    p { margin: 0; font-size: 14px; }
    @keyframes spin { to { transform: rotate(360deg); } }
  </style>
</head>
<body><div><span></span><p>${message}</p></div></body>
</html>`);
        target.document.close();
      } catch (_) {}
    }
  } catch (_) {
    target = null;
  }

  return {
    open(blob) {
      return openPdfBlob(blob, target);
    },
    openUrl(url) {
      const href = String(url || "").trim();
      if (!href) return false;
      try {
        if (target && !target.closed) {
          target.location.href = href;
          return true;
        }
        return !!window.open(href, "_blank", "noopener,noreferrer");
      } catch (_) {
        return false;
      }
    },
    close() {
      try {
        if (target && !target.closed) target.close();
      } catch (_) {}
    },
  };
}
