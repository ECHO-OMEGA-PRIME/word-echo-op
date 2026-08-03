# Word Echo

Word Echo is the private-cluster replacement for the rescued Cloudflare Worker
`word-echo-op`. It serves the recovered static site byte-for-byte from FORGE,
keeps the public `/health` contract, preserves safe SPA fallback behavior, and
runs without a database, write credential, timer, or external runtime call.

## Migration contract

The Worker had one fetch handler, two generic behaviors (`/` static serving and
`/health`), and one Workers Sites static-content namespace containing exactly
one 401,921-byte HTML value. The declared Analytics Engine binding was unused;
there is no analytics state or write path to migrate. `migration_contract.json`
records the catalog rescue, strict recovered bundle, authored repository source,
and static value as independent identities. Only the static payload is asserted
byte-equivalent.

The recovered page uses inline scripts, inline styles, and inline event handlers.
The CSP therefore retains `unsafe-inline` for compatibility while denying all
network connections (`connect-src 'none'`). Static analysis found no URL,
request, storage, form, or network-derived source feeding its `innerHTML` sinks.

Intentional hardening deltas:

- mutations now return 405;
- OPTIONS is limited to `/health` preflight;
- traversal, encoded traversal, dotfiles, NULs, and overlong paths fail closed;
- all response classes receive CSP, HSTS, framing, referrer, permissions, and
  content-type protections;
- reads and preflights have bounded rate limits;
- logs contain method, normalized path, status, duration, and request ID only.

## Local verification

```powershell
python -m pytest -q
python -m uvicorn app:app --host 127.0.0.1 --port 8464
python smoke_live.py --base http://127.0.0.1:8464
```

The service refuses to start if `public/index.html` does not match the recovered
static payload SHA-256. `.gitattributes` stores that large HTML file as exact
bytes so Linux and Windows clones deploy the same payload.

## Production deployment

`deploy_word_echo_op.sh` creates an immutable release beneath
`/home/forge/word-echo-op/releases`, verifies every independent provenance hash,
runs unit tests, boots the exact candidate on staging port 8465, live-smokes it,
and only then flips the relative `current` symlink and restarts production on
loopback port 8464. A red production smoke automatically restores and smokes the
prior release. The deployment writes metadata-only PostgreSQL receipts as the
root control plane; the runtime has no PostgreSQL role or credentials.

Required acceptance sequence:

1. clean deploy;
2. forced staging failure, proving production was untouched;
3. forced production failure, proving rollback to the prior green release;
4. `finalize_word_echo_op.sh`, which fresh-smokes the active release and only
   then updates the artifact catalog and migration tracker;
5. run the canonical Cloudflare migration audit;
6. use `register_tunnel_route.py apply`, public-edge smoke, and `status` to route
   the hostname. `rollback` restores the backed-up tunnel and DNS configuration.

The systemd service runs as the dedicated non-login `word-echo-op` user, binds
only to `127.0.0.1`, sees its immutable release through a read-only bind mount,
and has no supplementary groups or writable application directory.
