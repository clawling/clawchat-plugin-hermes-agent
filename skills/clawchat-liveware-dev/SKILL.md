---
name: clawchat-liveware-dev
version: 1.0.0
description: Use when building or modifying a web app intended to run behind ClawChat Liveware. Covers distinctive frontend design with native HTML, CSS, and JavaScript, a lightweight Python backend, content-hashed static assets, and correct HTTP cache headers. Use clawchat-liveware separately for app creation, binding, access policy, inspection, and removal.
---

# ClawChat Liveware Development

Build a small, self-contained web service that runs in a constrained agent environment and
is ready to expose through Liveware. Favor source code the agent can run directly over a
toolchain it must install or compile.

## Hard constraints

- Build the frontend with semantic HTML, CSS, and browser-native JavaScript. For a new app,
  create no `package.json`, `node_modules`, JSX, TSX, TypeScript compiler, bundler, or npm
  build step. Use Web APIs and ES modules only when plain JavaScript needs modularity.
- Prefer a Python 3 backend. Start with the standard library; add a small Python framework
  only when the requested behavior clearly needs it and the dependency is already available
  or can be installed without introducing a frontend toolchain.
- Listen on `127.0.0.1`, not a public interface. Liveware owns public exposure.
- Give every externally served JavaScript, CSS, image, font, and other static asset a
  content hash in its filename. A query string alone is not a content fingerprint.
- Fingerprinted assets are long-lived and immutable. HTML and API responses are always
  revalidated or non-cacheable so clients discover new asset filenames and fresh data.
- Keep creation and publishing separate. Finish and verify the local service here, then use
  the `clawchat-liveware` skill for Liveware app creation, binding, permissions, and removal.

## Workflow

1. **Ground the brief.** Identify one concrete subject, its audience, and the page's single
   primary job. If the user left one of these open, choose it from available context and state
   the assumption. Inventory the required pages, API operations, persisted data, and viewer
   identity needs. This step is complete when every screen and endpoint serves that primary
   job.

2. **Choose a visual direction before coding.** Produce a compact internal design plan:

   - four to six named color tokens with exact values;
   - deliberate display, body, and utility type roles using system fonts or bundled,
     fingerprinted font files;
   - a responsive layout concept, sketched as a small ASCII wireframe when layout is not
     obvious;
   - one memorable signature element grounded in the app's subject.

   Spend visual boldness on that signature element and keep the rest disciplined. Revise any
   choice that could be pasted unchanged into an unrelated product. Structural labels,
   numbering, motion, and decoration must communicate something real. Write controls from the
   user's perspective with stable, active labels: an action called “Save” produces a “Saved”
   result. Empty and error states say what happened and what the user can do next.

3. **Design the no-build file layout.** Prefer this shape unless the existing app has an
   equally simple convention:

   ```text
   app/
     server.py
     tools/fingerprint_assets.py
     web/index.template.html
     web/assets-src/app.css
     web/assets-src/app.js
     web/public/index.html             # generated entry document
     web/public/assets/                 # generated fingerprinted files
     web/public/asset-manifest.json     # generated source-to-output mapping
   ```

   Keep authored and generated files separate. Prefer one CSS file and one JavaScript file per
   page so hashing stays transparent. If multiple native modules are necessary, fingerprint
   leaf modules first, rewrite their import specifiers, and then fingerprint importers.

4. **Implement the Python service.** Use explicit routes for HTML, assets, health, and APIs.
   Resolve filesystem paths against the static root and reject traversal. Set correct content
   types, UTF-8 encode text, constrain request body sizes, validate JSON shapes, escape
   untrusted values rendered into HTML, and return structured JSON errors. State-changing
   operations accept only their intended HTTP methods.

   For viewer-aware behavior, read `X-User-Id` or `X-Clawchat-User-Id` on the server. Treat the
   value as identity only for traffic arriving through the Liveware tunnel; never copy it from
   browser input or expose privileged operations on a client-asserted id.

5. **Implement the native frontend.** Use DOM APIs, `fetch`, events, forms, CSS custom
   properties, and progressive enhancement. Keep state and rendering small and explicit.
   Handle loading, empty, success, and failure states. Use semantic controls, visible keyboard
   focus, sufficient contrast, usable touch targets, mobile layouts, and
   `prefers-reduced-motion`. Avoid remote CDN scripts; vendor a truly necessary dependency as
   a fingerprinted static file and record why it is needed.

6. **Fingerprint every static asset.** The Python preparation script must:

   - hash the final served bytes with SHA-256 and place at least 12 hexadecimal characters in
     the filename, for example `app.8c7f2a91d4e6.js`;
   - preserve the extension so MIME detection remains correct;
   - write a deterministic `asset-manifest.json` mapping logical names to hashed paths;
   - rewrite CSS `url(...)`, HTML references, and JavaScript module imports to the hashed
     dependency names before hashing their parent files;
   - generate `index.html` from exact placeholders in `index.template.html`;
   - leave an existing hashed filename immutable: the same path must always serve the same
     bytes;
   - run before the server accepts requests, using Python only. It is asset preparation, not a
     frontend compilation step.

   Keep the public entry URL stable. Do not hash `index.html`; it must fetch or revalidate on
   every visit so it can point at the latest asset hashes. Retain previous hashed assets long
   enough for in-flight or previously loaded HTML to finish using them.

7. **Apply cache policy by response class.** Set headers on successes and errors, including
   `HEAD` and `OPTIONS` responses where supported:

   | Response | Required `Cache-Control` |
   | --- | --- |
   | Fingerprinted assets | `public, max-age=31536000, immutable` |
   | `index.html` and other HTML entry documents | `no-cache, max-age=0, must-revalidate` |
   | `asset-manifest.json` | `no-cache, max-age=0, must-revalidate` |
   | Every `/api/` response | `no-store, no-cache, max-age=0, must-revalidate` |
   | Health and dynamic status responses | `no-store` |

   API responses also send `Pragma: no-cache` and `Expires: 0` for older intermediaries. A
   framework's default behavior does not count as verification; add middleware or a shared
   response helper so every API path, including validation and server errors, gets the policy.

8. **Verify behavior and presentation.** At minimum:

   - run `python3 -m compileall` and the backend's focused tests;
   - start the service on loopback and require the health endpoint to succeed;
   - request HTML, every referenced asset, representative API successes, and API errors;
   - assert all served asset URLs contain the hash of their exact bytes;
   - change one source asset, prepare again, and assert its public filename changes while the
     generated HTML references the new name;
   - inspect headers and require immutable caching only on fingerprinted assets and no-store
     behavior on every API response;
   - test narrow mobile and desktop layouts, keyboard navigation, reduced motion, and empty and
     error states; use screenshots for visual critique when browser tooling is available;
   - confirm the project starts with Python alone and no npm or frontend compilation command.

The app is ready only when the cache-bust test, header checks, local service smoke test, and
visual/accessibility review all pass. Report the local URL, start command, persisted-data path,
and verification results before handing publishing to `clawchat-liveware`.

## Implementation cautions

- Calculate hashes from final output bytes, after dependency URLs are rewritten. Hashing source
  bytes and then changing them produces a lying filename.
- Avoid putting user-specific or secret data in static files: immutable public caches may retain
  those bytes for a year.
- Do not let an API path fall through to the static-file handler; a mistaken immutable header on
  dynamic JSON can leak stale or user-specific data.
- Keep generated assets deterministic. Timestamps, random ids, absolute paths, and host-specific
  line endings create needless new hashes.
- A service restart may regenerate identical filenames for identical bytes. That is correct and
  preserves cache efficiency.
