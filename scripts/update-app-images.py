#!/usr/bin/env python3
"""Check Umbrel store apps for newer upstream images and open update PRs.

Scans app directories for docker-compose.yml + umbrel-app.yml, compares pinned
image tags/digests against GitHub Releases and the container registry, then
optionally writes updates and opens one PR per app.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import textwrap
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "PyYAML is required. Install with: pip install pyyaml"
    ) from exc

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / ".github" / "image-updates.yml"
IMAGE_RE = re.compile(
    r"^(?P<name>[^:@\s]+)"
    r"(?::(?P<tag>[^@\s]+))?"
    r"(?:@(?P<digest>sha256:[a-f0-9]+))?$"
)
REQUIRED_PLATFORMS = {"linux/amd64", "linux/arm64"}
TOP_LEVEL_KEY_RE = re.compile(r"^[A-Za-z0-9_]+:")


@dataclass
class ImageRef:
    name: str
    tag: str
    digest: str

    @property
    def tagged(self) -> str:
        return f"{self.name}:{self.tag}"

    @property
    def pinned(self) -> str:
        return f"{self.name}:{self.tag}@{self.digest}"

    @classmethod
    def parse(cls, value: str) -> ImageRef:
        match = IMAGE_RE.match(value.strip())
        if not match or not match.group("tag") or not match.group("digest"):
            raise ValueError(f"Expected name:tag@sha256:... image ref, got: {value!r}")
        return cls(match.group("name"), match.group("tag"), match.group("digest"))


@dataclass
class ServiceImage:
    service: str
    image: ImageRef
    is_primary: bool


@dataclass
class ImageChange:
    service: str
    old: ImageRef
    new: ImageRef
    reason: str


@dataclass
class AppUpdate:
    app_id: str
    app_name: str
    app_dir: Path
    old_version: str
    new_version: str
    release_notes: str
    release_url: str | None
    changes: list[ImageChange] = field(default_factory=list)
    skip_reason: str | None = None


def load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def load_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"app_globs": ["disonds-*"], "apps": {}}
    data = load_yaml(path) or {}
    data.setdefault("app_globs", ["disonds-*"])
    data.setdefault("apps", {})
    return data


def discover_apps(config: dict[str, Any]) -> list[Path]:
    globs = config.get("app_globs") or ["disonds-*"]
    found: set[Path] = set()
    for pattern in globs:
        for path in ROOT.glob(pattern):
            if path.is_dir() and (path / "docker-compose.yml").is_file() and (
                path / "umbrel-app.yml"
            ).is_file():
                found.add(path)
    return sorted(found, key=lambda p: p.name)


def github_request(url: str, token: str | None) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "disonds-umbrel-image-updater",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API {exc.code} for {url}: {body}") from exc


def parse_github_repo(repo_url: str) -> tuple[str, str] | None:
    match = re.search(r"github\.com[:/]([^/]+)/([^/#]+?)(?:\.git)?/?$", repo_url or "")
    if not match:
        return None
    return match.group(1), match.group(2)


def list_github_releases(owner: str, repo: str, token: str | None) -> list[dict[str, Any]]:
    releases: list[dict[str, Any]] = []
    page = 1
    while page <= 5:
        url = (
            f"https://api.github.com/repos/{owner}/{repo}/releases"
            f"?per_page=30&page={page}"
        )
        batch = github_request(url, token)
        if not batch:
            break
        releases.extend(batch)
        if len(batch) < 30:
            break
        page += 1
    return [
        release
        for release in releases
        if not release.get("draft") and not release.get("prerelease")
    ]


def primary_service_from_compose(compose: dict[str, Any], app_id: str) -> str | None:
    services = compose.get("services") or {}
    proxy = services.get("app_proxy") or {}
    env = proxy.get("environment") or {}
    app_host = env.get("APP_HOST")
    if not isinstance(app_host, str):
        return None
    prefix = f"{app_id}_"
    suffix = "_1"
    if app_host.startswith(prefix) and app_host.endswith(suffix):
        return app_host[len(prefix) : -len(suffix)]
    return None


def collect_service_images(
    compose: dict[str, Any],
    app_id: str,
    ignore_services: set[str],
) -> list[ServiceImage]:
    services = compose.get("services") or {}
    primary = primary_service_from_compose(compose, app_id)
    result: list[ServiceImage] = []
    for name, service in services.items():
        if name == "app_proxy" or name in ignore_services:
            continue
        if not isinstance(service, dict):
            continue
        image_value = service.get("image")
        if not isinstance(image_value, str):
            continue
        result.append(
            ServiceImage(
                service=name,
                image=ImageRef.parse(image_value),
                is_primary=(name == primary),
            )
        )
    if primary and not any(item.is_primary for item in result):
        raise ValueError(f"{app_id}: APP_HOST points to missing service {primary!r}")
    if not any(item.is_primary for item in result) and result:
        # Fallback: first non-ignored service is primary.
        result[0].is_primary = True
    return result


def map_release_to_image_tag(release_tag: str, current_image_tag: str) -> str:
    cur_v = bool(re.match(r"^v\d", current_image_tag))
    rel_v = bool(re.match(r"^v\d", release_tag))
    if cur_v and not rel_v:
        return f"v{release_tag}"
    if not cur_v and rel_v:
        return release_tag[1:]
    return release_tag


def strip_leading_v(tag: str) -> str:
    return tag[1:] if re.match(r"^v\d", tag) else tag


def split_packaging_version(version: str) -> tuple[str, int | None]:
    match = re.fullmatch(r"^(?P<base>.+)-(?P<suffix>\d+)$", version)
    if not match:
        return version, None
    base = match.group("base")
    # Avoid treating semver like 1.94.0 as base+suffix incorrectly: require that
    # base itself looks like an upstream version (contains a letter or a dot, or
    # MatriX-style), which all our apps do. Numeric-only bases are left intact.
    if re.fullmatch(r"\d+", base):
        return version, None
    return base, int(match.group("suffix"))


def compute_manifest_version(
    old_version: str,
    old_primary_tag: str,
    new_primary_tag: str,
    primary_digest_changed: bool,
) -> str:
    old_base, old_suffix = split_packaging_version(old_version)
    new_upstream = strip_leading_v(new_primary_tag)
    if old_primary_tag != new_primary_tag:
        return new_upstream
    if primary_digest_changed or old_base != new_upstream:
        # Same upstream tag, packaging/digest refresh.
        next_suffix = 1 if old_suffix is None else old_suffix + 1
        return f"{new_upstream}-{next_suffix}"
    return old_version


def summarize_release_notes(
    app_name: str,
    new_version: str,
    release: dict[str, Any] | None,
    *,
    digest_only: bool,
    sidecar_only: bool = False,
) -> str:
    if sidecar_only:
        return (
            f"Refreshes pinned sidecar image digest(s) for {app_name} "
            f"({new_version})."
        )
    if digest_only:
        return (
            f"Refreshes the {app_name} container image digest for {new_version} "
            f"(same upstream tag, rebuilt image)."
        )
    body = (release or {}).get("body") or ""
    body = body.replace("\r\n", "\n").strip()
    if body:
        # Keep the PR/manifest notes short and Umbrel-user oriented.
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        excerpt = " ".join(lines)
        excerpt = re.sub(r"\s+", " ", excerpt)
        if len(excerpt) > 400:
            excerpt = excerpt[:397].rstrip() + "..."
        return excerpt
    return f"Updates {app_name} to {new_version}."


def run_cmd(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=check, text=True, capture_output=True)


def inspect_image(tagged: str) -> tuple[str, set[str]]:
    proc = run_cmd(
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            tagged,
            "--format",
            "{{json .}}",
        ]
    )
    data = json.loads(proc.stdout)
    digest = None
    platforms: set[str] = set()

    manifest = data.get("manifest") or data.get("Manifest") or {}
    if isinstance(manifest, dict):
        digest = manifest.get("digest") or manifest.get("Digest")
        manifests = manifest.get("manifests") or manifest.get("Manifests") or []
        for item in manifests:
            platform = item.get("platform") or item.get("Platform") or {}
            os_name = platform.get("os") or platform.get("OS")
            arch = platform.get("architecture") or platform.get("Architecture")
            variant = platform.get("variant") or platform.get("Variant")
            if os_name and arch and os_name != "unknown" and arch != "unknown":
                key = f"{os_name}/{arch}"
                if variant:
                    key = f"{key}/{variant}"
                platforms.add(f"{os_name}/{arch}")

    if not digest:
        # Fallback: parse human-readable inspect output.
        human = run_cmd(["docker", "buildx", "imagetools", "inspect", tagged])
        digest_match = re.search(r"^Digest:\s*(sha256:[a-f0-9]+)", human.stdout, re.M)
        if not digest_match:
            raise RuntimeError(f"Could not resolve digest for {tagged}")
        digest = digest_match.group(1)
        for match in re.finditer(
            r"Platform:\s*(linux)/(amd64|arm64)", human.stdout
        ):
            platforms.add(f"{match.group(1)}/{match.group(2)}")

    if not digest.startswith("sha256:"):
        raise RuntimeError(f"Unexpected digest for {tagged}: {digest}")
    return digest, platforms


def replace_image_line(content: str, old: ImageRef, new: ImageRef) -> str:
    old_line = f"image: {old.pinned}"
    new_line = f"image: {new.pinned}"
    if old_line not in content:
        raise RuntimeError(f"Could not find image line for {old.pinned}")
    return content.replace(old_line, new_line, 1)


def replace_version(content: str, new_version: str) -> str:
    replaced, count = re.subn(
        r'^version:\s*[\'"]?[^\'"\n]+[\'"]?\s*$',
        f'version: "{new_version}"',
        content,
        count=1,
        flags=re.M,
    )
    if count != 1:
        raise RuntimeError("Could not replace version field in umbrel-app.yml")
    return replaced


def replace_release_notes(content: str, notes: str) -> str:
    lines = content.splitlines(keepends=True)
    start = None
    for idx, line in enumerate(lines):
        if line.startswith("releaseNotes:"):
            start = idx
            break
    if start is None:
        raise RuntimeError("Could not find releaseNotes in umbrel-app.yml")

    end = len(lines)
    for idx in range(start + 1, len(lines)):
        if TOP_LEVEL_KEY_RE.match(lines[idx]):
            end = idx
            break

    wrapped = textwrap.fill(
        notes.strip(),
        width=78,
        break_long_words=False,
        break_on_hyphens=False,
    )
    note_lines = [f"  {line}\n" for line in wrapped.splitlines()] or ["  \n"]
    block = ["releaseNotes: >-\n", *note_lines]
    if end < len(lines) and lines[end - 1].strip():
        # Keep a blank line before the next top-level key when the original had one.
        if lines[end - 1] != "\n" and not note_lines[-1].endswith("\n\n"):
            pass
    if end < len(lines) and lines[start:end] and lines[end - 1].strip() == "":
        block.append("\n")
    elif end < len(lines) and lines[end] and TOP_LEVEL_KEY_RE.match(lines[end]):
        # Ensure separation before next key.
        if not block[-1].endswith("\n"):
            block[-1] += "\n"
        # Original files usually have a blank line after releaseNotes.
        if end == start + 1 or (end > start and lines[end - 1].strip()):
            block.append("\n")

    return "".join(lines[:start] + block + lines[end:])


def apply_app_update(update: AppUpdate) -> None:
    compose_path = update.app_dir / "docker-compose.yml"
    manifest_path = update.app_dir / "umbrel-app.yml"
    compose_text = compose_path.read_text(encoding="utf-8")
    manifest_text = manifest_path.read_text(encoding="utf-8")

    for change in update.changes:
        compose_text = replace_image_line(compose_text, change.old, change.new)

    manifest_text = replace_version(manifest_text, update.new_version)
    manifest_text = replace_release_notes(manifest_text, update.release_notes)

    compose_path.write_text(compose_text, encoding="utf-8")
    manifest_path.write_text(manifest_text, encoding="utf-8")


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run_cmd(["git", "-C", str(ROOT), *args], check=check)


def default_branch() -> str:
    proc = git("rev-parse", "--abbrev-ref", "origin/HEAD", check=False)
    if proc.returncode == 0 and proc.stdout.strip().startswith("origin/"):
        return proc.stdout.strip().split("/", 1)[1]
    for candidate in ("master", "main"):
        probe = git("show-ref", "--verify", f"refs/remotes/origin/{candidate}", check=False)
        if probe.returncode == 0:
            return candidate
    return "master"


def build_pr_body(update: AppUpdate) -> str:
    lines = [
        "## Summary",
        f"- Automated image update for `{update.app_id}` ({update.app_name})",
        f"- Manifest version: `{update.old_version}` → `{update.new_version}`",
        "",
        "## Image changes",
    ]
    for change in update.changes:
        lines.append(
            f"- `{change.service}`: `{change.old.pinned}` → `{change.new.pinned}` "
            f"({change.reason})"
        )
    lines.extend(
        [
            "",
            "## Verification",
            "- Digests resolved with `docker buildx imagetools inspect`",
            f"- Required platforms: `{', '.join(sorted(REQUIRED_PLATFORMS))}`",
        ]
    )
    if update.release_url:
        lines.extend(["", f"Upstream release: {update.release_url}"])
    lines.extend(
        [
            "",
            "## Manual checklist before merge",
            "- [ ] Confirm upstream release notes / breaking changes",
            "- [ ] Confirm multi-arch pull is acceptable for this app",
            "- [ ] Test update path on umbrelOS if the change looks risky",
            "",
            "_This PR was opened by `scripts/update-app-images.py`. It will not auto-merge._",
        ]
    )
    return "\n".join(lines) + "\n"


def gh_args(*args: str) -> list[str]:
    command = ["gh", *args]
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo:
        # Insert --repo after the subcommand (e.g. gh pr list --repo ...)
        if len(args) >= 2:
            return ["gh", args[0], args[1], "--repo", repo, *args[2:]]
        return ["gh", *args, "--repo", repo]
    return command


def open_or_update_pr(update: AppUpdate, branch: str, base: str) -> None:
    title = f"chore({update.app_id}): update to {update.new_version}"
    body = build_pr_body(update)
    body_file = ROOT / ".git" / f"pr-body-{update.app_id}.md"
    body_file.write_text(body, encoding="utf-8")

    existing = run_cmd(
        gh_args("pr", "list", "--head", branch, "--json", "number,url"),
        check=False,
    )
    if existing.returncode == 0 and json.loads(existing.stdout or "[]"):
        pr = json.loads(existing.stdout)[0]
        print(f"Updated existing PR for {update.app_id}: {pr['url']}")
        return

    created = run_cmd(
        gh_args(
            "pr",
            "create",
            "--base",
            base,
            "--head",
            branch,
            "--title",
            title,
            "--body-file",
            str(body_file),
        )
    )
    print(f"Opened PR for {update.app_id}:\n{created.stdout.strip()}")


def commit_and_push_app(update: AppUpdate, base: str) -> None:
    branch = f"chore/update-{update.app_id}"
    git("fetch", "origin", base)
    git("checkout", "-B", branch, f"origin/{base}")
    apply_app_update(update)
    git("add", str(update.app_dir / "docker-compose.yml"), str(update.app_dir / "umbrel-app.yml"))
    status = git("status", "--porcelain")
    if not status.stdout.strip():
        print(f"No file changes for {update.app_id} after write; skipping PR")
        git("checkout", base, check=False)
        return

    git(
        "commit",
        "-m",
        f"chore({update.app_id}): update to {update.new_version}",
    )
    git("push", "-u", "origin", branch, "--force")
    open_or_update_pr(update, branch, base)
    git("checkout", base, check=False)


def plan_app_update(
    app_dir: Path,
    config: dict[str, Any],
    token: str | None,
) -> AppUpdate | None:
    app_id = app_dir.name
    app_cfg = (config.get("apps") or {}).get(app_id) or {}
    ignore_services = set(app_cfg.get("ignore_services") or [])

    compose = load_yaml(app_dir / "docker-compose.yml")
    manifest = load_yaml(app_dir / "umbrel-app.yml")
    app_name = str(manifest.get("name") or app_id)
    old_version = str(manifest.get("version"))
    repo_url = str(manifest.get("repo") or "")

    services = collect_service_images(compose, app_id, ignore_services)
    if not services:
        print(f"Skipping {app_id}: no pinned images found")
        return None

    primary = next(item for item in services if item.is_primary)
    release = None
    release_url = None
    target_primary_tag = primary.image.tag
    github_repo = parse_github_repo(repo_url)

    if github_repo:
        owner, repo = github_repo
        try:
            releases = list_github_releases(owner, repo, token)
        except Exception as exc:  # noqa: BLE001 - continue with digest-only path
            print(f"Warning: {app_id}: GitHub releases unavailable: {exc}")
            releases = []
        if releases:
            latest = releases[0]
            candidate_tag = map_release_to_image_tag(latest["tag_name"], primary.image.tag)
            # Prefer the newest release whose mapped image tag can be inspected.
            chosen = None
            for item in releases:
                mapped = map_release_to_image_tag(item["tag_name"], primary.image.tag)
                try:
                    inspect_image(f"{primary.image.name}:{mapped}")
                except Exception:  # noqa: BLE001
                    continue
                chosen = item
                candidate_tag = mapped
                break
            if chosen is not None:
                release = chosen
                release_url = chosen.get("html_url")
                target_primary_tag = candidate_tag

    changes: list[ImageChange] = []
    skip_reason = None

    for service in services:
        if service.is_primary:
            desired_tag = target_primary_tag
            reason = (
                "new upstream release"
                if desired_tag != service.image.tag
                else "digest refresh"
            )
        else:
            desired_tag = service.image.tag
            reason = "sidecar digest refresh"

        tagged = f"{service.image.name}:{desired_tag}"
        try:
            digest, platforms = inspect_image(tagged)
        except Exception as exc:  # noqa: BLE001
            if service.is_primary:
                return AppUpdate(
                    app_id=app_id,
                    app_name=app_name,
                    app_dir=app_dir,
                    old_version=old_version,
                    new_version=old_version,
                    release_notes="",
                    release_url=release_url,
                    skip_reason=f"failed to inspect {tagged}: {exc}",
                )
            print(f"Warning: {app_id}/{service.service}: inspect failed: {exc}")
            continue

        missing = REQUIRED_PLATFORMS - platforms
        # Single-arch images sometimes report platforms only via human output;
        # if platforms is empty, re-check via human inspect before failing.
        if missing and platforms:
            if service.is_primary:
                return AppUpdate(
                    app_id=app_id,
                    app_name=app_name,
                    app_dir=app_dir,
                    old_version=old_version,
                    new_version=old_version,
                    release_notes="",
                    release_url=release_url,
                    skip_reason=(
                        f"{tagged} missing platforms {sorted(missing)}; "
                        f"found {sorted(platforms)}"
                    ),
                )
            print(
                f"Warning: {app_id}/{service.service}: missing {sorted(missing)}; skipping"
            )
            continue
        if not platforms:
            # Digest-only image or inspect JSON without platform list: verify via human text.
            human = run_cmd(["docker", "buildx", "imagetools", "inspect", tagged])
            found = set(
                f"{m.group(1)}/{m.group(2)}"
                for m in re.finditer(r"Platform:\s*(linux)/(amd64|arm64)", human.stdout)
            )
            missing = REQUIRED_PLATFORMS - found
            if missing:
                if service.is_primary:
                    return AppUpdate(
                        app_id=app_id,
                        app_name=app_name,
                        app_dir=app_dir,
                        old_version=old_version,
                        new_version=old_version,
                        release_notes="",
                        release_url=release_url,
                        skip_reason=(
                            f"{tagged} missing platforms {sorted(missing)}"
                        ),
                    )
                continue

        new_ref = ImageRef(service.image.name, desired_tag, digest)
        if new_ref.pinned == service.image.pinned:
            continue
        changes.append(
            ImageChange(
                service=service.service,
                old=service.image,
                new=new_ref,
                reason=reason,
            )
        )

    if not changes:
        if skip_reason:
            return AppUpdate(
                app_id=app_id,
                app_name=app_name,
                app_dir=app_dir,
                old_version=old_version,
                new_version=old_version,
                release_notes="",
                release_url=release_url,
                skip_reason=skip_reason,
            )
        print(f"OK {app_id}: images are current")
        return None

    primary_change = next((c for c in changes if c.service == primary.service), None)
    sidecar_only = False
    if primary_change is None:
        # Sidecar-only refresh: bump packaging suffix on the current manifest version.
        base, suffix = split_packaging_version(old_version)
        new_version = f"{base}-{(suffix or 0) + 1}"
        digest_only = True
        sidecar_only = True
    else:
        new_version = compute_manifest_version(
            old_version,
            primary.image.tag,
            primary_change.new.tag,
            primary_digest_changed=primary_change.old.tag == primary_change.new.tag,
        )
        digest_only = primary_change.old.tag == primary_change.new.tag

    notes = summarize_release_notes(
        app_name,
        new_version,
        release,
        digest_only=digest_only,
        sidecar_only=sidecar_only,
    )
    return AppUpdate(
        app_id=app_id,
        app_name=app_name,
        app_dir=app_dir,
        old_version=old_version,
        new_version=new_version,
        release_notes=notes,
        release_url=release_url,
        changes=changes,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Path to image-updates.yml",
    )
    parser.add_argument(
        "--app",
        action="append",
        dest="apps",
        help="Limit to one or more app ids (repeatable)",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write file changes to the working tree (without --open-pr)",
    )
    parser.add_argument(
        "--open-pr",
        action="store_true",
        help="Create/update one PR per updated app (implies writing on a branch)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(args.config)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    apps = discover_apps(config)
    if args.apps:
        wanted = set(args.apps)
        apps = [path for path in apps if path.name in wanted]
        missing = wanted - {path.name for path in apps}
        if missing:
            print(f"Unknown app ids: {', '.join(sorted(missing))}", file=sys.stderr)
            return 2

    updates: list[AppUpdate] = []
    skipped: list[AppUpdate] = []
    for app_dir in apps:
        print(f"Checking {app_dir.name}...")
        update = plan_app_update(app_dir, config, token)
        if update is None:
            continue
        if update.skip_reason:
            print(f"SKIP {update.app_id}: {update.skip_reason}")
            skipped.append(update)
            continue
        updates.append(update)
        print(
            f"UPDATE {update.app_id}: {update.old_version} -> {update.new_version} "
            f"({len(update.changes)} image change(s))"
        )
        for change in update.changes:
            print(f"  - {change.service}: {change.old.pinned}")
            print(f"    -> {change.new.pinned} ({change.reason})")

    if skipped:
        print(f"\nSkipped {len(skipped)} app(s) due to inspect/platform errors.")

    if not updates:
        print("No updates to open.")
        return 0

    if args.open_pr:
        base = default_branch()
        # Ensure we can return to a detached-safe base branch locally/CI.
        git("checkout", "-B", base, f"origin/{base}", check=False)
        for update in updates:
            print(f"Opening PR for {update.app_id}...")
            commit_and_push_app(update, base)
        return 0

    if args.write:
        for update in updates:
            apply_app_update(update)
            print(f"Wrote updates for {update.app_id}")
        return 0

    print("\nDry run only. Re-run with --write or --open-pr to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
