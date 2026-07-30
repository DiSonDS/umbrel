# DiSonDS Umbrel App Store

Community App Store for [umbrelOS](https://umbrel.com). Apps here are packaged for one-click install and are **not** vetted by the Umbrel team — only install community stores you trust.

**Store ID:** `disonds`  
**UI name:** DiSonDS App Store  
**Gallery assets:** [DiSonDS/umbrel-gallery](https://github.com/DiSonDS/umbrel-gallery)

## Add this store in umbrelOS

1. Open **App Store** → **⋯** / community stores
2. Paste the GitHub URL: `https://github.com/DiSonDS/umbrel`
3. Install apps from the DiSonDS store like any other Umbrel app

## Apps

| App | ID | Category | Description | Port |
| --- | --- | --- | --- | --- |
| [HomeTube](https://github.com/EgalitarianMonkey/hometube) | `disonds-hometube` | media | Ad-free video & playlist downloader for your media server | 8510 |
| [TorrServer](https://github.com/YouROK/TorrServer) | `disonds-torrserver` | media | Stream torrents over HTTP to any player on your network | 8097 |
| [LiteLLM](https://github.com/BerriAI/litellm) | `disonds-litellm` | ai | OpenAI-compatible gateway for 100+ LLM providers | 4001 |

## Repository layout

```text
umbrel-app-store.yml          # Store id + display name
disonds-<app>/                # One directory per app (id must match folder name)
  umbrel-app.yml              # App Store listing metadata
  docker-compose.yml          # Runtime services (app_proxy + containers)
  data/…                      # Optional scaffold for bind mounts
  hooks/…                     # Optional lifecycle hooks
```

Conventions:

- App IDs use the store prefix: `disonds-<name>`
- Icons and gallery screenshots live in [umbrel-gallery](https://github.com/DiSonDS/umbrel-gallery) under `disonds-<app>/` and are referenced by raw GitHub URLs in `umbrel-app.yml`
- Prefer the same Umbrel packaging rules as the [official App Store](https://github.com/getumbrel/umbrel-apps) (pinned images, `${APP_DATA_DIR}` persistence, `app_proxy`, etc.)

## Related Umbrel repositories

- [Official Umbrel App Store](https://github.com/getumbrel/umbrel-apps) — packages published in the official store
- [Community App Store template](https://github.com/getumbrel/umbrel-community-app-store) — starter template for custom stores like this one
