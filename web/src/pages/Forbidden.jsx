// web/src/pages/Forbidden.jsx
import { Link } from "react-router-dom";

export default function Forbidden() {
  return (
    <div className="min-h-[60vh] flex items-center justify-center p-6">
      <div className="max-w-lg text-center">
        <div className="text-3xl font-bold mb-2">Acceso denegado</div>
        <div className="text-gray-600 mb-6">
          No tiene permisos para acceder a esta sección.
        </div>
        <div className="space-x-3">
          <Link className="btn" to="/">Ir al inicio</Link>
          <Link className="btn btn-secondary" to="/login">Iniciar sesión</Link>
        </div>
      </div>
    </div>
  );
}
