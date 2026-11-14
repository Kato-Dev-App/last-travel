#!/usr/bin/env python3
"""Generate manifests automatically for the full, low and mods profiles.
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import tomllib
import urllib.parse
import zipfile
from pathlib import Path

VERSION_PLACEHOLDER_RE = re.compile(r"^\$\{.*\}$")
PROFILES = ("full", "low", "mods")


def detect_base_url(path: Path) -> str:
    """Derive the GitHub raw URL for path from origin and the current branch."""
    def git(*arguments: str) -> str:
        try:
            return subprocess.run(
                ["git", "-C", str(path), *arguments],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ValueError(
                "Cannot read Git configuration; specify --base-url"
            ) from exc

    remote = git("remote", "get-url", "origin")
    match = re.fullmatch(
        r"(?:https?://github\.com/|git@github\.com:|ssh://git@github\.com/)"
        r"([^/]+)/([^/]+?)(?:\.git)?/?", remote,
    )
    if not match:
        raise ValueError("origin is not a supported GitHub URL; specify --base-url")
    owner, repository = match.groups()
    # symbolic-ref also works on a newly initialized repository without commits.
    try:
        branch = git("symbolic-ref", "--quiet", "--short", "HEAD")
    except ValueError:
        # A detached checkout can still use its exact commit as a raw reference.
        branch = git("rev-parse", "HEAD")
    root = Path(git("rev-parse", "--show-toplevel")).resolve()
    relative = path.resolve().relative_to(root).as_posix()
    url = (
        f"https://raw.githubusercontent.com/{owner}/{repository}/"
        f"{urllib.parse.quote(branch, safe='')}"
    )
    if relative != ".":
        url += f"/{urllib.parse.quote(relative)}"
    return url


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_placeholder_version(zf: zipfile.ZipFile) -> str | None:
    try:
        manifest = zf.read("META-INF/MANIFEST.MF").decode("utf-8", "ignore")
    except KeyError:
        return None
    for line in manifest.splitlines():
        if line.startswith("Implementation-Version:"):
            return line.split(":", 1)[1].strip()
    return None


def read_fabric_metadata(zf: zipfile.ZipFile) -> tuple[str, str] | None:
    try:
        raw = zf.read("fabric.mod.json")
    except KeyError:
        return None
    try:
        data = json.loads(raw, strict=False)
    except json.JSONDecodeError:
        return None
    return data.get("id"), data.get("version")


def read_forge_metadata(zf: zipfile.ZipFile) -> tuple[str, str] | None:
    for candidate in ("META-INF/neoforge.mods.toml", "META-INF/mods.toml"):
        try:
            raw = zf.read(candidate)
        except KeyError:
            continue
        try:
            data = tomllib.loads(raw.decode("utf-8"))
        except tomllib.TOMLDecodeError:
            continue
        mods = data.get("mods") or []
        if not mods:
            continue
        return mods[0].get("modId"), mods[0].get("version")
    return None


def extract_metadata(jar_path: Path, loader: str) -> tuple[str, str]:
    if loader == "vanilla":
        return jar_path.stem, "unknown"

    with zipfile.ZipFile(jar_path) as zf:
        result = None
        if loader == "fabric":
            result = read_fabric_metadata(zf)
        elif loader in ("forge", "neoforge"):
            result = read_forge_metadata(zf)
        else:
            result = read_fabric_metadata(zf) or read_forge_metadata(zf)

        if result is None:
            print(f"warning: no mod metadata found in {jar_path.name}", file=sys.stderr)
            return jar_path.stem, "unknown"

        mod_id, version = result
        mod_id = mod_id or jar_path.stem
        if not version or VERSION_PLACEHOLDER_RE.match(version):
            resolved = resolve_placeholder_version(zf)
            if resolved:
                version = resolved
            else:
                print(f"warning: could not resolve version for {jar_path.name}", file=sys.stderr)
                version = version or "unknown"
        return mod_id, version


def find_subfolder(base_path: Path, aliases: list[str]) -> Path | None:
    """Finds a matching subfolder among possible aliases."""
    for alias in aliases:
        candidate = base_path / alias
        if candidate.is_dir():
            return candidate
    return None


def build_category_url(base_url: str, category: str, filename: str) -> str:
    """Constructs URL for a file in a category without duplicating folder names."""
    clean_base = base_url.rstrip("/")
    if clean_base.endswith(f"/{category}"):
        clean_base = clean_base[: -len(category) - 1]
    elif clean_base.endswith("/mods") or clean_base.endswith("/resourcepacks") or clean_base.endswith("/shaderpacks"):
        clean_base = clean_base.rsplit("/", 1)[0]
    clean_base = clean_base.rstrip("/") + "/"
    return f"{clean_base}{category}/{urllib.parse.quote(filename)}"


def process_mods(folder: Path | None, loader: str, base_url: str) -> list[dict]:
    if folder is None:
        return []
    mods = []
    for jar_path in sorted(folder.glob("*.jar")):
        mod_id, mod_version = extract_metadata(jar_path, loader)
        mods.append(
            {
                "id": mod_id,
                "name": jar_path.name,
                "version": mod_version,
                "sha256": sha256_of(jar_path),
                "url": build_category_url(base_url, "mods", jar_path.name),
            }
        )
    mods.sort(key=lambda f: f["id"])
    return mods


def process_resourcepacks(folder: Path | None, default_version: str, base_url: str) -> list[dict]:
    if folder is None:
        return []
    packs = []
    for rp_path in sorted([p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in (".zip", ".rar")]):
        pack_id = rp_path.stem.lower().replace(" ", "_").replace("-", "_")
        packs.append(
            {
                "id": pack_id,
                "name": rp_path.name,
                "version": default_version,
                "sha256": sha256_of(rp_path),
                "url": build_category_url(base_url, "resourcepacks", rp_path.name),
            }
        )
    packs.sort(key=lambda f: f["id"])
    return packs


def process_shaderpacks(folder: Path | None, default_version: str, base_url: str) -> list[dict]:
    if folder is None:
        return []
    shaders = []
    for sp_path in sorted([p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in (".zip", ".rar")]):
        shader_id = sp_path.stem.lower().replace(" ", "_").replace("-", "_")
        shaders.append(
            {
                "id": shader_id,
                "name": sp_path.name,
                "version": default_version,
                "sha256": sha256_of(sp_path),
                "url": build_category_url(base_url, "shaderpacks", sp_path.name),
            }
        )
    shaders.sort(key=lambda f: f["id"])
    return shaders


def build_manifest(path: Path, loader: str, version: str, game_version: str, base_url: str) -> dict:
    mods_dir = find_subfolder(path, ["mods", "mod"])
    rp_dir = find_subfolder(path, ["resourcepacks", "resourcepack", "textures"])
    sp_dir = find_subfolder(path, ["shaderpacks", "shaderpack", "shaders"])

    # Fallback legacy: si la carpeta pasada directamente contiene .jar y no tiene subcarpetas
    if mods_dir is None and rp_dir is None and sp_dir is None:
        direct_jars = list(path.glob("*.jar"))
        if direct_jars or path.name.lower() in ("mods", "mod"):
            mods_dir = path

    mods_list = process_mods(mods_dir, loader, base_url)
    rp_list = process_resourcepacks(rp_dir, version, base_url)
    sp_list = process_shaderpacks(sp_dir, version, base_url)

    # Use the actual directory layout, including aliases and mods/mods.
    # base_url always points to the directory passed as path.
    for folder, entries in ((mods_dir, mods_list), (rp_dir, rp_list), (sp_dir, sp_list)):
        if folder is not None:
            for entry in entries:
                relative_path = (folder / entry["name"]).relative_to(path).as_posix()
                entry["url"] = f"{base_url.rstrip('/')}/{urllib.parse.quote(relative_path)}"

    return {
        "version": version,
        "gameVersion": game_version,
        "loader": loader,
        "files": {
            "mods": mods_list,
            "resourcepacks": rp_list,
            "shaderpacks": sp_list,
        },
    }


def write_manifest(output: Path, manifest: dict) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    files_obj = manifest["files"]
    n_mods = len(files_obj["mods"])
    n_rp = len(files_obj["resourcepacks"])
    n_sp = len(files_obj["shaderpacks"])
    total = n_mods + n_rp + n_sp
    print(f"wrote {output} with {total} files ({n_mods} mods, {n_rp} resourcepacks, {n_sp} shaderpacks)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default=Path(__file__).resolve().parent, type=Path,
                        help="Repository root, or a single pack directory with --output")
    parser.add_argument("--loader", choices=["fabric", "forge", "neoforge", "vanilla"],
                        help="Override existing loader (default for new manifests: forge)")
    parser.add_argument("--version", help="Override manifest version (default: existing version or 1.0.0)")
    parser.add_argument("--game-version", help="Override Minecraft version (default: existing version or 1.20.1)")
    parser.add_argument("--base-url", help="URL of --path (default: detected from Git origin and current branch)")
    parser.add_argument("--output", type=Path, help="Generate only one manifest at this path")
    args = parser.parse_args()

    if not args.path.is_dir():
        parser.error(f"{args.path} is not a directory")

    try:
        base_url = args.base_url or detect_base_url(args.path)
    except ValueError as exc:
        parser.error(str(exc))

    if args.output is not None:
        targets = [(args.path, args.output, base_url)]
    else:
        targets = [
            (args.path / profile, args.path / f"manifest-{profile}.json",
             f"{base_url.rstrip('/')}/{profile}")
            for profile in PROFILES if (args.path / profile).is_dir()
        ]
        if not targets:
            parser.error("No full/, low/ or mods/ profiles found; use --output for a single pack")

    # Validate all existing manifests before overwriting any of them.
    jobs = []
    for path, output, profile_url in targets:
        existing = {}
        if output.exists():
            try:
                existing = json.loads(output.read_text(encoding="utf-8"))
                if not isinstance(existing, dict):
                    raise ValueError("expected a JSON object")
            except (OSError, ValueError) as exc:
                parser.error(f"Cannot read {output}: {exc}")
        jobs.append((
            path, output, profile_url,
            args.loader or existing.get("loader") or "forge",
            args.version or existing.get("version") or "1.0.0",
            args.game_version or existing.get("gameVersion") or "1.20.1",
        ))

    for path, output, profile_url, loader, version, game_version in jobs:
        manifest = build_manifest(path, loader, version, game_version, profile_url)
        write_manifest(output, manifest)

    if args.output is None:
        print("\nManifest raw URLs:")
        root_url = base_url.rstrip("/")
        for _, output, _ in targets:
            print(f"{output.name}: {root_url}/{urllib.parse.quote(output.name)}")


if __name__ == "__main__":
    main()
