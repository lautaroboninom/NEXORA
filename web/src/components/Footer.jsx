// web/src/components/Footer.jsx
import React from "react";

export default function Footer() {
  return (
    <footer className="border-t bg-white">
      <div className="max-w-7xl mx-auto px-4 py-3 text-sm text-gray-500 flex items-center justify-between">
        <span>&copy; {new Date().getFullYear()} Sepid S.A. Todos los derechos reservados.</span>
        <span>NEXORA - Gestión integral de ventas y servicio técnico</span>
      </div>
    </footer>
  );
}
