import Footer from "./Footer.jsx";

export default function AuthLayout({ title, subtitle, aside, children }) {
  return (
    <div className="min-h-screen flex flex-col bg-gray-50">
      <main className="flex-1 flex items-center justify-center px-4 py-8 sm:py-12">
        <section className="w-full max-w-4xl overflow-hidden rounded-lg border border-gray-200 bg-white shadow-sm">
          <div className="grid md:grid-cols-[0.95fr_1.05fr]">
            <aside className="border-b border-gray-200 bg-slate-50 p-6 md:border-b-0 md:border-r">
              <img
                src="/branding/logotipo-nexora.png"
                alt="NEXORA"
                className="h-14 w-auto max-w-full object-contain"
                onError={(event) => {
                  event.currentTarget.onerror = null;
                  event.currentTarget.src = "/branding/logo-nexora.png";
                }}
              />
              <div className="mt-8">
                <h1 className="text-2xl font-semibold text-gray-950">{title}</h1>
                {subtitle && <p className="mt-3 max-w-sm text-sm leading-6 text-gray-600">{subtitle}</p>}
              </div>
              {aside && <div className="mt-8">{aside}</div>}
            </aside>
            <div className="p-6 sm:p-8">{children}</div>
          </div>
        </section>
      </main>
      <Footer />
    </div>
  );
}
