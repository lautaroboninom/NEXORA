import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  getIngresoFotos,
  uploadIngresoFotos,
  patchIngresoFoto,
  deleteIngresoFoto,
  fetchBlobAuth,
  downloadAuth,
} from "../lib/api";
import { formatDateTime } from "../lib/ui-helpers";

const PAGE_SIZE = 20;

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "-";
  const units = ["B", "KB", "MB", "GB"];
  let idx = 0;
  let value = bytes;
  while (value >= 1024 && idx < units.length - 1) {
    value /= 1024;
    idx += 1;
  }
  return `${value.toFixed(idx === 0 ? 0 : 1)} ${units[idx]}`;
}

export default function IngresoPhotos({ ingresoId, canManage, showFilters = false }) {
  const [photos, setPhotos] = useState([]);
  const [page, setPage] = useState(1);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [uploading, setUploading] = useState(false);
  const [uploadErrors, setUploadErrors] = useState([]);
  const [statusMessage, setStatusMessage] = useState("");
  const [editingId, setEditingId] = useState(null);
  const [commentDraft, setCommentDraft] = useState("");
  const fileInputRef = useRef(null);

  const [viewer, setViewer] = useState({ open: false, index: 0, zoom: 1, rotation: 0 });
  // Blob URLs para miniaturas y archivos completos
  const thumbUrlsRef = useRef(new Map()); // id -> objectURL
  const fullUrlsRef = useRef(new Map());  // id -> objectURL
  const [thumbSrc, setThumbSrc] = useState({}); // id -> objectURL
  const [fullSrc, setFullSrc] = useState({});   // id -> objectURL
  const [typeFilter, setTypeFilter] = useState('all'); // all|image|pdf|video
  const visiblePhotos = useMemo(() => {
    const mimeOk = (p) => String(p?.mime_type || '').toLowerCase();
    if (typeFilter === 'all') return photos;
    if (typeFilter === 'image') return photos.filter((p) => mimeOk(p).startsWith('image/'));
    if (typeFilter === 'pdf') return photos.filter((p) => mimeOk(p) === 'application/pdf');
    if (typeFilter === 'video') return photos.filter((p) => mimeOk(p).startsWith('video/'));
    return photos;
  }, [photos, typeFilter]);

  const currentPhoto = useMemo(() => (viewer.open ? visiblePhotos[viewer.index] : null), [viewer, visiblePhotos]);

  const resetViewer = useCallback(() => {
    setViewer({ open: false, index: 0, zoom: 1, rotation: 0 });
  }, []);

  const loadPhotos = useCallback(async (targetPage = 1, append = false) => {
    if (!ingresoId) return;
    setLoading(true);
    setError("");
    try {
      const data = await getIngresoFotos(ingresoId, { page: targetPage, page_size: PAGE_SIZE });
      const list = Array.isArray(data?.results) ? data.results : [];
      setPhotos((prev) => (append ? [...prev, ...list] : list));
      setPage(data?.page || targetPage);
      setHasMore(Boolean(data?.has_next));
      // Pre-cargar miniaturas autenticadas como blob URLs
      if (!append) {
        thumbUrlsRef.current.forEach((url) => URL.revokeObjectURL(url));
        thumbUrlsRef.current.clear();
        setThumbSrc({});
      }
      const promises = list.map(async (p) => {
        try {
          const { blob } = await fetchBlobAuth(p.thumbnail_url);
          const url = URL.createObjectURL(blob);
          const prevUrl = thumbUrlsRef.current.get(p.id);
          if (prevUrl) URL.revokeObjectURL(prevUrl);
          thumbUrlsRef.current.set(p.id, url);
          setThumbSrc((prev) => ({ ...prev, [p.id]: url }));
        } catch (_) {
          // ignorar errores individuales
        }
      });
      Promise.allSettled(promises);
    } catch (err) {
      setError(err?.message || "No se pudieron cargar los archivos");
    } finally {
      setLoading(false);
    }
  }, [ingresoId]);

  useEffect(() => {
    if (!ingresoId) return;
    loadPhotos(1, false);
  }, [ingresoId, loadPhotos]);

  useEffect(() => {
    if (viewer.open && viewer.index >= visiblePhotos.length) {
      if (visiblePhotos.length === 0) {
        resetViewer();
      } else {
        setViewer((prev) => ({ ...prev, index: Math.max(visiblePhotos.length - 1, 0) }));
      }
    }
  }, [visiblePhotos, viewer, resetViewer]);

  // Asegurar carga de imagen completa (hoisted declaration to avoid TDZ in effects)
  function ensureFullLoaded(photo) {
    if (!photo) return;
    const id = photo.id;
    if (fullUrlsRef.current.has(id)) return;
    fetchBlobAuth(photo.url)
      .then(({ blob }) => {
        const url = URL.createObjectURL(blob);
        fullUrlsRef.current.set(id, url);
        setFullSrc((prev) => ({ ...prev, [id]: url }));
      })
      .catch(() => {
        // fallback: se ver la miniatura
      });
  }

  // Cargar imagen completa al navegar en el visor
  useEffect(() => {
    if (!viewer.open) return;
    const p = visiblePhotos[viewer.index];
    if (p) ensureFullLoaded(p);
  }, [viewer.open, viewer.index, visiblePhotos, ensureFullLoaded]);

  const handleUpload = async (fileList) => {
    const files = Array.from(fileList || []).filter((f) => f && f.size);
    if (!files.length) return;
    setUploading(true);
    setUploadErrors([]);
    setStatusMessage("");
    try {
      const data = await uploadIngresoFotos(ingresoId, files);
      const errs = Array.isArray(data?.errors) ? data.errors : [];
      if (errs.length) {
        setUploadErrors(errs.map((e) => e?.detail || e?.name || "No se pudo subir el archivo"));
      } else {
        setUploadErrors([]);
      }
      await loadPhotos(1, false);
      setStatusMessage("Archivos subidos correctamente");
    } catch (err) {
      setUploadErrors([err?.message || "No se pudo subir el archivo"]);
    } finally {
      setUploading(false);
    }
  };

  const onFileChange = (event) => {
    handleUpload(event.target.files);
    event.target.value = "";
  };

  const onDrop = (event) => {
    event.preventDefault();
    if (!canManage) return;
    handleUpload(event.dataTransfer.files);
  };

  const onDragOver = (event) => {
    if (!canManage) return;
    event.preventDefault();
  };

  const startEditing = (photo) => {
    setEditingId(photo.id);
    setCommentDraft(photo.comentario || "");
  };

  const cancelEditing = () => {
    setEditingId(null);
    setCommentDraft("");
  };

  const saveComment = async (photoId) => {
    try {
      const payload = { comentario: commentDraft };
      const updated = await patchIngresoFoto(ingresoId, photoId, payload);
      setPhotos((prev) => prev.map((p) => (p.id === photoId ? updated : p)));
      cancelEditing();
      setStatusMessage("Comentario actualizado");
    } catch (err) {
      setUploadErrors([err?.message || "No se pudo guardar el comentario"]);
    }
  };

  const confirmDelete = async (photoId) => {
    if (!window.confirm("¿Eliminar el archivo seleccionado?")) return;
    try {
      await deleteIngresoFoto(ingresoId, photoId);
      setPhotos((prev) => prev.filter((p) => p.id !== photoId));
      setStatusMessage("Archivo eliminado");
      // Revocar y limpiar URLs asociadas a la Archivo eliminado
      const t = thumbUrlsRef.current.get(photoId);
      if (t) {
        URL.revokeObjectURL(t);
        thumbUrlsRef.current.delete(photoId);
      }
      const f = fullUrlsRef.current.get(photoId);
      if (f) {
        URL.revokeObjectURL(f);
        fullUrlsRef.current.delete(photoId);
      }
      setThumbSrc((prev) => {
        const next = { ...prev };
        delete next[photoId];
        return next;
      });
      setFullSrc((prev) => {
        const next = { ...prev };
        delete next[photoId];
        return next;
      });
    } catch (err) {
      setUploadErrors([err?.message || "No se pudo eliminar el archivo"]);
    }
  };


  const openViewer = (index) => {
    setViewer({ open: true, index, zoom: 1, rotation: 0 });
    const p = visiblePhotos[index];
    if (p) ensureFullLoaded(p);
  };

  const changeViewerIndex = (delta) => {
    setViewer((prev) => {
      const next = prev.index + delta;
      if (next < 0 || next >= visiblePhotos.length) return prev;
      return { ...prev, index: next, zoom: 1, rotation: 0 };
    });
  };

  const adjustZoom = (amount) => {
    setViewer((prev) => {
      const nextZoom = Math.min(3, Math.max(0.5, Number((prev.zoom + amount).toFixed(2))));
      return { ...prev, zoom: nextZoom };
    });
  };

  const rotate = () => {
    setViewer((prev) => ({ ...prev, rotation: (prev.rotation + 90) % 360 }));
  };

  const handleDownload = async (photo) => {
    if (!photo) return;
    try {
      await downloadAuth(photo.url, photo.original_name || `ingreso-${ingresoId}-archivo-${photo.id}`);
    } catch (err) {
      setUploadErrors([err?.message || "No se pudo descargar la foto"]);
    }
  };

  // Limpiar todos los Object URLs al desmontar
  useEffect(() => {
    return () => {
      try {
        thumbUrlsRef.current.forEach((url) => URL.revokeObjectURL(url));
        fullUrlsRef.current.forEach((url) => URL.revokeObjectURL(url));
      } finally {
        thumbUrlsRef.current.clear();
        fullUrlsRef.current.clear();
      }
    };
  }, []);

  return (
    <div className="border rounded p-4 mt-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h3 className="text-lg font-semibold">Archivos del ingreso</h3>
          <p className="text-sm text-gray-600">Sub imgenes, PDF y videos cortos.</p>
        </div>
        {canManage && (
          <button
            className="bg-blue-600 text-white px-3 py-2 rounded"
            onClick={() => fileInputRef.current?.click()}
            type="button"
            disabled={uploading}
          >
            {uploading ? "Subiendo..." : "Cargar archivos"}
          </button>
        )}
      </div>

      {canManage && (
        <div
          className="mt-3 border-2 border-dashed border-blue-300 rounded bg-blue-50 p-4 text-sm text-gray-600"
          onDrop={onDrop}
          onDragOver={onDragOver}
        >
          <input
            ref={fileInputRef}
            type="file"
            accept="image/*,application/pdf,video/mp4"
            multiple
            className="hidden"
            onChange={onFileChange}
          />
          <p className="font-medium">Arrastr y solt archivos ac o us el botn "Cargar archivos".</p>
          <p className="mt-1 text-xs text-gray-500">Formatos: JPG/PNG, PDF y MP4 (mx. 10 MB c/u, hasta 50 archivos).</p>
        </div>
      )}

      {statusMessage && (
        <div className="mt-3 text-sm text-green-700 bg-green-100 border border-green-200 px-3 py-2 rounded">
          {statusMessage}
        </div>
      )}
      {uploadErrors.length > 0 && (
        <div className="mt-3 text-sm text-red-700 bg-red-100 border border-red-200 px-3 py-2 rounded">
          <ul className="list-disc list-inside">
            {uploadErrors.map((msg, idx) => (
              <li key={idx}>{msg}</li>
            ))}
          </ul>
        </div>
      )}
      {error && (
        <div className="mt-3 text-sm text-red-700 bg-red-100 border border-red-200 px-3 py-2 rounded">
          {error}
        </div>
      )}

      {showFilters && (
        <div className="mt-3 flex flex-wrap items-center gap-2 text-sm">
          <span className="text-gray-600">Filtrar:</span>
          {[
            { v: 'all', label: 'Todos' },
            { v: 'image', label: 'Imgenes' },
            { v: 'pdf', label: 'PDF' },
            { v: 'video', label: 'Videos' },
          ].map((opt) => (
            <button
              key={opt.v}
              className={`px-2 py-1 rounded border ${typeFilter === opt.v ? 'bg-blue-600 text-white border-blue-600' : ''}`}
              onClick={() => setTypeFilter(opt.v)}
              type="button"
            >
              {opt.label}
            </button>
          ))}
        </div>
      )}

      <div className="mt-4 space-y-3">
        {loading && visiblePhotos.length === 0 && (
          <div className="text-sm text-gray-500">Cargando archivos...</div>
        )}
        {!loading && visiblePhotos.length === 0 && (
          <div className="text-sm text-gray-500">An no hay archivos asociados a este ingreso.</div>
        )}
        
        {visiblePhotos
          .filter((p) => {
            const mime = String(p?.mime_type || '').toLowerCase();
            if (typeFilter === 'all') return true;
            if (typeFilter === 'image') return mime.startsWith('image/');
            if (typeFilter === 'pdf') return mime === 'application/pdf';
            if (typeFilter === 'video') return mime.startsWith('video/');
            return true;
          })
          .map((photo, idx) => (
          <div key={photo.id} className="flex flex-col md:flex-row gap-3 border rounded p-3 bg-white">
            <div className="w-full md:w-28 md:h-28 flex-shrink-0 flex items-center justify-center bg-gray-100 rounded overflow-hidden">
              {(() => {
                const mime = String(photo?.mime_type || '').toLowerCase();
                const src = thumbSrc[photo.id] || "";
                const hasThumb = !!src;
                if (hasThumb) {
                  return (
                    <img
                      src={src}
                      alt={photo.original_name || `Archivo ${photo.id}`}
                      className="object-cover w-full h-full"
                      onClick={() => {
                        if (mime.startsWith('image/')) openViewer(idx);
                      }}
                      style={{ cursor: mime.startsWith('image/') ? 'pointer' : 'default' }}
                    />
                  );
                }
                if (mime === 'application/pdf') {
                  return (
                    <div className="w-full h-full flex items-center justify-center bg-red-50 text-red-700 font-semibold">
                      PDF
                    </div>
                  );
                }
                if (mime.startsWith('video/')) {
                  return (
                    <div className="w-full h-full flex items-center justify-center bg-indigo-50 text-indigo-700 font-semibold">
                      VIDEO
                    </div>
                  );
                }
                return (
                  <div className="w-full h-full flex items-center justify-center text-gray-600">ARCHIVO</div>
                );
              })()}
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-gray-600">
                <span className="font-medium text-gray-900">{photo.original_name || `Archivo ${photo.id}`}</span>
                <span>{formatDateTime(photo.created_at)}</span>
                <span>{photo.usuario_nombre || "-"}</span>
                <span>{formatBytes(photo.size_bytes)}</span>
                {!!(photo.width && photo.height) && (
                  <span>{photo.width}x{photo.height}px</span>
                )}
              </div>
              {editingId === photo.id ? (
                <div className="mt-3 space-y-2">
                  <textarea
                    className="w-full border rounded p-2 text-sm"
                    rows={3}
                    value={commentDraft}
                    onChange={(e) => setCommentDraft(e.target.value)}
                  />
                  <div className="flex gap-2">
                    <button
                      className="bg-blue-600 text-white px-3 py-1 rounded"
                      onClick={() => saveComment(photo.id)}
                      type="button"
                    >
                      Guardar
                    </button>
                    <button
                      className="px-3 py-1 rounded border"
                      onClick={cancelEditing}
                      type="button"
                    >
                      Cancelar
                    </button>
                  </div>
                </div>
              ) : (
                <div className="mt-2 text-sm text-gray-700 whitespace-pre-wrap">
                  {photo.comentario ? photo.comentario : <span className="text-gray-400 italic">Sin comentario</span>}
                </div>
              )}
              {canManage && editingId !== photo.id && (
                <div className="flex gap-3 mt-3 text-sm">
                  <button className="text-blue-600" onClick={() => startEditing(photo)} type="button">
                    {photo.comentario ? "Editar comentario" : "Agregar comentario"}
                  </button>
                  <button className="text-red-600" onClick={() => confirmDelete(photo.id)} type="button">
                    Eliminar
                  </button>
                  <button className="text-gray-600" onClick={() => handleDownload(photo)} type="button">Descargar</button>
                  {['application/pdf'].includes(String(photo?.mime_type || '').toLowerCase()) && (
                    <button className="text-blue-600" onClick={() => handleOpenInline(photo)} type="button">Ver</button>
                  )}
                </div>
              )}
            </div>
          </div>
        ))}
      </div>

      {hasMore && (
        <div className="mt-4 text-center">
          <button
            className="px-3 py-2 border rounded"
            onClick={() => loadPhotos(page + 1, true)}
            type="button"
            disabled={loading}
          >
            {loading ? "Cargando..." : "Ver ms"}
          </button>
        </div>
      )}

      {viewer.open && currentPhoto && String(currentPhoto?.mime_type || '').toLowerCase().startsWith('image/') && (
        <div className="fixed inset-0 bg-black/80 z-40 flex flex-col items-center justify-center p-6">
          <div className="flex justify-between items-center w-full max-w-4xl text-white mb-4">
            <div>
              <div className="font-semibold">{currentPhoto.original_name || `Archivo ${currentPhoto.id}`}</div>
              <div className="text-sm text-gray-300">{formatDateTime(currentPhoto.created_at)}</div>
            </div>
            <div className="flex gap-2">
              <button className="px-3 py-1 bg-gray-700 rounded" onClick={() => adjustZoom(0.25)} type="button">Zoom +</button>
              <button className="px-3 py-1 bg-gray-700 rounded" onClick={() => adjustZoom(-0.25)} type="button">Zoom -</button>
              <button className="px-3 py-1 bg-gray-700 rounded" onClick={rotate} type="button">Rotar</button>
              <button className="px-3 py-1 bg-gray-700 rounded" onClick={() => handleDownload(currentPhoto)} type="button">
                Descargar
              </button>
              <button className="px-3 py-1 bg-red-600 rounded" onClick={resetViewer} type="button">Cerrar</button>
            </div>
          </div>
          <div className="relative max-w-4xl max-h-[70vh] overflow-auto bg-black/40 rounded">
            <img
              src={fullSrc[currentPhoto.id] || thumbSrc[currentPhoto.id] || ""}
              alt={currentPhoto.original_name || `Archivo ${currentPhoto.id}`}
              style={{ transform: `scale(${viewer.zoom}) rotate(${viewer.rotation}deg)` }}
              className="max-w-full max-h-[70vh] object-contain transition-transform duration-200"
            />
          </div>
          <div className="flex gap-4 text-white mt-4">
            <button
              className="px-3 py-1 bg-gray-700 rounded"
              onClick={() => changeViewerIndex(-1)}
              disabled={viewer.index === 0}
              type="button"
            >
              Anterior
            </button>
            <button
              className="px-3 py-1 bg-gray-700 rounded"
              onClick={() => changeViewerIndex(1)}
              disabled={viewer.index >= visiblePhotos.length - 1}
              type="button"
            >
              Siguiente
            </button>
          </div>
        </div>
      )}
    </div>
  );
}










