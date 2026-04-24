import { Link } from 'react-router-dom';

export function NotFound() {
  return (
    <div className="flex flex-col items-center justify-center min-h-[60vh] gap-5 px-8">
      <div className="font-mono text-[11px] tracking-[0.2em] uppercase text-ink-faint">404</div>
      <h1 className="text-3xl font-medium text-ink text-center">Page not found</h1>
      <p className="text-sm text-ink-muted text-center max-w-md">
        That page doesn&rsquo;t exist, or the thread moved somewhere else.
      </p>
      <Link
        to="/"
        className="inline-flex items-center gap-2 px-4 h-10 border border-ink text-ink hover:bg-ink hover:text-paper transition-colors text-sm"
      >
        Back to dashboard
      </Link>
    </div>
  );
}
