// web/src/components/BusquedaNSCard.jsx

import { useState } from "react";
import { useNavigate } from "react-router-dom";

export default function BusquedaNSCard() {
  const [ns, setNs] = useState("");
  const [os, setOs] = useState("");
  const nav = useNavigate();

  function onSubmit(e) {
    e.preventDefault();
    const needle = (ns || "").trim();
    if (!needle) return;
    nav(`/buscar-ns?serie=${encodeURIComponent(needle)}`);
  }

  return (
    <div className="border rounded p-4 mt-4">
      <div className="font-semibold mb-2">Búsqueda por N/S o MG</div>
      <form onSubmit={onSubmit} className="flex gap-2">
        <input
          className="border rounded p-2 w-full max-w-md"
          placeholder="Ingresá el N° de serie o MG"
          value={ns}
          onChange={(e) => setNs(e.target.value)}
          aria-label="Búsqueda por número de serie o MG"
        />
        <button className="bg-blue-600 text-white px-3 py-2 rounded">
          Buscar
        </button>
      </form>
      <div className="mt-3">
        <form
          className="flex gap-2 items-center"
          onSubmit={(e) => {
            e.preventDefault();
            const digits = String(os || "").replace(/\D/g, "");
            if (!digits) return;
            nav(`/ingresos/${parseInt(digits, 10)}`);
          }}
        >
          <input
            className="border rounded p-2 w-full max-w-md"
            placeholder="Ingresá el N° de Orden (OS)"
            value={os}
            onChange={(e) => setOs(e.target.value)}
            aria-label="Búsqueda por número de orden"
          />
          <button className="bg-blue-600 text-white px-3 py-2 rounded">
            Ir
          </button>
        </form>
      </div>
    </div>
  );
}
