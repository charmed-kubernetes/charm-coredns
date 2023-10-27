#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
"""Update to a new upstream release."""
import argparse
import json
import logging
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Iterable, List, Optional, Set, TypedDict

import yaml
from semver import VersionInfo

log = logging.getLogger("CoreDNS Image Update")
logging.basicConfig(level=logging.INFO)
DH_REPO = "https://hub.docker.com/v2/repositories/coredns/coredns/tags"
DH_IMAGE = "docker.io/coredns/coredns:{tag}"
ROCKS_CC = "upload.rocks.canonical.com:5000/cdk"


@dataclass(frozen=True)
class Registry:
    """Object to define how to contact a Registry."""

    base: str
    user_pass: Optional[str] = None

    @property
    def name(self) -> str:
        name, *_ = self.base.split("/")
        return name

    @property
    def path(self) -> List[str]:
        _, *path = self.base.split("/")
        return path

    @property
    def user(self) -> str:
        user, _ = self.user_pass.split(":", 1)
        return user

    @property
    def password(self) -> str:
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


SyncAsset = TypedDict("SyncAsset", {"source": str, "target": str, "type": str})
SyncCreds = TypedDict("SyncCreds", {"registry": str, "user": str, "pass": str})


class SyncConfig(TypedDict):
    """Type definition for building sync config."""

    version: int
    creds: List[SyncCreds]
    sync: List[SyncAsset]


def sync_asset(image: str, registry: Registry):
    """Factory for generating SyncAssets."""
    _, *name_tag = image.split("/")
    full_path = "/".join(registry.path + name_tag)
    dest = f"{registry.name}/{full_path}"
    return SyncAsset(source=image, target=dest, type="image")


def gather_releases() -> Set[str]:
    """Fetch from github the release manifests by version."""
    images = set()
    with urllib.request.urlopen(DH_REPO) as resp:
        for item in json.load(resp)["results"]:
            try:
                VersionInfo.parse(item["name"])
            except ValueError:
                continue
            images.add(item["name"])
    return images


def mirror_image(images: Iterable[str], registry: Registry, check: bool, debug: bool):
    """Synchronize all source images to target registry, only pushing changed layers."""
    sync_config = SyncConfig(
        version=1,
        creds=registry.creds,
        sync=[sync_asset(DH_IMAGE.format(tag=image), registry) for image in images],
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
                log.info(line.strip())
            proc.poll()


def update_metadata(latest):
    meta = Path(__file__).parent / ".." / "metadata.yaml"
    loaded = yaml.safe_load(meta.read_text())
    current = loaded["resources"]["coredns-image"]["upstream-source"]
    base, tag = current.rsplit(":", 1)
    if replacement := (f"{base}:{latest}" if tag != latest else None):
        replaced = meta.read_text().replace(current, replacement)
        meta.write_text(replaced)
        log.info(
            f'Updated to {loaded["resources"]["coredns-image"]["upstream-source"]}'
        )


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
        help="If selected, will not run the sync\n"
        "but instead checks if a sync is necessary",
    )
    parser.add_argument(
        "--debug", action="store_true", help="If selected, regsync debug will appear"
    )
    return parser


if __name__ == "__main__":
    args = get_argparser().parse_args()
    all_images = gather_releases()
    sorted_images = sorted(all_images, key=VersionInfo.parse, reverse=True)
    update_metadata(sorted_images[0])
    mirror_image(
        all_images, Registry(args.registry, args.user_pass), args.check, args.debug
    )
