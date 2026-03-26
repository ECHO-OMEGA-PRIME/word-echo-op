import { getAssetFromKV } from '@cloudflare/kv-asset-handler';
import manifestJSON from '__STATIC_CONTENT_MANIFEST';
const assetManifest = JSON.parse(manifestJSON);

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // Security headers
    const securityHeaders = {
      'X-Content-Type-Options': 'nosniff',
      'X-Frame-Options': 'SAMEORIGIN',
      'Referrer-Policy': 'strict-origin-when-cross-origin',
    };

    try {
      const response = await getAssetFromKV(
        { request, waitUntil: ctx.waitUntil.bind(ctx) },
        {
          ASSET_NAMESPACE: env.__STATIC_CONTENT,
          ASSET_MANIFEST: assetManifest,
        }
      );

      const headers = new Headers(response.headers);
      Object.entries(securityHeaders).forEach(([k, v]) => headers.set(k, v));
      headers.set('Cache-Control', 'public, max-age=3600');

      return new Response(response.body, {
        status: response.status,
        headers,
      });
    } catch (e) {
      // Serve index.html for any path
      try {
        const notFoundRequest = new Request(new URL('/index.html', request.url).toString(), request);
        const response = await getAssetFromKV(
          { request: notFoundRequest, waitUntil: ctx.waitUntil.bind(ctx) },
          {
            ASSET_NAMESPACE: env.__STATIC_CONTENT,
            ASSET_MANIFEST: assetManifest,
          }
        );
        const headers = new Headers(response.headers);
        Object.entries(securityHeaders).forEach(([k, v]) => headers.set(k, v));
        return new Response(response.body, { status: 200, headers });
      } catch {
        return new Response('Not Found', { status: 404 });
      }
    }
  },
};
