import { useEffect } from "react";
import { X } from "lucide-react";
import { NavBranding, NavList } from "./Sidebar";

/**
 * Off-canvas navigation drawer for `<md` viewports. Slides in over the
 * page content (the body keeps its scroll position so nothing reflows
 * when the drawer is opened or closed).
 *
 * - The hamburger button in `TopBar` toggles `open`.
 * - Clicking the scrim or any nav link closes the drawer (`onClose`).
 * - `Escape` also closes the drawer; the body scroll lock prevents the
 *   underlying page from scrolling while the drawer is open.
 */
export function MobileDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  useEffect(() => {
    if (!open) return;

    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = previousOverflow;
    };
  }, [open, onClose]);

  return (
    <div
      aria-hidden={!open}
      className={
        "md:hidden fixed inset-0 z-50 transition-opacity duration-150 " +
        (open ? "pointer-events-auto opacity-100" : "pointer-events-none opacity-0")
      }
    >
      <button
        type="button"
        aria-label="Close navigation"
        onClick={onClose}
        className="absolute inset-0 bg-ink/45"
      />
      <aside
        role="dialog"
        aria-modal="true"
        aria-label="Primary navigation"
        className={
          "absolute inset-y-0 left-0 w-[80vw] max-w-72 bg-paper border-r border-rule " +
          "flex flex-col gap-5 px-4 py-5 overflow-y-auto " +
          "transition-transform duration-200 ease-out " +
          (open ? "translate-x-0" : "-translate-x-full")
        }
      >
        <div className="flex items-start justify-between gap-2">
          <NavBranding />
          <button
            type="button"
            onClick={onClose}
            aria-label="Close navigation"
            className="h-11 w-11 -mr-2 inline-flex items-center justify-center text-ink-muted hover:text-ink"
          >
            <X className="h-4 w-4" strokeWidth={1.5} />
          </button>
        </div>
        <NavList onNavigate={onClose} />
        <div className="text-[10px] text-ink-faint font-mono">AutoSDR · v0.4</div>
      </aside>
    </div>
  );
}
