# mediacleanuparr

**Purge low-rated, oversized, and empty media from your Radarr/Sonarr libraries to reclaim disk space — safely, with a dry run first.**

---

## The problem

Media libraries have a way of quietly getting out of control. You add things on a
whim, Overseerr/Jellyseerr requests pile up, and Radarr/Sonarr keep grabbing
upgrades — and before long your array is full of stuff you'll never actually
watch: one-star movies, a 200 GB show you bailed on after episode two, and a
graveyard of empty "monitored" entries and orphaned folders left behind by past
deletions.

Cleaning that up by hand is miserable. You'd have to cross-reference ratings,
sort by size, figure out what's actually taking up space, and delete each item
in Radarr/Sonarr one at a time — all while hoping you don't nuke something you
meant to keep or leave orphaned files behind.

**mediacleanuparr does that triage for you.** It connects to Radarr and Sonarr,
scores everything by audience rating, surfaces your biggest space hogs and your
empty/orphaned cruft, and lets you clean it all out from one screen — with a
**mandatory dry run** so you always see exactly what will happen before anything
is deleted. Reclaim space, keep the good stuff, and stop low-quality titles from
silently coming back.

It runs as a single Docker container with a clean web UI, is built for Unraid
(but runs anywhere Docker does), **defaults to dry-run-only**, and never deletes
outside your media folder.

- **Image:** `tophat17/mediacleanuparr`
- **Web UI:** port `8787`

---

## Table of contents

