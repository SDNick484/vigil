# vigil hub: sessions from every machine, reachable from anywhere

- **Date:** 2026-11-25
- **Status:** Draft for the author's review
- **Companion:** a private consumer reads the hub's API; this spec pins down the contract it relies on (§6) and
  nothing else about it.

## 1. Intent

The author develops on one or more machines (a laptop today, more later) and in cloud sessions. They want one
always-on place that lists every machine's recent Claude Code sessions with their live state (running, busy,
joinable by Remote Control link) and that other tools can read from anywhere. The always-on host is a small
server that runs programs only: it has no Claude home, no `claude` binary, and nobody develops on it. Machines may
be asleep, off the private network, or off entirely.

vigil already lists other machines' sessions through [peers](../../../README.md#peers), but peers are pulled on
demand by a machine that can reach them, and a peer's token can do everything (resume, review, apply). This
design adds a **hub mode**: a vigil that indexes nothing itself and receives sessions pushed by each machine,
plus **scoped credentials** and optional **Cloudflare Access verification** so the hub can sit behind a public
reverse proxy safely.

## 2. Goals

1. A vigil started in hub mode lists every machine's sessions through the existing `/api/v1/sessions`, with
   `machine` filled in, and keeps each machine's last snapshot when it goes quiet (marked stale).
2. A machine pushes its sessions to a hub, so the hub never needs to reach the machine and never holds a
   credential that can start `claude` anywhere.
3. Credentials carry a scope: `read`, `push` (bound to one machine name) or `full`. A reader such as a dashboard
   gets `read`.
4. Behind Cloudflare Access, vigil verifies the Access JWT on every proxied request and maps the identity (a
   user's email or a service token's client ID) to a scope. One sign-in for a browser, no second secret for a
   machine.
5. Resume from the hub hands off to the owning machine's vigil, with the Remote Control link and a copyable
   `claude --resume` command as fallbacks.

## 3. Non-goals

- The hub running `claude` in any form: no resume, restart, review, suggestion actions or file apply on the hub.
- The hub calling machines. It only builds links to them.
- Hub-to-hub federation, or a hub that is also a peer source.
- Changing how peers work. A machine may have peers and also push to a hub; the two are independent.
- Any identity provider other than Cloudflare Access.

## 4. Anti-goals

- A credential handed to a read-only consumer that can start, stop or review a session.
- A push credential for one machine that can overwrite another machine's sessions.
- A path through the reverse proxy that skips Access (for example a bearer token alone over the tunnel once
  Access is configured).
- Hub mode that quietly needs `claude`, a Claude home, or the file watcher.
- Any code or wording specific to the author's private tools or hosts in this repository.

## 5. Constraints

- Everything in `CLAUDE.md` still holds, except as amended in §11.
- `/api/v1` is read-only and fields are only ever added there. The new write endpoints live outside it, like the
  existing POST actions.
- Requires Python 3.14. The author's server runs Alpine, whose stable release ships an older Python; the hub is
  installed with a standalone 3.14 build (§10), not by lowering `requires-python`.
- New dependency: a JWT library with RSA support (PyJWT with `cryptography`, both shipped as musl wheels).

## 6. API contract

### 6.1 Machine → hub (scope `push`, bound to `{name}`)

`PUT /ingest/{name}/sessions` — the machine's complete snapshot. JSON body:

```json
{
  "format": 1,
  "machine": "laptop",
  "generated_at": "2026-11-25T18:04:05Z",
  "vigil_version": "0.9.0",
  "sessions": [ { "...": "the fields of GET /api/v1/sessions, one object per session" } ]
}
```

