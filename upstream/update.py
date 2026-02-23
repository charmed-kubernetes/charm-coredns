#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Update to a new upstream release."""

import argparse
import json
import logging
import re
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from itertools import accumulate
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Generator, Iterable, List, Optional, Set, Tuple, TypedDict

import yaml
from semver import VersionInfo

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)
GH_REPO = "https://api.github.com/repos/{repo}"
GH_TAGS = "https://api.github.com/repos/{repo}/tags"
GH_BRANCH = "https://api.github.com/repos/{repo}/branches/{branch}"
GH_COMMIT = "https://api.github.com/repos/{repo}/commits/{sha}"
GH_RAW = "https://raw.githubusercontent.com/{repo}/{branch}/{path}/{rel}/{manifest}"
ROCKS_CC = "upload.rocks.canonical.com:5000/cdk"


def _ver_maker(v: str) -> Tuple[int, ...]:
    return tuple(map(int, v.split(".")))


SOURCES = dict(
    coredns=dict(
        repo="kubernetes/kubernetes",
        manifest="coredns.yaml.sed",
        release_tags=True,
        path="cluster/addons/dns/coredns/",
        version_parser=VersionInfo.parse,
        minimum="v1.32.0",
    ),
)
FILEDIR = Path(__file__).parent
VERSION_RE = re.compile(r"^v\d+\.\d+")
IMG_RE = re.compile(r"^(\s+image:\s+)(\S+)")


@dataclass(frozen=True)
class Registry:
    """Object to define how to contact a Registry."""

    base: str
    user_pass: Optional[str] = None

    @property
    def name(self) -> str:
        """Get the name of the registry."""
        name, *_ = self.base.split("/")
        return name

    @property
    def path(self) -> List[str]:
        """Get the path to the registry."""
        _, *path = self.base.split("/")
        return path

    @property
    def user(self) -> str:
        """Get the user for the registry."""
        user, _ = self.user_pass.split(":", 1)
        return user

    @property
    def password(self) -> str:
        """Get the password for the registry."""
        _, pw = self.user_pass.split(":", 1)
        return pw

    @property
    def creds(self) -> List["SyncCreds"]:
        """Get credentials as a SyncCreds Dict."""
        creds = []
        if self.user_pass:
            creds.append(
                {
                    "registry": self.name,
                    "user": self.user,
                    "pass": self.password,
                }
            )
        return creds


@dataclass(frozen=True)
class Release:
    """Defines a release type."""

    name: str
    path: Path = Path()
    size: int = 0
    upstream: str = ""

    @property
    def drop_sed(self) -> Path:
        return Path(self.upstream.removesuffix(".sed"))

    def __hash__(self) -> int:
        """Unique based on its name."""
        return hash(self.name)

    def __eq__(self, other) -> bool:
        """Comparable based on its name."""
        return isinstance(other, Release) and self.name == other.name

    def __lt__(self, other) -> bool:
        """Compare version numbers."""
        a, b = self.name[1:], other.name[1:]
        try:
            return VersionInfo.parse(a) < VersionInfo.parse(b)
        except ValueError:
            return _ver_maker(a) < _ver_maker(b)


SyncAsset = TypedDict("SyncAsset", {"source": str, "target": str, "type": str})
SyncCreds = TypedDict("SyncCreds", {"registry": str, "user": str, "pass": str})


class SyncConfig(TypedDict):
    """Type definition for building sync config."""

    version: int
    creds: List[SyncCreds]
    sync: List[SyncAsset]


def migrate_source(image: str) -> str:
    """Source image registries were migrated to a new registry."""
    return image


def sync_asset(image: str, registry: Registry):
    """Factory for generating SyncAssets."""
    _, *name_tag = image.split("/")
    full_path = "/".join(registry.path + name_tag)
    dest = f"{registry.name}/{full_path}"
    return SyncAsset(source=migrate_source(image), target=dest, type="image")



