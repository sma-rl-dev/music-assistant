#!/usr/bin/env python3
"""Deterministic seed and verification for the Music Assistant tester-env baseline.

Theme: a small shared-household music server. One music library is imported from
the upstream `test` provider ("Test / demo provider"), a second non-admin account
listens along, three hand-made playlists collect tracks, and a few artists, albums,
tracks and playlists are favorited.

Every write goes through the JSON-RPC `/api` surface (the same commands the UI
uses): `config/providers/setup`, `auth/user/create`, `music/playlists/*` and
`music/favorites/add_item`. There is no database access and no browser work, so
`./tester-env seed` converges to the same state on a fresh deploy and is safe to
re-run on an already seeded instance.

The catalogue itself is produced by the pinned `test` provider (upstream's own
hermetic fixture): 5 artists, 25 albums, 500 tracks, 5 podcasts and 5 audiobooks,
with names derived from `random.Random(item_id)` and therefore stable for the
pinned version 2.10.13. Playlists, favorites and the listener account reference
library items, so they are resolved by name/order at seed time instead of ids.

Usage:
    tester-env-seed.py seed   <base-url>   # create/normalise the seeded baseline
    tester-env-seed.py verify <base-url>   # assert the seeded baseline over the API
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request

ADMIN_USER = "admin"
ADMIN_PASSWORD = "admin12345"

PROVIDER_DOMAIN = "test"
EXPECTED_COUNTS = {
    "artists": 5,
    "albums": 25,
    "tracks": 500,
    "podcasts": 5,
    "audiobooks": 5,
}

# (username, password, role, display name)
USERS = [("listener", "listener12345", "user", "Alex Rivera")]

# (playlist name, track count)
PLAYLISTS = [("Sunday Morning", 12), ("Road Trip 2026", 10), ("Focus Session", 8)]

# Indexes into the name-sorted library lists.
FAVORITE_ARTISTS = [0, 3]
FAVORITE_ALBUMS = [0, 5, 11]
FAVORITE_TRACKS = [0, 61, 122, 183, 244, 305]
FAVORITE_PLAYLISTS = ["Focus Session"]

# Visible server name (Settings -> System) for the household theme.
SERVER_NAME = "Riverside Home"

WAIT_SECONDS = 120


class SeedError(RuntimeError):
    """A seed or verify step could not be completed."""


class Client:
    """Minimal JSON-RPC client for the Music Assistant API."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = ""
        self._message_id = 0

    def _post(self, path: str, payload: dict) -> tuple[int, object]:
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = resp.read().decode()
                return resp.status, json.loads(body) if body else None
        except urllib.error.HTTPError as err:
            return err.code, err.read().decode(errors="replace")

    def command(self, command: str, args: dict | None = None) -> object:
        self._message_id += 1
        status, body = self._post(
            "/api",
            {"message_id": f"seed-{self._message_id}", "command": command, "args": args or {}},
        )
        if status != 200:
            raise SeedError(f"{command} failed with HTTP {status}: {body}")
        if isinstance(body, dict) and body.get("error_code"):
            raise SeedError(f"{command} returned an error: {json.dumps(body)[:300]}")
        return body

    def login(self, username: str, password: str) -> None:
        deadline = time.monotonic() + WAIT_SECONDS
        while True:
            status, body = self._post(
                "/auth/login",
                {
                    "provider_id": "builtin",
                    "credentials": {"username": username, "password": password},
                },
            )
            if status == 200 and isinstance(body, dict) and body.get("token"):
                self.token = body["token"]
                return
            if time.monotonic() > deadline:
                raise SeedError(f"login as {username} failed (HTTP {status}): {body}")
            time.sleep(2)


