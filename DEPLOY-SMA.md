# Deploying Music Assistant for tester-env

Baseline: tag `2.10.13` (`61cbb98981aa53685fc026905f5caa762462680c`).
Upstream's Dockerfile is **not** a source build (it installs the release wheel
built by GitHub Actions), so this checkout ships its own `Dockerfile.tester-env`
and a `tester-env` CLI.

## Quick Start

```bash
./tester-env deploy    # build image, start container, create admin, finish onboarding
./tester-env seed      # add the deterministic baseline (provider, listener, playlists, favorites)
./tester-env verify    # assert the seeded baseline over the API
./tester-env status    # state, URL, credentials
./tester-env logs -f   # container logs
./tester-env reset     # stop + rm container and delete the data bind dir (keeps the image)
./tester-env stop      # stop the container, data preserved
```

URL `http://127.0.0.1:8095/` (host) or `http://host.docker.internal:8095/`
(browser MCP container). Credentials `admin` / `admin12345`.

## Image

`Dockerfile.tester-env` on `ghcr.io/music-assistant/base:1.6.3` (Python 3.14.7,
ffmpeg, PyAV wheel, jemalloc, snapcast):

1. `uv venv` + `uv pip install -r requirements_all.txt` — dependency layer, only
   invalidated by a requirements change (cold ≈114 s, image 3.36 GB total).
2. `COPY . /app/source`, rewrite the pyproject version to `2.10.13` (the release
   workflow does the same; the frontend gates features on `server_version`), then
   `uv pip install --no-deps -e` and `compileall`.
3. Entrypoint = upstream jemalloc preload + `python -m music_assistant
   --data-dir /data --cache-dir /data/.cache`; the module is served from the
   source tree via `PYTHONPATH=/app/source`.

A source edit only re-runs step 2 (≈7 s); dependency layers stay cached.

## State and reset

`/data` is a host bind mount at `.tester-env-data<run-id>/` (settings.json,
auth.db, library.db, cache, logs, webrtc certs), written as the invoking
uid/gid. `./tester-env reset` removes the container and that directory; there is
no external service state.

## Determinism

- First boot has no users: `POST /api` answers 503 and `POST /setup` is public
  for 15 minutes after boot. `deploy` always recreates the container, so the
  window is fresh, then it posts `/setup`, calls `config/onboard_complete`, pins
  `config/core/save {domain: webserver, values: {base_url}}` (from
  `TESTER_ENV_BROWSER_ORIGIN`, else `http://127.0.0.1:<port>`), and re-verifies
  `POST /auth/login {provider_id: builtin}`.
- `deploy` waits for `/info` `status: running` before returning.
- Redeploys over preserved data take the login path instead of setup.

## Data

`./tester-env seed` adds the whole deterministic baseline through the same
JSON-RPC `/api` surface the UI uses, then `./tester-env verify` re-checks it:

```bash
./tester-env reset && ./tester-env deploy --run-id <id> --port 8095 && ./tester-env seed
./tester-env verify    # API-level check of every seeded count/name/relationship
```

Implementation: `tester-env-seed.py` (`seed` / `verify` subcommands). There is no
database or `settings.json` poking; the seed is idempotent and re-runs converge to
the same state.

### Seeded state (all browser-visible)

| Surface | Contents |
| --- | --- |
| Music provider | `Test / demo provider` (domain `test`, type music, status loaded) under Music sources |
| Library | 5 artists, 25 albums, 500 tracks, 5 podcasts (25 episodes each), 5 audiobooks |
| Artists | `gangsta rap Test Artist 0`, `latin Test Artist 1`, `new age Test Artist 2`, `tango Test Artist 3`, `church music Test Artist 4` |
| Albums | `<genre> Test Album 0..4` per artist (5 per artist) |
| Tracks | `<genre> Test Track <artist> - <album> - <track>` (`Test Track 0 - 0 - 0` … `4 - 4 - 19`) |
| Playlists | `Sunday Morning` (12 tracks), `Road Trip 2026` (10), `Focus Session` (8) |
| Favorites | 2 artists (`church music Test Artist 4`, `new age Test Artist 2`), 3 albums, 6 tracks, playlist `Focus Session` |
| Users | `admin` (ADMIN) and `Alex Rivera` / `listener` (USER); listener login works, `auth/users` is 403 |
| Settings | server name `Riverside Home`; online metadata retrieval off |

Credentials: `admin` / `admin12345` and `listener` / `listener12345`.

The catalogue is upstream's hermetic `test` provider fixture, so names are fake
but stable for tag 2.10.13. The playlists, favorites and listener account are
Music Assistant-native objects; playlist track order follows a fixed spread over
the name-sorted library, so assert names/counts, never ids.

### Why the seed touches settings

- `webserver.server_name = "Riverside Home"` makes Settings → System show a
  realistic household name instead of `Music Assistant (<hostname>)`.
- `metadata.enable_online_metadata = false`: the sandbox has no dependable
  MusicBrainz/audioDB/Last.fm access, and every item read schedules a metadata
  task that the controller throttles to one item per 30 s on a two-slot task
  queue. Leaving it on starves real user actions (adding playlist tracks,
  syncing) for minutes.
- Favoriting still schedules those refreshes, so the seed cancels the
  favorite-triggered `metadata_lookup` tasks and clears finished task rows. The
  instance starts with a quiet task queue.

## Browser assertions for the populated baseline

- Login as `admin` / `admin12345`: the landing page (Discover) shows **Players**
  (the browser registers as `This device`) plus **Top Picks for You**, not the
  onboarding wizard or an empty shell.
- **Artists** shows 5 cards; `church music Test Artist 4` and
  `new age Test Artist 2` carry a filled heart, the other three do not.
- **Albums** shows 25 tiles; **Tracks** lists 500 rows (first row
  `church music Test Track 4 - 0 - 0 (2021)`).
- **Playlists** lists `Sunday Morning`, `Road Trip 2026` and a favorited
  `Focus Session` next to the app's own `500 Random tracks (from library)`
  (that name echoes the library size) and `All favorited tracks`.
- Opening `Focus Session` shows 8 tracks starting with
  `church music Test Track 4 - 0 - 4`, `church music Test Track 4 - 3 - 6`,
  `gangsta rap Test Track 0 - 1 - 8`.
- **Podcasts** shows 5 shows, **Audiobooks** 5 books.
- **Settings → User management** shows `admin` (ADMIN) and `Alex Rivera`
  (USER). **Settings → System** shows server name `Riverside Home`; the
  **Metadata** module shows online metadata retrieval unchecked.
- Reset + deploy + seed reproduces this state exactly (verified twice with
  byte-identical snapshots).

## Caveats

- The UI bundle is the pinned `music-assistant-frontend 2.17.297.post4` wheel
  from `requirements_all.txt`; server-side mutants are live, UI-bundle mutants
  need the separate frontend repo.
- Builtin metadata/analysis providers and mDNS/SSDP device discovery stay active
  and log sandbox warnings; never assert on task progress or discovery status.
- The web player ("This device") is registered per browser session, not seeded;
  demo players (`_demo_player_provider` with `PYTHONDEVMODE=1`) can be added
  later if a scenario needs stable server-side players.
- `cliairplay` is missing (upstream copies it from a release asset), so AirPlay
  logs a PTP-timing warning; the provider still loads.
- `IMAGE_TAG` (set by `scripts/rl-env`) names the image; `RUN_ID` scopes only the
  container and data dir.