- `sessions`: one per resume chain (the chain's newest session), unarchived, active in the last 30 days, at most
  500, newest activity first. Each object has every field `GET /api/v1/sessions` returns for a local session
  (`id`, `short_id`, `name`, `title`, `label`, `note`, `pinned`, `archived`, `cwd`, `git_branch`, `machine`,
  `version`, `first_prompt`, `last_user`, `last_assistant`, `started_at`, `last_activity_at`, `message_count`,
  `models`, `previous_id`, `chain_id`, `project_key`, `project_label`, `transcript_path`, `remote_url`,
  `remote_url_at`, `open_url`, `busy`, `status`). Unknown extra fields are ignored, so a newer machine can push to
  an older hub.
- `machine` in the body must equal `{name}`, and `{name}` must be the machine the credential is bound to.
- The hub validates strictly: a known field with the wrong type, a time that isn't ISO 8601, or a session without
  `id` or `chain_id` rejects the whole body with 422 (the first problem named). Unknown fields are ignored. Text
  fields over 500 characters are cut, as the index does. Then it replaces that machine's stored snapshot in one
  step (write to a temporary file, `fsync`, rename). (Stricter than `vigil.peers.session_from_dict`, which
  tolerates a peer's bad fields; ingest is a contract between vigils of known versions.)
- The stored digest is computed over the `sessions` array exactly as received (canonical JSON of the parsed
  array before any cutting), so it matches the machine's own digest (§6.1 heartbeat).
- Answers: `200 {"ok": true, "received_at": "<ISO>"}`; `403` wrong machine for the credential or a `{name}` not in
  the hub's `[[machines]]`; `413` body over 8 MiB; `422` malformed body, with the first problem named.

`POST /ingest/{name}/heartbeat` — JSON `{"generated_at": "<ISO>", "digest": "<hex>"}`.

- `digest` is SHA-256 over the canonical JSON (sorted keys, no whitespace) of the `sessions` array last sent.
- `200 {"ok": true}` when the hub's stored snapshot has that digest; it updates `last_seen_at`.
- `409 {"ok": false, "need_snapshot": true}` when it doesn't (the hub restarted with no or older data, or a push
  was lost); the machine then sends a full `PUT` at once.

Ingest requests need no `Origin` header (they are not browser requests) and are refused for any scope but `push`.

### 6.2 Machine push behaviour

- A machine with `[hub]` configured runs a push loop inside `vigil serve`.
- Live state (`status`, `busy`, `open_url`) comes from `claude agents --json`, which today runs only when a page
  asks (`LiveStatus`, a lazy 5-second cache). The push loop adds its own timer that probes it every
  `hub.live_interval` seconds (60 to 3600, default 900). The author keeps several sessions running in the
  background and does not need "join now" to be minute-accurate, so a slow default was chosen over a busy one.
  One probe covers every session, so the cost does not grow with the number of sessions. Any probe made for a
  page in between is used too.
- It builds the snapshot after every index change (from the watcher, which costs no probe) and after every probe,
  and pushes when the digest differs from the last one the hub accepted, at most once every 5 seconds. With
  nothing to push it sends a heartbeat every 60 seconds.
- The snapshot body carries `live_checked_at`: when the live state in it was last probed (null if never).
- Failures back off exponentially from 5 s to 5 min and never block or slow serving; the last error and the time of
  the last accepted push show on `GET /api/v1/health` as `hub: {url, last_push_at, last_error}` (null without
  `[hub]`).
- Requests follow the peers rules: no redirects followed, proxy settings and `.netrc` ignored, 3 s connect and
  10 s total timeout, credentials sent only to `hub.url`.
- Credentials sent: Cloudflare Access service-token headers (`CF-Access-Client-Id`, `CF-Access-Client-Secret`)
  when configured, else `Authorization: Bearer` with `hub.token_file` (for a hub reached directly, without
  Access). One of the two is required.

### 6.3 Hub → readers (scope `read` or `full`)

`GET /api/v1/sessions` in hub mode lists every machine's stored sessions, with the existing query parameters
(`q`, `project`, `cwd`, `since`, `status`, `all`, `limit`) applied across machines, newest activity first.
Fields added to each session (on a non-hub vigil they are also present: `stale: false`, `received_at: null`,
`resume_url: null`, `machine_url: null`):

| Field | Meaning |
| --- | --- |
| `stale` | The machine has not pushed or sent a heartbeat for `hub.stale_after` seconds (default 180). |
| `received_at` | When the hub accepted the snapshot this session came from. |
| `resume_url` | Absolute URL of the hub's page for this session (`hub_mode.public_url` + `/machines/{name}/sessions/{id}`); null when `public_url` is unset. |
| `machine_url` | The owning machine's vigil base URL from the hub's `[[machines]]`, or null. |

Added on every vigil (local and hub), and pushed in snapshots:

| Field | Meaning |
| --- | --- |
| `remote_urls` | Every Remote Control link the session's transcript has recorded, oldest first, deduplicated (`remote_url` is the last). Lets a consumer recognize a session from any link it once had, e.g. one cited in a commit trailer. Read from the same transcript records `remote_url` comes from; stored by a new index migration. At most 50, each at most 500 characters. |

When a session is stale, the hub rewrites it on the way out: `open_url: null`, `busy: false`,
`status: "unknown"`. `remote_url` is kept (it may still work if the session is live).

`GET /api/v1/sessions/{id}` works as today over the stored sessions, without `chain` beyond the one session
(machines push one session per chain) and with `resume_command` (`claude --resume <id>`).

`GET /api/v1/machines` (new; on a non-hub vigil it returns `[]`):

| Field | Meaning |
| --- | --- |
| `name`, `url` | From `[[machines]]`. |
| `last_snapshot_at`, `last_seen_at` | Last accepted `PUT`, last accepted `PUT` or heartbeat; null if never. |
| `live_checked_at` | From the machine's latest snapshot: when its live state was last probed. |
| `stale` | As above. |
| `session_count` | Sessions in the stored snapshot. |

`GET /api/v1/health` in hub mode: `status`, `version`, `claude_version: null`, `indexed_sessions` (total stored),
`last_scan_at: null`, and `mode: "hub"` (`"local"` otherwise).

### 6.4 The hub's session page

`GET /machines/{name}/sessions/{id}` (HTML), reached from the hub's list or from a reader's `resume_url`:

- **Open**: the session's `open_url` when present (live, joinable now), labelled as joining the running session.
- **Resume on <machine>**: a link to `{machine_url}/sessions/{id}` when `machine_url` is set; the machine's own
  vigil then offers its normal Resume. Marked "<machine> last seen <time>" when stale.
- **Remote Control link**: `remote_url` when present and different from `open_url`, with the note that it works
  only if the session is still running.
- **Command**: `cd <cwd> && claude --resume <id>` with a copy button.
- The session's stored fields (name, folder, branch, times, where you left off) as on a peer session's page.

## 7. Hub mode

Selected by `mode = "hub"` in the config (default `"local"`).

- No scan, no file watcher, no `claude_home` read, no `claude` lookup. Startup must succeed with no `claude` on
  `PATH` and no Claude home.
- Resume, restart, review, meta, every suggestion action and the review reads answer 404 in hub mode; their
  buttons are not rendered.
- `peers` set together with `mode = "hub"` is a startup error. `[hub]` (the push client) with `mode = "hub"` is too.
- `[hub_mode]` table: `public_url` (optional, `https://host` with no path; used for `resume_url`),
  `stale_after` (seconds, 30 to 3600, default 180).
- `[[machines]]`: `name` (same rules as `peers[].name`), `url` (optional, `http(s)://host[:port]`, used for links
  only; the hub never requests it). A push for a name not listed is refused with 403.
- Storage: `$XDG_DATA_HOME/vigil/hub/<name>.json` (mode 0600; directory 0700), one per machine, holding the
  snapshot, its digest, `received_at` and `last_seen_at`. `last_seen_at` from heartbeats is also kept in memory
  and flushed at most once a minute. Read at startup; a corrupt file is logged and treated as no snapshot.
- Listing and filtering reuse the in-memory code that peers' sessions already go through.
- The sessions page shows the Machine column, a stale badge with "last seen", and the Open, Resume on <machine>
  and copy-command actions from §6.4.

## 8. Credentials and scopes

### 8.1 Scopes

| Scope | Allows |
| --- | --- |
| `read` | `GET` and `HEAD` on `/api/v1/*` only. |
| `push` | `PUT /ingest/{name}/sessions` and `POST /ingest/{name}/heartbeat` for its bound `name` only. |
| `full` | Everything vigil's own pages can do today (and, on a hub, browsing its pages). Not ingest. |

A request without a sufficient scope gets 403 with the needed scope named. Local requests (loopback, as defined
today) keep full access and no token, as now.

### 8.2 Bearer tokens

- `server.token_file` (existing) keeps its meaning: a `full` token. Nothing configured today changes behaviour.
- `[[server.tokens]]`: `file` (same file rules as `token_file`), `scope` (`read`, `push` or `full`), `machine`
  (required for `push`, forbidden otherwise). Two entries with the same token are a startup error.
- Any of them turns remote access on, as `token_file` does today. The browser login page accepts only `full`
  tokens.
- Peers keep using the peer's `full` token (forwarding resume needs it). A future change can let a peer use a
  `read` token for listing only; out of scope here.

### 8.3 Cloudflare Access verification

`[server.access]`, optional:

| Key | Meaning |
| --- | --- |
| `team_domain` | `<team>.cloudflareaccess.com`; certificates from `https://<team_domain>/cdn-cgi/access/certs`, issuer `https://<team_domain>`. |
| `aud` | The Access application's audience tag(s): a string or a list. |
| `emails` | User emails that get `full`. Case-insensitive. |
| `[[server.access.service_tokens]]` | `client_id`, `scope`, `machine` (as for `[[server.tokens]]`). |

- The assertion is read from `Cf-Access-Jwt-Assertion` only (not the `CF_Authorization` cookie).
- Verified: RS256 signature against the team's keys, `aud` contains a configured value, `iss` matches, `exp` in the
  future and `nbf`/`iat` not ahead of now (30 s leeway).
- Keys are fetched with the same HTTP rules as peers (no redirects, no proxies, 3 s timeouts), cached for an hour,
  and re-fetched at most once a minute when a token names an unknown `kid`. With no keys available every
  assertion fails (fail closed).
- Identity: a user token's `email` claim; a service token's `common_name` claim, which Access sets to the token's
  client ID. Unlisted identities get 403.
- Once `[server.access]` is set, a **proxied** request (it carries any proxy header vigil already recognizes)
  must carry a valid assertion; a bearer token is not accepted in its place. A **direct** remote request (no
  proxy headers, non-loopback peer) uses bearer tokens as in §8.2. Loopback requests stay local.
- A browser signed in through Access sees no vigil login page and gets no vigil cookie: the assertion comes with
  every request. A POST still needs the same-origin `Origin` header.
- Hosts: the public hostname must be in `server.allowed_hosts`, as today.

## 9. Browser hand-off from a hub to a machine

The machine's login cookie changes from `SameSite=Strict` to `SameSite=Lax`, so following the hub's
"Resume on <machine>" link opens the machine's page already signed in. Every state-changing request is a POST
that must carry the machine's own `Origin`, so Lax does not let another site act. The README's sentence about
links from other sites showing the login page is removed.

## 10. Packaging for a small Alpine server

`deploy/alpine/` (new, generic, placeholders only), modelled on a typical OpenRC service install:

- `install.sh`: creates a `vigil` system user; installs `uv`, then a standalone CPython 3.14 build for musl and a
  venv under `/opt/vigil`; installs vigil from a checkout or a pip spec; writes `/etc/init.d/vigil`; creates
  `/etc/vigil/` (root:vigil 0750) and `/var/lib/vigil/` (vigil 0700). Re-running upgrades the code and keeps config
  and data.
- `vigil.initd`: runs `vigil serve` as `vigil` with `XDG_CONFIG_HOME=/etc` and `XDG_DATA_HOME=/var/lib`, so the
  config is `/etc/vigil/config.toml` and data `/var/lib/vigil/`.
- `config.example.toml`: a hub config with `mode = "hub"`, `[hub_mode]`, `[[machines]]`, `[server]` bind and
  allowed hosts, `[server.access]` and one `[[server.tokens]]` read token, all with placeholder values.
- README: a "Hub" section covering hub mode, the push setup on a machine, Access setup (an application, its AUD,
  a policy allowing the author's email and one service token per machine) and the scopes.

## 11. Changes to CLAUDE.md

- Replace "v1 binds to `127.0.0.1` only; any other bind address is a startup error." with: "Binds to `127.0.0.1`
  unless remote access is configured (`server.token_file`, `[[server.tokens]]` or `[server.access]`)."
- Add: "Hub mode (`mode = "hub"`) must start and serve without `claude` or a Claude home, and never runs
  `claude`."
- Add: "Ingest (`/ingest/*`) accepts only `push` credentials, each bound to one machine name."

## 12. Testing

- **Access:** RSA keys generated in the test, served by a fake certificate endpoint. Cases: valid user, valid
  service token, wrong `aud`, wrong `iss`, expired, future `nbf`, unknown `kid` (one re-fetch, then 403), keys
  unreachable (403), unlisted email, unlisted client ID, bearer token alone on a proxied request with Access set
  (403).
- **Scope matrix:** every route against no credential, `read`, `push` (bound machine and another), `full`, local.
- **Ingest:** valid snapshot, wrong machine, unknown machine, oversized, malformed field, extra unknown fields
  accepted, atomic replace (a failed write leaves the old snapshot), heartbeat with matching and stale digest.
- **Hub listing:** filters across machines, stale rewrite of `open_url`/`busy`/`status`, `resume_url` and
  `machine_url`, `/api/v1/machines`, health in hub mode.
- **Hub mode:** starts with no `claude` on `PATH` and no Claude home; action routes 404; invalid config
  combinations refused.
- **Push client:** a fake hub; push on change, debounce, heartbeat, 409 → full push, back-off, no redirect followed,
  credentials sent only to the hub's origin, health fields.
- **Cookie:** `SameSite=Lax`; a cross-site POST with the cookie still refused for its `Origin`.
- The Alpine install script gets a container smoke test like the existing install tests, if one exists for it;
  otherwise a shellcheck pass and a manual check listed in the plan.

## 13. Order of work

1. Scopes and `[[server.tokens]]`; Access verification; cookie to Lax; `remote_urls`.
2. Hub mode (storage, ingest, listing, machines endpoint, session page) and the machine's push client.
3. Alpine packaging and the README "Hub" section.

Each step is its own issue and PR with a `CHANGELOG.md` line.

## 14. Approaches considered

| Approach | Trade-off | Chosen |
| --- | --- | --- |
| Pull: extend peers so the hub fetches each machine and `/api/v1/sessions` includes peers' sessions | Reuses peers and their offline cache, but the hub must reach every machine (often asleep or off the private network) and hold a token for each that today can resume and review | No |
| Push: each machine sends snapshots to the hub | New ingest endpoint and push loop, but the hub needs only to be reachable and holds no credential for any machine | **Yes** |
| Access at the edge plus vigil bearer tokens only | Smallest change to vigil, but a second login in the browser, two secrets per machine, and vigil never checks who Access let through | No |
| vigil verifies Access JWTs | A JWT dependency, but one sign-in, one secret per machine, and the tunnel can't be used to skip Access | **Yes** |
| Lower `requires-python` for hub mode | Fits the server's stock Python, but splits the codebase's support matrix | No (standalone 3.14 build instead) |

## 15. Left to the builder

- Module layout (`vigil/hub.py`, `vigil/access.py`, `vigil/push.py` or similar) and router names, appended to the
  router list.
- How the push loop hooks into the watcher's change notifications and shares `LiveStatus` with the pages.
- The exact canonical-JSON digest helper and where `last_seen_at` flushing lives.
- Page layout of the hub's session page, following the peer session page.