def main(source: str, registry: Registry, check: bool, debug: bool):
    """Main update logic."""
    latest, local_releases = gather_releases(source)
    unique_releases = list(dict.fromkeys(accumulate((sorted(local_releases)), dedupe)))
    all_images = set(image for release in unique_releases for image in images(source, release))
    mirror_image(all_images, registry, check, debug)
    return latest, all_images


def _gather_repo_tags(tree_url: str) -> List:
    """Retrieve semantically ordered release tags from GitHub, following pagination.

    Returns:
        Release tag strings sorted from newest to oldest.

    Raises:
        ValueError: If no tags are retrieved.

    """
    tag_names: List[str] = []

    while tree_url:
        resp = urllib.request.urlopen(tree_url, timeout=5)
        page = json.loads(resp.read())
        if not page and not tag_names:
            raise ValueError("No k8s tags retrieved.")
        tag_names.extend([tag["name"] for tag in page])

        link = resp.headers.get("Link", "")
        if not link:
            break

        # parse Link header and follow 'next' if present
        next_parsed = {record for record in link.split(",") if 'rel="next"' in record}
        if next_parsed:
            first = next_parsed.pop()
            tree_url = first.split(";")[0].strip().strip("<>")
        else:
            break

    return tag_names

def gather_releases(source: str) -> Tuple[str, Set[Release]]:
    """Fetch from github the release manifests by version."""
    context = dict(**SOURCES[source])
    version_parser = context["version_parser"]
    releases = []
    if context.get("default_branch"):
        with urllib.request.urlopen(GH_REPO.format(**context)) as resp:
            context["branch"] = json.load(resp)["default_branch"]
        with urllib.request.urlopen(GH_BRANCH.format(**context)) as resp:
            branch = json.load(resp)
            context["sha"] = branch["commit"]["sha"]
        with urllib.request.urlopen(GH_COMMIT.format(**context)) as resp:
            commit = json.load(resp)
            tree_url = commit["commit"]["tree"]["url"]
        for part in Path(context["path"]).parts:
            with urllib.request.urlopen(tree_url) as resp:
                tree = json.load(resp)
                tree_url = next(item["url"] for item in tree["tree"] if item["path"] == part)
        with urllib.request.urlopen(tree_url) as resp:
            releases = sorted(
                [
                    Release(item["path"], upstream=GH_RAW.format(rel=item["path"], **context))
                    for item in json.load(resp)["tree"]
                    if VERSION_RE.match(item["path"])
                    and version_parser(context["minimum"]) <= version_parser(item["path"])
                ],
                key=lambda r: version_parser(r.name),
                reverse=True,
            )
    elif context.get("release_tags"):
        resp = _gather_repo_tags(GH_TAGS.format(**context))
        releases = sorted(
            [
                Release(
                    item,
                    upstream=GH_RAW.format(branch=item, rel="", **context),
                )
                for item in resp
                if (
                    VERSION_RE.match(item)
                    and not version_parser(item[1:]).prerelease
                    and version_parser(context["minimum"][1:])
                    <= version_parser(item[1:])
                )
            ],
            key=lambda r: version_parser(r.name[1:]),
            reverse=True,
        )

    releases = [download(source, release) for release in releases]
    return releases[0].name, set(releases)


def gather_current(source: str) -> Set[Release]:
    """Gather currently supported manifests by the charm."""
    manifest = SOURCES[source]["manifest"]
    return set(
        Release(release_path.parent.name, release_path, release_path.stat().st_size)
        for release_path in (FILEDIR / source / "manifests").glob(f"*/{manifest}")
    )


def find_images(path: Path):
    """Replace images in a release."""
    lines = path.read_text().splitlines(True)
    return [m.groups()[-1] for img in lines if (m := IMG_RE.match(img))]


