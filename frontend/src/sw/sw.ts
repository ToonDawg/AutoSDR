/// <reference lib="webworker" />

/**
 * AutoSDR service worker — ticket 0005 unit 6 + 7.
 *
 * Built with `vite-plugin-pwa`'s `injectManifest` strategy: the plugin
 * injects the precache manifest into the placeholder
 * `self.__WB_MANIFEST` at build time; we own everything else. Keeping
 * the file < 100 LoC was a Skeptic-mandated bound when we accepted
 * Workbox lock-in — see ticket 0005 § *vite-plugin-pwa vs hand-rolled SW*.
 *
 * Responsibilities:
 *
 *   1. Precache the built shell so the app loads offline.
 *   2. Use a NetworkFirst cache for `/api/` so the operator gets fresh
 *      data when online and a *recent-ish* fallback when offline.
 *   3. Listen for `push` events; show a privacy-strict notification.
 *      Payload shape comes from
 *      `autosdr.push.build_hitl_payload` and is enforced by
 *      `tests/test_push_payload_privacy.py`.
 *   4. On `notificationclick` focus an existing dashboard window
 *      pointed at the right thread, or open one. The deep-link URL
 *      comes from the payload's `url` (set by the server using the
 *      configured `dashboard_origin`), so we never have to guess
 *      the tailnet hostname client-side.
 */

import { precacheAndRoute, cleanupOutdatedCaches } from 'workbox-precaching';
import { registerRoute, NavigationRoute } from 'workbox-routing';
import { NetworkFirst } from 'workbox-strategies';

declare const self: ServiceWorkerGlobalScope;

cleanupOutdatedCaches();
precacheAndRoute(self.__WB_MANIFEST);

registerRoute(
  ({ url }) => url.pathname.startsWith('/api/'),
  new NetworkFirst({ cacheName: 'autosdr-api', networkTimeoutSeconds: 4 }),
);

registerRoute(
  new NavigationRoute(async ({ event }) => {
    try {
      return await fetch((event as FetchEvent).request);
    } catch {
      const cache = await caches.open('autosdr-api');
      const fallback = await cache.match('/');
      return fallback ?? new Response('offline', { status: 503 });
    }
  }),
);

interface HitlPushPayload {
  title?: string;
  body?: string;
  thread_id?: string;
  lead_first_name?: string;
  hitl_reason?: string;
  escalated_at?: string;
  url?: string;
}

self.addEventListener('push', (event: PushEvent) => {
  const payload: HitlPushPayload = (() => {
    if (!event.data) return {};
    try {
      return event.data.json() as HitlPushPayload;
    } catch {
      return { title: 'AutoSDR', body: event.data.text() };
    }
  })();
  const title = payload.title ?? 'AutoSDR: thread needs your eye';
  const body = payload.body ?? 'Tap to triage.';
  const tag = payload.thread_id ? `hitl-${payload.thread_id}` : 'hitl';
  const url = payload.url ?? '/inbox';
  event.waitUntil(
    self.registration.showNotification(title, {
      body,
      tag,
      icon: '/pwa-192x192.png',
      badge: '/pwa-64x64.png',
      data: { url, thread_id: payload.thread_id ?? null },
      requireInteraction: false,
    }),
  );
});

self.addEventListener('notificationclick', (event: NotificationEvent) => {
  event.notification.close();
  const data = (event.notification.data ?? {}) as { url?: string };
  const target = data.url ?? '/inbox';
  event.waitUntil(
    (async () => {
      const allClients = await self.clients.matchAll({
        type: 'window',
        includeUncontrolled: true,
      });
      for (const client of allClients) {
        if ('focus' in client && client.url.includes(new URL(target, client.url).pathname)) {
          await (client as WindowClient).focus();
          return;
        }
      }
      if (allClients.length > 0 && 'focus' in allClients[0]) {
        const client = allClients[0] as WindowClient;
        await client.focus();
        await client.navigate(target);
        return;
      }
      await self.clients.openWindow(target);
    })(),
  );
});

self.addEventListener('message', (event: ExtendableMessageEvent) => {
  if (event.data === 'SKIP_WAITING') self.skipWaiting();
});

export {};
