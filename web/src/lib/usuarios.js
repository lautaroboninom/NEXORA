function fold(value) {
  return String(value ?? "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .trim();
}

export function bejermanStatus(user) {
  if (user?.bejerman_credentials_valid) return "valid";
  if (user?.bejerman_credentials_configured) return "review";
  return "pending";
}

export function filterUsers(
  users,
  { query = "", role = "all", active = "all", bejerman = "all" } = {},
) {
  const needle = fold(query);
  return (Array.isArray(users) ? users : []).filter((user) => {
    if (role !== "all" && user?.rol !== role) return false;
    if (active === "active" && !user?.activo) return false;
    if (active === "inactive" && user?.activo) return false;
    if (bejerman !== "all" && bejermanStatus(user) !== bejerman) return false;
    if (!needle) return true;
    const haystack = fold(
      [
        user?.id,
        user?.nombre,
        user?.email,
        user?.rol,
        user?.bejerman_credentials_username,
        user?.bejerman_workstation,
        user?.bejerman_seller_code,
      ].join(" "),
    );
    return haystack.includes(needle);
  });
}

export function describeUserAgent(rawUserAgent) {
  const ua = String(rawUserAgent || "");
  if (!ua) return "Sin datos del navegador";

  let browser = "Navegador";
  if (/Edg\//i.test(ua)) browser = "Edge";
  else if (/OPR\//i.test(ua)) browser = "Opera";
  else if (/Chrome\//i.test(ua)) browser = "Chrome";
  else if (/Firefox\//i.test(ua)) browser = "Firefox";
  else if (/Safari\//i.test(ua)) browser = "Safari";

  let system = "sistema desconocido";
  if (/Windows NT/i.test(ua)) system = "Windows";
  else if (/Android/i.test(ua)) system = "Android";
  else if (/iPhone|iPad|iPod/i.test(ua)) system = "iOS";
  else if (/Mac OS X/i.test(ua)) system = "macOS";
  else if (/Linux/i.test(ua)) system = "Linux";

  return `${browser} en ${system}`;
}

export function formatUserDate(value) {
  if (!value) return "Sin registro";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Sin registro";
  return new Intl.DateTimeFormat("es-AR", {
    dateStyle: "short",
    timeStyle: "short",
  }).format(date);
}

