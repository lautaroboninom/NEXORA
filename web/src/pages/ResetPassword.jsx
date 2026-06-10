// ResetPassword.jsx
import { useEffect, useState } from "react";
import { useSearchParams, useNavigate } from "react-router-dom";
import { postAuthReset } from "../lib/api";
import Footer from "../components/Footer.jsx";

export default function ResetPassword(){
  const [sp] = useSearchParams();
  const token = sp.get("t") || sp.get("token") || "";
  const nav = useNavigate();
  const [p1, setP1] = useState("");
  const [p2, setP2] = useState("");
  const [err, setErr] = useState("");
  const [done, setDone] = useState(false);

  useEffect(()=>{ if(!token) setErr("Link inválido"); }, [token]);

  async function submit(e){
    e.preventDefault();
    setErr("");
    if(p1.length < 8) return setErr("La contraseña debe tener al menos 8 caracteres.");
    if(p1 !== p2)   return setErr("Las contraseñas no coinciden.");
    try{
      await postAuthReset(token, p1);
      setDone(true);
      setTimeout(()=> nav("/login"), 1200);
    }catch(e){ setErr(e?.message || "No se pudo restablecer"); }
  }

  return (
    <div className="min-h-screen flex flex-col">
      <main className="flex-1">
        <div className="max-w-sm mx-auto mt-16 border rounded p-4 bg-white">
          <h1 className="text-xl font-semibold mb-3">Restablecer contraseña</h1>
          {done ? (
            <div className="text-sm text-green-700">Listo! Ya puedes iniciar sesión.</div>
          ) : (
            <form onSubmit={submit} className="space-y-3">
              {err && <div className="bg-red-100 text-red-700 p-2 rounded">{err}</div>}
              <input type="password" className="border rounded p-2 w-full" placeholder="Nueva contraseña"
                     value={p1} onChange={e=>setP1(e.target.value)} />
              <input type="password" className="border rounded p-2 w-full" placeholder="Repetir contraseña"
                     value={p2} onChange={e=>setP2(e.target.value)} />
              <button className="bg-blue-600 text-white w-full p-2 rounded">Guardar</button>
            </form>
          )}
        </div>
      </main>
      <Footer />
    </div>
  );
}


