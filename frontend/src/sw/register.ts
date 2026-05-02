/**
 * Service worker registration shim.
 *
 * Loaded from `main.tsx`. Vite-plugin-pwa generates the
 * `virtual:pwa-register` module at build time; in development we
 * deliberately no-op so the dev server stays HMR-fast (push features
 * only activate on the production-served build, per ticket 0005's
 * *Service-worker dev/prod parity* risk note).
 *
 * The registration is fire-and-forget — if the SW fails to install
 * (browser without push support, http://-without-localhost in dev,
 * etc.), the dashboard still works as a plain SPA. The Settings →
 * Notifications card is the only place that surfaces SW state to the
 * operator.
 */

export function registerServiceWorker(): void {
  if (!('serviceWorker' in navigator)) return;
  if (import.meta.env.DEV) return;

  void import('virtual:pwa-register')
    .then(({ registerSW }) => {
      registerSW({
        immediate: true,
        onRegisteredSW(_, registration) {
          if (registration && 'pushManager' in registration) {
            window.dispatchEvent(
              new CustomEvent('autosdr:sw-ready', { detail: { registration } }),
            );
          }
        },
        onRegisterError(error) {
          console.warn('AutoSDR service worker failed to register:', error);
        },
      });
    })
    .catch((error) => {
      console.warn('AutoSDR service worker module failed to load:', error);
    });
}
