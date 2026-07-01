export const fullWidthButtonClass = "w-full justify-center sm:w-auto";

export function ResponsiveActionBar({ children, className = "" }) {
  return (
    <div className={`flex w-full flex-col gap-2 sm:w-auto sm:flex-row sm:flex-wrap sm:items-center ${className}`}>
      {children}
    </div>
  );
}

export function MobileDataList({ children, className = "" }) {
  return <div className={`space-y-3 md:hidden ${className}`}>{children}</div>;
}

export function MobileDataCard({ children, className = "", as: Component = "div", ...props }) {
  return (
    <Component
      className={`mobile-data-card w-full rounded-md border border-slate-200 bg-white p-3 text-left text-sm shadow-sm transition-colors ${className}`}
      {...props}
    >
      {children}
    </Component>
  );
}

export function MobileDataField({ label, value, children, className = "", valueClassName = "" }) {
  return (
    <div className={className}>
      <div className="text-[11px] font-semibold uppercase text-slate-500">{label}</div>
      <div className={`mt-0.5 break-words text-sm text-slate-900 ${valueClassName}`}>{children ?? value ?? "-"}</div>
    </div>
  );
}

export function DesktopTableWrap({ children, className = "" }) {
  return <div className={`desktop-data-table hidden overflow-x-auto rounded-md border border-slate-200 bg-white md:block ${className}`}>{children}</div>;
}

export function ResponsiveModalOverlay({ children, className = "", ...props }) {
  return (
    <div
      className={`fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/50 p-2 sm:p-4 ${className}`}
      {...props}
    >
      {children}
    </div>
  );
}

export function ResponsiveModalPanel({ children, className = "", ...props }) {
  return (
    <div
      className={`my-2 max-h-[calc(100dvh-1rem)] w-full overflow-y-auto rounded bg-white shadow-xl sm:my-4 sm:max-h-[calc(100dvh-2rem)] ${className}`}
      {...props}
    >
      {children}
    </div>
  );
}