- [Features](#features)
- [Install on Unraid](#install-on-unraid)
- [Install with Docker (Compose / run)](#install-with-docker)
- [First-run setup](#first-run-setup)
- [How to use](#how-to-use)
- [Safety model](#safety-model)
- [Build from source](#build-from-source)
- [License](#license)

---

## Features

- **Rating scan (Scan & review tab).** Pulls movies/series from Radarr/Sonarr,
  scores them by **TMDb audience rating**, and flags anything below a threshold
  you pick with a 0–100 slider. You review, select/deselect, then confirm.
- **Ratings come from Radarr/Sonarr first** (the rating already in their
  metadata — free, no API calls), falling back to the **TMDb API** only when
  needed. TMDb has no daily request cap, so it scales to large libraries.
- **Biggest items tab.** Find the N titles using the most disk space (movies, TV,
  or both) regardless of rating — great for clearing large shows you never watch.
  Scans up to **500 items**, and an optional **Remove empty items & orphaned
  folders** toggle surfaces every 0-byte entry (removed from Radarr/Sonarr with
  the same re-download restriction the rest of the app applies) plus orphaned
  empty folders on disk — all pre-selected for one-click cleanup.
- **Empty / 0-byte cleanup.** A toggle that auto-selects entries with **no files
  on disk that are also below your rating threshold** and removes them from
  Radarr/Sonarr (no file-deletion needed — there's nothing to delete). It also
  finds **orphaned empty folders** under your media roots and offers to remove
  them. Well-rated empties (e.g. a monitored title not downloaded yet) are left
  alone.
- **Requested by.** When Overseerr/Jellyseerr is connected, each row shows who
  requested the title, so you don't purge something a housemate just asked for.
- **Exclude from scans.** Tick a box on any row to skip that title on all future
  scans ("don't ask again"). Excluded titles are listed with a one-click remove.
- **Overseerr / Jellyseerr ("Seerr") sync (optional).** When configured, anything
  deleted is also cleared in Seerr so it stops auto-requeuing it. The title stays
  **re-requestable** — it is **not** blacklisted.
- **Auto-unblock on re-request (optional).** Turn on *"Auto-unblock when
  re-requested in Seerr"* and point an Overseerr/Jellyseerr webhook at the app. When
  a human re-requests a previously-removed title, mediacleanuparr automatically lifts
  the block it set — removes the Radarr/Sonarr exclusion, re-monitors, and re-adds &
  searches if needed. It only ever lifts blocks **it** applied, never ones you set
  yourself.
- **Sonarr handling.** Sonarr has no native import exclusion like Radarr, so a
  single **Unmonitor flagged series** toggle keeps the show in Sonarr but stops
  it re-downloading.
- **Reports & logs.** Every deletion run is logged and saved as JSON + CSV in
  `/config/reports`, viewable on the Logs tab.

---

## Install on Unraid

mediacleanuparr is available in **Community Applications** — the easiest way to
install it.

1. Open the **Apps** tab in the Unraid web UI.
2. Search **mediacleanuparr** and click **Install**.
3. Fill in the form:

| Field | Value | Notes |
|---|---|---|
| **WebUI Port** | `8787` | Change the host side if 8787 is taken. |
| **Config** | `/mnt/user/appdata/mediacleanuparr` | Database, logs, reports. |
| **Media** | e.g. `/mnt/user/Media` | **Must match the path Radarr/Sonarr report** (see below). Read-write. |
| **TZ** *(advanced)* | `America/Vancouver` | Your timezone. |

4. **Apply**, then open the WebUI and do the [first-run setup](#first-run-setup).

> Prefer to add it by hand? Download [`unraid-template.xml`](unraid-template.xml)
> to `/boot/config/plugins/dockerMan/templates-user/` and it'll show up under
> **Docker → Add Container → Template**.

> **Path-matching gotcha:** mediacleanuparr compares file paths with what
> Radarr/Sonarr report. Mount `/media` so those paths line up. If Radarr reports
> a movie at `/mnt/user/Media/Movies/...`, map that same host path to `/media`
> **and** set `MEDIA_ROOTS` (advanced) so the container sees the same root — or
> simply mount the share at the identical path. When pointing the app at Radarr/
> Sonarr, use your server's **LAN IP** (e.g. `http://192.168.1.10:7878`), not
> `localhost`.

---

## Install with Docker

### docker-compose

```yaml
services:
  mediacleanuparr:
    image: tophat17/mediacleanuparr:latest
    container_name: mediacleanuparr
    ports:
      - "8787:8787"            # host:container — change the first number to remap
    volumes:
      - ./config:/config       # database, logs, reports
      - /path/to/your/media:/media   # mount EXACTLY as Radarr/Sonarr see it
    restart: unless-stopped
```

```bash
docker compose up -d
# open http://<server-ip>:8787
```

### docker run

```bash
docker run -d \
  --name mediacleanuparr \
  -p 8787:8787 \
  -v /path/to/appdata/mediacleanuparr:/config \
  -v /path/to/your/media:/media \
  -e TZ=America/Vancouver \
  --restart unless-stopped \
  tophat17/mediacleanuparr:latest
```

`PUID=99`, `PGID=100`, and `UMASK=002` are baked in with Unraid-friendly
defaults. Only the **port** and **media path** need setting at deploy time —
everything else is configured in the web UI.

---

## First-run setup

Open `http://<server-ip>:8787` and go to the **Setup** tab:

1. Enter your **Radarr** URL + API key and **Sonarr** URL + API key, and click
   **Test connection** on each. Use the LAN IP, not `localhost`.
2. Enter a **TheMovieDB (TMDb) API key** — free from your
   [TMDb account → Settings → API](https://www.themoviedb.org/settings/api).
   **This is required to scan.** Test it.
3. *(Optional)* Enter your **Overseerr/Jellyseerr** URL + API key to enable the
   "Requested by" column and delete-time sync. To also **auto-unblock on re-request**,
   tick that toggle, **Save**, then copy the generated Webhook URL into
   Overseerr/Jellyseerr → *Settings → Notifications → Webhook* (trigger on request
   events). A re-requested title then gets its block lifted automatically.
4. Review the safety switches (all conservative by default) and **Save**.

---

## How to use

**Scan & review tab.** Set the rating threshold with the slider, choose scope
(Movies / TV / Both), optionally enable **Include unrated** and **Clean up empty
(0-byte) items & folders**, then **Run dry scan**. Nothing is ever deleted by a
scan. Review the flagged list, adjust the checkboxes, type `DELETE`, and confirm
to act. Each row has an **Exclude** box to skip that title forever.

**Biggest items tab.** Pick Movies / TV / Both and how many to show (up to 500),
then **Find biggest items**. It lists the largest titles by size; tick the ones
to purge and confirm. Enable **Remove empty items & orphaned folders** to also
flag every 0-byte entry (removed from Radarr/Sonarr and blocked from
re-download) and orphaned empty folders on disk — these come pre-selected.

**Logs & reports tab.** A full audit trail of every action, plus downloadable
JSON/CSV reports of each deletion run.

---

## Safety model

- **Dry-run-only** is ON by default — you must explicitly turn it off to allow
  any deletion.
- **Delete files from disk** is a separate switch; file-bearing items are never
  deleted unless it's on. (Empty 0-byte entries don't need it — there are no
  files.)
- Every deletion requires you to **type `DELETE`** and confirm.
- Path guardrails reject `/`, `/config`, `/app`, system paths, a bare media root,
  and anything outside your mounted media roots.
- Empty-folder removal only touches directories **inside** your media roots that
  contain no files anywhere beneath them — never a media root itself.

---

## Build from source

```bash
git clone https://github.com/tophat17/Mediacleanuparr.git
cd Mediacleanuparr
docker build -t mediacleanuparr:latest .
```

Run the tests:

```bash
pip install -r requirements.txt
python -m pytest -q
```

---

## License

[MIT](LICENSE) © tophat17
