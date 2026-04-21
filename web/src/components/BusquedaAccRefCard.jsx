import { useState } from "react";
import { useNavigate } from "react-router-dom";

export default function BusquedaAccRefCard() {
  const [ref, setRef] = useState("");
  const nav = useNavigate();

  function onSubmit(e) {
    e.preventDefault();
    const needle = (ref || "").trim();
    if (!needle) return;
    nav(`/buscar-accesorio?ref=${encodeURIComponent(needle)}`);
  }

  return (
    <div className="border rounded p-4 mt-4">
      <div className="font-semibold mb-2">Búsqueda por N° de referencia de accesorio</div>
      <form onSubmit={onSubmit} className="flex gap-2">
        <input
          className="border rounded p-2 w-full max-w-md"
          placeholder="Ej.: ref A123, 001-XYZ, etc."
          value={ref}
          onChange={(e) => setRef(e.target.value)}
          aria-label="Búsqueda por referencia de accesorio"
        />
        <button className="bg-blue-600 text-white px-3 py-2 rounded">
          Buscar
        </button>
      </form>
    </div>
  );
}
