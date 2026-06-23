export default function Tabs({ value, onChange, items, extraRight }) {
  return (
    <div className="mb-4 flex items-center gap-2 overflow-x-auto border-b">
      <div className="flex min-w-max gap-2">
        {items.map((it) => (
          <button
            key={it.value}
            className={`whitespace-nowrap px-3 py-2 rounded-t ${
              value === it.value
                ? "bg-white border border-b-0"
                : "text-gray-600 hover:text-black"
            }`}
            onClick={() => onChange(it.value)}
            type="button"
          >
            {it.label}
          </button>
        ))}
      </div>
      <div className="ml-auto shrink-0">{extraRight}</div>
    </div>
  );
}
