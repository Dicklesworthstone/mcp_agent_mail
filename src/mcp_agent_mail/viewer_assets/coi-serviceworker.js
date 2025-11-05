/*! coi-serviceworker v0.1.7 - Guido Zuidhof and contributors, licensed under MIT */
/*
 * Cross-Origin-Isolation Service Worker for GitHub Pages
 *
 * This service worker enables Cross-Origin-Isolation on static hosts that don't
 * support custom headers (like GitHub Pages). It intercepts all requests and adds
 * the required COOP/COEP headers to enable SharedArrayBuffer and OPFS.
 *
 * Based on: https://github.com/gzuidhof/coi-serviceworker
 * License: MIT
 */

if (typeof window === 'undefined') {
  // Service Worker context
  self.addEventListener('install', () => self.skipWaiting());
  self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

  self.addEventListener('fetch', (event) => {
    const request = event.request;

    // Only intercept same-origin requests
    if (request.cache === 'only-if-cached' && request.mode !== 'same-origin') {
      return;
    }

    event.respondWith(
      fetch(request)
        .then((response) => {
          // Don't modify opaque responses (cross-origin without CORS)
          if (response.status === 0) {
            return response;
          }

          const newHeaders = new Headers(response.headers);
          newHeaders.set('Cross-Origin-Embedder-Policy', 'require-corp');
          newHeaders.set('Cross-Origin-Opener-Policy', 'same-origin');

          return new Response(response.body, {
            status: response.status,
            statusText: response.statusText,
            headers: newHeaders,
          });
        })
        .catch((error) => {
          console.error('Service worker fetch error:', error);
          return new Response('Service worker fetch failed', { status: 503 });
        })
    );
  });
} else {
  // Main thread context - registration code
  (() => {
    const reloadedBySelf = window.sessionStorage.getItem('coi-reloaded');

    // Avoid infinite reload loops
    if (reloadedBySelf === 'true') {
      window.sessionStorage.removeItem('coi-reloaded');
      return;
    }

    const coiScriptElement = document.currentScript || document.querySelector('script[src*="coi-serviceworker"]');
    const coiScriptSrc = coiScriptElement?.src;

    if (!coiScriptSrc) {
      console.error('Could not determine coi-serviceworker.js URL');
      return;
    }

    // Check if already isolated
    if (window.crossOriginIsolated === true) {
      return;
    }

    // Register service worker
    navigator.serviceWorker
      .register(coiScriptSrc)
      .then(
        (registration) => {
          console.log('[COI] Service worker registered:', registration.scope);

          // Wait for service worker to be ready
          registration.addEventListener('updatefound', () => {
            console.log('[COI] Service worker update found');
          });

          if (registration.active && !navigator.serviceWorker.controller) {
            // Service worker is active but not controlling the page yet
            window.sessionStorage.setItem('coi-reloaded', 'true');
            window.location.reload();
          }

          // Listen for controlling service worker changes
          navigator.serviceWorker.addEventListener('controllerchange', () => {
            if (!reloadedBySelf) {
              window.sessionStorage.setItem('coi-reloaded', 'true');
              window.location.reload();
            }
          });
        },
        (error) => {
          console.error('[COI] Service worker registration failed:', error);
        }
      );
  })();
}