def download(source: str, release: Release) -> Release:
    """Download the manifest files for a specific release."""
    log.info(f"Getting Release {source}: {release.name}")
    # open a temporary file to download the manifest
    with NamedTemporaryFile(mode="wb", delete=False) as tmpfile:
        path, _ = urllib.request.urlretrieve(release.upstream, tmpfile.name)
        images = find_images(Path(path))
        image_tags = {img.split(":")[-1] for img in images}
        r = Release(image_tags.pop(), Path(tmpfile.name), release.size)
        dest = FILEDIR / source / "manifests" / r.name / release.drop_sed.name
        dest.parent.mkdir(exist_ok=True)
        shutil.move(r.path, dest)
    return Release(r.name, dest, release.size)

def copy(tmp: Path, manifest: str, release: Release) -> Release:
    """Copy the manifest files for a specific release."""
    log.info(f"Copying Release {manifest}: {release.name}")
    dest = FILEDIR / manifest / "manifests" / release.name
    dest.parent.mkdir(exist_ok=True)
    src = tmp / manifest / "manifests" / release.name
    shutil.copy(src, dest)
    return Release(release.name, dest, release.size)


def dedupe(this: Release, next: Release) -> Release:
    """Remove duplicate releases.

    returns this release if this==next by content
    returns next release if this!=next by content
    """
    if this.path.read_text() != next.path.read_text():
        # Found different in at least one file
        return next

    next.path.unlink()
    next.path.parent.rmdir()
    log.info(f"Deleting Duplicate Release {next.name}")
    return this


def images(source: str, component: Release) -> Generator[str, None, None]:
    """Yield all images from each release."""
    with Path(component.path).open() as fp:
        for line in fp:
            if m := IMG_RE.match(line):
                image = m.groups()[1]
                yield image


def mirror_image(images: Iterable[str], registry: Registry, check: bool, debug: bool):
    """Synchronize all source images to target registry, only pushing changed layers."""
    sync_config = SyncConfig(
        version=1,
        creds=registry.creds,
        sync=[sync_asset(image, registry) for image in images],
    )
    with NamedTemporaryFile(mode="w") as tmpfile:
        yaml.safe_dump(sync_config, tmpfile)
        command = "check" if check else "once"
        args = ["regsync", "-c", tmpfile.name, command]
        args += ["-v", "debug"] if debug else []
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
        )
        while proc.returncode is None:
            for line in proc.stdout:
                log.warning(line.strip())
            proc.poll()


def get_argparser():
    """Build the argparse instance."""
    parser = argparse.ArgumentParser(
        description="Update from upstream releases.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--registry",
        default=ROCKS_CC,
        type=str,
        help="Registry to which images should be mirrored.\n\n"
        "example\n"
        "  --registry my.registry:5000/path\n"
        "\n",
    )
    parser.add_argument(
        "--user_pass",
        default=None,
        type=str,
        help="Username and password for the registry separated by a colon\n\n"
        "if missing, regsync will attempt to use authfrom ${HOME}/.docker/config.json\n"
        "example\n"
        "  --user-pass myuser:mypassword\n"
        "\n",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="If selected, will not run the sync\nbut instead checks if a sync is necessary",
    )
    parser.add_argument(
        "--debug", action="store_true", help="If selected, regsync debug will appear"
    )
    parser.add_argument(
        "--sources",
        nargs="+",
        default=list(SOURCES.keys()),
        choices=SOURCES.keys(),
        type=str,
        help="Which manifest sources to be updated.\n\nexample\n  --source cloud_provider\n\n",
    )
    return parser


class UpdateError(Exception):
    """Represents an error performing the update."""


if __name__ == "__main__":
    try:
        args = get_argparser().parse_args()
        registry = Registry(args.registry, args.user_pass)
        image_set = set()
        for source in args.sources:
            version, source_images = main(source, registry, args.check, args.debug)
            Path(FILEDIR, source, "version").write_text(f"{version}\n")
            print(f"source: {source} latest={version}")
            image_set |= source_images
        print("images:")
        for image in sorted(image_set):
            print(image)
    except UpdateError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