def wait_for(check, what: str, timeout: float = WAIT_SECONDS):
    """Poll `check` until it returns a truthy value."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = check()
        if last:
            return last
        time.sleep(2)
    raise SeedError(f"timed out waiting for {what} (last value: {last!r})")


def library_counts(client: Client) -> dict[str, int]:
    return {
        media_type: client.command(f"music/{media_type}/count")
        for media_type in EXPECTED_COUNTS
    }


def library_items(client: Client, media_type: str, **args) -> list[dict]:
    return client.command(f"music/{media_type}/library_items", {"limit": 1000, **args})


def by_name(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda item: (item["name"], str(item["item_id"])))


def playlist_track_indexes(position: int, count: int, library_size: int) -> list[int]:
    """Deterministic spread of library indexes for one playlist."""
    step = library_size // count
    indexes = [position * 7 + i * step for i in range(count)]
    if indexes[-1] >= library_size:
        raise SeedError(f"playlist selection {indexes[-1]} exceeds library size {library_size}")
    return indexes


def expected_playlists(tracks: list[dict]) -> dict[str, list[str]]:
    """Map each seeded playlist name to its expected ordered track names."""
    result = {}
    for position, (name, count) in enumerate(PLAYLISTS):
        indexes = playlist_track_indexes(position, count, len(tracks))
        result[name] = [tracks[index]["name"] for index in indexes]
    return result


def ensure_core_settings(client: Client) -> None:
    """Name the server and keep metadata lookups local.

    Online metadata lookups (MusicBrainz, fanart.tv, The Audio DB, ...) are
    queued as background tasks and can each hold one of only two task slots
    while they retry against the internet, which starves user actions such as
    adding tracks to a playlist. The sandbox has no dependable metadata provider,
    so the baseline turns them off; only the local/library metadata path remains.
    """
    client.command("config/core/save", {"domain": "webserver", "values": {"server_name": SERVER_NAME}})
    client.command(
        "config/core/save",
        {"domain": "metadata", "values": {"enable_online_metadata": False}},
    )
    print(f"    server name '{SERVER_NAME}', online metadata disabled")


def ensure_provider(client: Client) -> None:
    configs = client.command("config/providers")
    if not any(config.get("domain") == PROVIDER_DOMAIN for config in configs):
        print(f"    adding music provider '{PROVIDER_DOMAIN}' through the setup flow")
        step = client.command("config/providers/setup", {"provider_domain": PROVIDER_DOMAIN})
        if not isinstance(step, dict) or step.get("type") != "finish":
            raise SeedError(f"provider setup did not finish: {json.dumps(step)[:300]}")
    loaded = wait_for(
        lambda: [
            config
            for config in client.command("config/providers")
            if config.get("domain") == PROVIDER_DOMAIN and config.get("status") == "loaded"
        ],
        f"provider '{PROVIDER_DOMAIN}' to load",
    )
    print(f"    provider '{PROVIDER_DOMAIN}' loaded ({loaded[0].get('default_name')})")

    wait_for(
        lambda: library_counts(client) == EXPECTED_COUNTS,
        f"library counts {EXPECTED_COUNTS}",
        timeout=300,
    )
    print(f"    library synced: {EXPECTED_COUNTS}")


def ensure_users(client: Client) -> None:
    existing = {user["username"] for user in client.command("auth/users")}
    for username, password, role, display_name in USERS:
        if username in existing:
            print(f"    user '{username}' already exists")
            continue
        client.command(
            "auth/user/create",
            {
                "username": username,
                "password": password,
                "role": role,
                "display_name": display_name,
            },
        )
        print(f"    created user '{username}' ({role}, {display_name})")


def ensure_playlists(client: Client, tracks: list[dict]) -> None:
    expected = expected_playlists(tracks)
    existing = {item["name"]: item for item in library_items(client, "playlists")}
    for position, (name, count) in enumerate(PLAYLISTS):
        if name in existing:
            # Recreate instead of diffing tracks so a re-seed converges exactly.
            client.command("music/playlists/remove", {"item_id": existing[name]["item_id"]})
        playlist = client.command("music/playlists/create_playlist", {"name": name})
        uris = [
            tracks[index]["uri"]
            for index in playlist_track_indexes(position, count, len(tracks))
        ]
        client.command(
            "music/playlists/add_playlist_tracks",
            {"db_playlist_id": playlist["item_id"], "uris": uris},
        )
        wait_for(
            lambda: [
                track["name"]
                for track in client.command(
                    "music/playlists/playlist_tracks",
                    {
                        "item_id": playlist["item_id"],
                        "provider_instance_id_or_domain": "library",
                    },
                )
            ]
            == expected[name],
            f"playlist '{name}' to hold {len(uris)} tracks",
        )
        print(f"    playlist '{name}' holds {len(uris)} tracks")


def ensure_favorites(client: Client) -> None:
    artists = by_name(library_items(client, "artists"))
    albums = by_name(library_items(client, "albums"))
    tracks = by_name(library_items(client, "tracks"))
    playlists = {item["name"]: item for item in library_items(client, "playlists")}

    selected = (
        [artists[index] for index in FAVORITE_ARTISTS]
        + [albums[index] for index in FAVORITE_ALBUMS]
        + [tracks[index] for index in FAVORITE_TRACKS]
        + [playlists[name] for name in FAVORITE_PLAYLISTS]
    )
    for item in selected:
        client.command("music/favorites/add_item", {"item": item["uri"]})
    print(
        "    favorites: "
        f"{len(FAVORITE_ARTISTS)} artists, {len(FAVORITE_ALBUMS)} albums, "
        f"{len(FAVORITE_TRACKS)} tracks, {len(FAVORITE_PLAYLISTS)} playlist(s)"
    )


def clear_metadata_tasks(client: Client) -> None:
    """Cancel the metadata refreshes that the favorite writes scheduled.

    Reading a library item schedules a `metadata_lookup` background task and the
    metadata controller throttles those to one item per 30 seconds, so the
    favorite step would otherwise occupy both task slots for minutes and delay
    real user actions (adding playlist tracks, syncing). Cancelling them leaves
    the instance with a quiet task queue; the seeded favorites are already stored.
    """

    def leftovers() -> list[dict]:
        return [
            task
            for task in client.command("tasks/list")
            if (task.get("metadata") or {}).get("task_domain") == "metadata_lookup"
            and task["status"] in ("pending", "running")
        ]

    cancelled = leftovers()
    for task in cancelled:
        client.command("tasks/cancel", {"task_id": task["id"]})
    if cancelled:
        wait_for(lambda: not leftovers(), "metadata refreshes to stop", timeout=60)
    # keep the Tasks page free of the cancelled refresh rows from this seed run
    client.command("tasks/clear_finished")
    print(f"    cancelled {len(cancelled)} favorite-triggered metadata refresh task(s)")


def cmd_seed(base_url: str) -> int:
    client = Client(base_url)
    print("==> seeding the Music Assistant baseline")
    client.login(ADMIN_USER, ADMIN_PASSWORD)
    print("    admin login ok")
    ensure_core_settings(client)
    ensure_provider(client)
    ensure_users(client)
    tracks = by_name(library_items(client, "tracks"))
    ensure_playlists(client, tracks)
    ensure_favorites(client)
    clear_metadata_tasks(client)
    print("==> seed complete")
    return 0


def cmd_verify(base_url: str) -> int:
    client = Client(base_url)
    errors: list[str] = []

    def check(condition: bool, message: str) -> None:
        if condition:
            print(f"    ok: {message}")
        else:
            errors.append(message)

    print("==> verifying the Music Assistant baseline")
    try:
        client.login(ADMIN_USER, ADMIN_PASSWORD)
        print("    ok: admin login")
    except SeedError as err:
        print(f"    FAIL: {err}")
        return 1

    info = client.command("info")
    check(info.get("status") == "running", f"/info reports status running ({info.get('status')})")
    check(
        info.get("server_version") == "2.10.13",
        f"/info reports server_version 2.10.13 ({info.get('server_version')})",
    )
    check(info.get("onboard_done") is True, "onboarding completed")

    server_name = client.command(
        "config/core/get_value", {"domain": "webserver", "key": "server_name"}
    )
    check(server_name == SERVER_NAME, f"server name is '{SERVER_NAME}' (got {server_name!r})")
    online_metadata = client.command(
        "config/core/get_value", {"domain": "metadata", "key": "enable_online_metadata"}
    )
    check(online_metadata is False, "online metadata lookups are disabled")

    providers = client.command("config/providers")
    test_provider = next(
        (config for config in providers if config.get("domain") == PROVIDER_DOMAIN), None
    )
    check(test_provider is not None, f"music provider '{PROVIDER_DOMAIN}' is configured")
    check(
        bool(test_provider) and test_provider.get("status") == "loaded",
        f"music provider '{PROVIDER_DOMAIN}' status loaded",
    )
    check(
        bool(test_provider)
        and test_provider.get("default_name") == "Test / demo provider"
        and test_provider.get("type") == "music",
        "music provider is 'Test / demo provider' (type music)",
    )

    counts = library_counts(client)
    check(counts == EXPECTED_COUNTS, f"library counts are {EXPECTED_COUNTS} (got {counts})")

    artists = by_name(library_items(client, "artists"))
    albums = by_name(library_items(client, "albums"))
    tracks = by_name(library_items(client, "tracks"))
    podcasts = library_items(client, "podcasts")
    audiobooks = library_items(client, "audiobooks")
    check(
        bool(artists) and all(re.search(r" Test Artist [0-4]$", a["name"]) for a in artists),
        "artist names follow '<genre> Test Artist <n>'",
    )
    check(
        bool(albums) and all(re.search(r" Test Album [0-4]$", a["name"]) for a in albums),
        "album names follow '<genre> Test Album <n>'",
    )

    users = {user["username"]: user for user in client.command("auth/users")}
    admin = users.get(ADMIN_USER)
    check(bool(admin) and admin.get("role") == "admin", "admin user has the admin role")
    for username, _password, role, display_name in USERS:
        user = users.get(username)
        check(
            bool(user) and user.get("role") == role and user.get("display_name") == display_name,
            f"user '{username}' exists as {role} '{display_name}'",
        )

    playlists = {item["name"]: item for item in library_items(client, "playlists")}
    expected = expected_playlists(tracks)
    for name in expected:
        playlist = playlists.get(name)
        if not playlist:
            errors.append(f"playlist '{name}' is missing")
            continue
        actual = [
            track["name"]
            for track in client.command(
                "music/playlists/playlist_tracks",
                {"item_id": playlist["item_id"], "provider_instance_id_or_domain": "library"},
            )
        ]
        check(actual == expected[name], f"playlist '{name}' holds its {len(expected[name])} tracks")

    favorite_artists = by_name(library_items(client, "artists", favorite=True))
    favorite_albums = by_name(library_items(client, "albums", favorite=True))
    favorite_tracks = by_name(library_items(client, "tracks", favorite=True))
    favorite_playlists = {item["name"] for item in library_items(client, "playlists", favorite=True)}
    check(
        [item["name"] for item in favorite_artists]
        == [artists[index]["name"] for index in FAVORITE_ARTISTS],
        f"{len(FAVORITE_ARTISTS)} artists are favorited",
    )
    check(
        [item["name"] for item in favorite_albums]
        == [albums[index]["name"] for index in FAVORITE_ALBUMS],
        f"{len(FAVORITE_ALBUMS)} albums are favorited",
    )
    check(
        [item["name"] for item in favorite_tracks]
        == [tracks[index]["name"] for index in FAVORITE_TRACKS],
        f"{len(FAVORITE_TRACKS)} tracks are favorited",
    )
    check(
        favorite_playlists == set(FAVORITE_PLAYLISTS),
        f"playlist(s) {FAVORITE_PLAYLISTS} are favorited",
    )

    check(len(podcasts) == EXPECTED_COUNTS["podcasts"], "podcasts are present")
    check(len(audiobooks) == EXPECTED_COUNTS["audiobooks"], "audiobooks are present")

    if errors:
        print(f"==> verify FAILED ({len(errors)} problem(s))")
        for error in errors:
            print(f"    - {error}")
        return 1
    print("==> verify PASSED")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("seed", "verify"))
    parser.add_argument("base_url", help="Music Assistant base URL, e.g. http://127.0.0.1:8095")
    args = parser.parse_args()
    try:
        return cmd_seed(args.base_url) if args.command == "seed" else cmd_verify(args.base_url)
    except SeedError as err:
        print(f"==> FAIL: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
