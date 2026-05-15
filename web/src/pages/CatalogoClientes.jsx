import { useEffect, useState } from "react";
import { getClientes, postCliente, deleteCliente, patchCliente, postClienteMerge } from "../lib/api";

const Input = ({ className = "", ...p }) => <input {...p} className={`border rounded p-2 w-full ${className}`} />;

export default function CatalogoClientes() {
  const [rows, setRows] = useState([]);
  const [f, setF] = useState({ razon_social: "", cod_empresa: "", telefono: "", telefono_2: "", email: "" });
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [edit, setEdit] = useState(null);
  const [ef, setEf] = useState({ razon_social: "", cod_empresa: "", telefono: "", telefono_2: "", email: "" });
  const [savingEdit, setSavingEdit] = useState(false);
  const [mergeFrom, setMergeFrom] = useState("");
  const [mergeTo, setMergeTo] = useState("");
  const [merging, setMerging] = useState(false);

  const load = async () => {
    setErr("");
    setMsg("");
    try {
      setRows(await getClientes());
    } catch (e) {
      setErr(e.message);
    }
  };
  useEffect(() => {
    load();
  }, []);

  const on = (k) => (e) => setF({ ...f, [k]: e.target.value });

  const add = async (e) => {
    e.preventDefault();
    try {
      await postCliente(f);
      setF({ razon_social: "", cod_empresa: "", telefono: "", telefono_2: "", email: "" });
      setMsg("Cliente agregado");
      load();
    } catch (e) {
      setErr(e.message);
    }
  };

  const del = async (id) => {
    if (!confirm("Eliminar cliente?")) return;
    try {
      await deleteCliente(id);
      load();
    } catch (e) {
      setErr(e.message);
    }
  };

  const openEdit = (cliente) => {
    setErr("");
    setMsg("");
    setEdit(cliente);
    setEf({
      razon_social: cliente.razon_social || "",
      cod_empresa: cliente.cod_empresa || "",
      telefono: cliente.telefono || "",
      telefono_2: cliente.telefono_2 || "",
      email: cliente.email || "",
    });
  };

  const saveEdit = async (e) => {
    e.preventDefault();
    if (!edit) return;
    try {
      setSavingEdit(true);
      setErr("");
      setMsg("");
      const payload = {
        razon_social: ef.razon_social,
        cod_empresa: ef.cod_empresa,
        telefono: (ef.telefono || "").trim() || null,
        telefono_2: (ef.telefono_2 || "").trim() || null,
        email: (ef.email || "").trim() || null,
      };
      await patchCliente(edit.id, payload);
      setMsg("Cliente actualizado");
      setEdit(null);
      setEf({ razon_social: "", cod_empresa: "", telefono: "", telefono_2: "", email: "" });
      await load();
    } catch (e) {
      setErr(e.message);
    } finally {
      setSavingEdit(false);
    }
  };

  const merge = async (e) => {
    e.preventDefault();
    if (!mergeFrom || !mergeTo) {
      setErr("Seleccione origen y destino para unificar.");
      return;
    }
    if (mergeFrom === mergeTo) {
      setErr("El origen y el destino no pueden ser el mismo cliente.");
      return;
    }
    const src = rows.find((r) => String(r.id) === String(mergeFrom));
    const dst = rows.find((r) => String(r.id) === String(mergeTo));
    const srcLabel = src ? `${src.razon_social} (#${src.id})` : mergeFrom;
    const dstLabel = dst ? `${dst.razon_social} (#${dst.id})` : mergeTo;
    if (!confirm(`Unificar ${srcLabel} dentro de ${dstLabel}? Se moverán los equipos y se eliminará el duplicado.`)) return;
    try {
      setMerging(true);
      setErr("");
      setMsg("");
      await postClienteMerge(mergeFrom, mergeTo);
      setMsg("Clientes unificados");
      setMergeFrom("");
      setMergeTo("");
      await load();
    } catch (e) {
      setErr(e.message);
    } finally {
      setMerging(false);
    }
  };

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">Clientes</h1>
      {err && <div className="bg-red-100 text-red-700 p-2 rounded">{err}</div>}
      {msg && <div className="bg-green-100 text-green-700 p-2 rounded">{msg}</div>}

      <form onSubmit={add} className="border rounded p-3 grid grid-cols-1 md:grid-cols-4 gap-3">
        <div>
          <label className="text-sm">Razón social</label>
          <Input value={f.razon_social} onChange={on("razon_social")} required />
        </div>
        <div>
          <label className="text-sm">Código empresa</label>
          <Input value={f.cod_empresa} onChange={on("cod_empresa")} required />
        </div>
        <div>
          <label className="text-sm">Teléfono</label>
          <Input value={f.telefono} onChange={on("telefono")} />
        </div>
        <div>
          <label className="text-sm">Teléfono 2</label>
          <Input value={f.telefono_2} onChange={on("telefono_2")} />
        </div>
        <div className="md:col-span-2">
          <label className="text-sm">Email</label>
          <Input type="email" value={f.email} onChange={on("email")} />
        </div>
        <div className="md:col-span-4">
          <button className="bg-blue-600 text-white px-4 py-2 rounded">Agregar</button>
        </div>
      </form>

      <form onSubmit={merge} className="border rounded p-3 grid grid-cols-1 md:grid-cols-3 gap-3">
        <div className="md:col-span-3 font-semibold">Unificar clientes duplicados</div>
        <div>
          <label className="text-sm">Mover (origen)</label>
          <select
            className="border rounded p-2 w-full"
            value={mergeFrom}
            onChange={(e) => setMergeFrom(e.target.value)}
          >
            <option value="">-- Seleccione cliente a eliminar --</option>
            {rows.map((c) => (
              <option key={c.id} value={c.id}>
                #{c.id} - {c.cod_empresa} - {c.razon_social}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="text-sm">Conservar (destino)</label>
          <select
            className="border rounded p-2 w-full"
            value={mergeTo}
            onChange={(e) => setMergeTo(e.target.value)}
          >
            <option value="">-- Seleccione cliente a conservar --</option>
            {rows.map((c) => (
              <option key={c.id} value={c.id}>
                #{c.id} - {c.cod_empresa} - {c.razon_social}
              </option>
            ))}
          </select>
        </div>
        <div className="flex items-end">
          <button
            type="submit"
            className="bg-amber-600 text-white px-4 py-2 rounded disabled:opacity-60"
            disabled={merging}
          >
            {merging ? "Unificando..." : "Unificar y borrar origen"}
          </button>
        </div>
        <div className="md:col-span-3 text-xs text-gray-600">
          Mueve todos los equipos del origen al destino y elimina el duplicado (útil para casos como OXICASTHOMECARE).
        </div>
      </form>

      <table className="w-full border">
        <thead>
          <tr className="bg-gray-50">
            <th className="p-2 text-left">ID</th>
            <th className="p-2 text-left">Razón social</th>
            <th className="p-2 text-left">Cdigo</th>
            <th className="p-2 text-left">Teléfono</th>
            <th className="p-2 text-left">Teléfono 2</th>
            <th className="p-2 text-left">Email</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {rows.map((c) => (
            <tr key={c.id} className="border-t">
              <td className="p-2">{c.id}</td>
              <td className="p-2">{c.razon_social}</td>
              <td className="p-2">{c.cod_empresa}</td>
              <td className="p-2">{c.telefono || "-"}</td>
              <td className="p-2">{c.telefono_2 || "-"}</td>
              <td className="p-2">{c.email || "-"}</td>
              <td className="p-2 text-right">
                <div className="flex gap-2 justify-end">
                  <button onClick={() => openEdit(c)} className="px-3 py-1 border rounded">Editar</button>
                  <button onClick={() => del(c.id)} className="px-3 py-1 border rounded">Eliminar</button>
                </div>
              </td>
            </tr>
          ))}
          {!rows.length && (
            <tr>
              <td colSpan="7" className="p-3 text-center text-gray-500">
                Sin clientes
              </td>
            </tr>
          )}
        </tbody>
      </table>

      {edit && (
        <div
          className="fixed inset-0 z-30 flex items-center justify-center bg-black/50 p-4"
          role="dialog"
          aria-modal="true"
          onClick={() => !savingEdit && setEdit(null)}
        >
          <div
            className="bg-white rounded shadow-xl w-full max-w-2xl p-4"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-lg font-semibold">Editar cliente</h2>
              <button
                type="button"
                className="text-sm text-gray-600 hover:text-gray-900"
                onClick={() => !savingEdit && setEdit(null)}
                aria-label="Cerrar"
              >
                Cerrar
              </button>
            </div>
            {err && <div className="bg-red-100 text-red-700 p-2 rounded mb-2">{err}</div>}
            <form onSubmit={saveEdit} className="grid grid-cols-1 md:grid-cols-2 gap-3">
              <div className="md:col-span-2">
                <label className="text-sm">Razón social</label>
                <Input value={ef.razon_social} onChange={(e) => setEf({ ...ef, razon_social: e.target.value })} required />
              </div>
              <div>
                <label className="text-sm">Código empresa</label>
                <Input value={ef.cod_empresa} readOnly className="bg-gray-50 text-gray-700" required />
              </div>
              <div>
                <label className="text-sm">Teléfono</label>
                <Input value={ef.telefono} onChange={(e) => setEf({ ...ef, telefono: e.target.value })} />
              </div>
              <div>
                <label className="text-sm">Teléfono 2</label>
                <Input value={ef.telefono_2} onChange={(e) => setEf({ ...ef, telefono_2: e.target.value })} />
              </div>
              <div className="md:col-span-2">
                <label className="text-sm">Email</label>
                <Input type="email" value={ef.email} onChange={(e) => setEf({ ...ef, email: e.target.value })} />
              </div>
              <div className="md:col-span-2 flex justify-end gap-2 mt-2">
                <button type="button" className="px-4 py-2 border rounded" onClick={() => setEdit(null)} disabled={savingEdit}>Cancelar</button>
                <button type="submit" className="px-4 py-2 rounded bg-blue-600 text-white disabled:opacity-60" disabled={savingEdit}>
                  {savingEdit ? "Guardando..." : "Guardar"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}



