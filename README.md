# mediacleanuparr

**Prune your Radarr & Sonarr libraries by audience rating — safely, with a mandatory dry run.**

mediacleanuparr is a small self-hosted web app that scans your movie and TV
libraries, flags low-rated titles, the biggest space hogs, and empty/orphaned
entries, shows you a **dry run first**, and only deletes after you review the
list and type `DELETE`. It runs as a single Docker container with a clean web UI
and is built for Unraid (but runs anywhere Docker does).

- **Image:** `tophat17/mediacleanuparr`
- **Web UI:** port `8787`
- **Defaults to dry-run-only** and never deletes outside your media folder.

---

## Table of contents

- [Features](#features)
- [Install on Unraid](#install-on-unraid)
- [Install with Docker (Compose / run)](#install-with-docker)
- [First-run setup](#first-run-setup)
- [How to use](#how-to-use)
- [Safety model](#safety-model)
- [Build from source](#build-from-source)
- [Getting listed on the Unraid App Store](#getting-listed-on-the-unraid-app-store)
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
- **Sonarr handling.** Sonarr has no native import exclusion like Radarr, so a
  single **Unmonitor flagged series** toggle keeps the show in Sonarr but stops
  it re-downloading.
- **Reports & logs.** Every deletion run is logged and saved as JSON + CSV in
  `/config/reports`, viewable on the Logs tab.

---

## Install on Unraid

### Option A — Community Applications (once listed)

> Listing is in progress (see [Getting listed](#getting-listed-on-the-unraid-app-store)).
> When it's live:

1. Open the **Apps** tab in the Unraid web UI.
2. Search **mediacleanuparr** and click **Install**.
3. Set the two paths and the port (see the form fields below), then **Apply**.

### Option B — Add the template manually (works today)

You can run it before it's in the store by adding the template directly:

1. In Unraid, go to **Docker → Add Container**.
2. In the **Template repositories** box (Docker page, bottom), you can paste this
   repo URL to make the template available:
   `https://github.com/tophat17/Mediacleanuparr`
   — or simply download
   [`unraid-template.xml`](unraid-template.xml) to
   `/boot/config/plugins/dockerMan/templates-user/my-mediacleanuparr.xml` and it
   will appear under **Add Container → Template**.
3. Fill in the form:

| Field | Value | Notes |
|---|---|---|
| **WebUI Port** | `8787` | Change the host side if 8787 is taken. |
| **Config** | `/mnt/user/appdata/mediacleanuparr` | Database, logs, reports. |
| **Media** | e.g. `/mnt/user/Media` | **Must match the path Radarr/Sonarr report** (see below). Read-write. |
| **TZ** *(advanced)* | `America/Vancouver` | Your timezone. |

4. **Apply**, then open the WebUI and do the [first-run setup](#first-run-setup).

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
   "Requested by" column and delete-time sync.
4. Review the safety switches (all conservative by default) and **Save**.

---

## How to use

**Scan & review tab.** Set the rating threshold with the slider, choose scope
(Movies / TV / Both), optionally enable **Include unrated** and **Clean up empty
(0-byte) items & folders**, then **Run dry scan**. Nothing is ever deleted by a
scan. Review the flagged list, adjust the checkboxes, type `DELETE`, and confirm
to act. Each row has an **Exclude** box to skip that title forever.

**Biggest items tab.** Pick Movies / TV / Both and how many to show, then **Find
biggest items**. It lists the largest titles by size; tick the ones to purge and
confirm.

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

## Getting listed on the Unraid App Store

Community Applications (CA) is Unraid's app store. The current process
([official docs](https://docs.unraid.net/unraid-os/using-unraid-to/run-docker-containers/community-applications/)):

1. **Publish the image publicly** to Docker Hub (`tophat17/mediacleanuparr`) — done
   via the included GitHub Actions workflow.
2. **Provide the template + docs** — this repo includes
   [`unraid-template.xml`](unraid-template.xml), an icon, and this README.
3. **Create a support thread** on the Unraid Community forums (required). The
   template's `Support` field should point at it (it currently points at this
   repo's Issues as a fallback).
4. **Use an open-source license** — this project is MIT (see [LICENSE](LICENSE)).
5. **Submit to Community Applications** via the CA submission form linked from the
   docs above. The CA moderation team reviews submissions for safety and
   conflicts before listing.

Once approved, Unraid users install it straight from the **Apps** tab as in
[Option A](#install-on-unraid).

---

## License

[MIT](LICENSE) © tophat17
