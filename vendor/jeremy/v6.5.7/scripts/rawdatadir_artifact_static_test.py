#!/usr/bin/env python3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "ops" / "build-rawdatadir-artifact.sh"
FETCH = ROOT / "ops" / "fetch-rawdatadir-artifact.sh"
SIDECAR = ROOT / "ops" / "maintain-rawdatadir-sidecar.sh"
ELIGIBILITY = ROOT / "ops" / "fastartifact_source_eligibility.py"
PUBLISH = ROOT / "ops" / "publish-rawdatadir-artifact.sh"
INSTALL = ROOT / "ops" / "install-p2p-services.sh"
IPFS = ROOT / "ops" / "ipfs_content_sidecar.py"
SEAL = ROOT / "ops" / "seal_rawdatadir_sidecar_content.py"
DOC = ROOT / "docs" / "rawdatadir-libp2p-sync.md"
IPFS_DOC = ROOT / "docs" / "ipfs-content-sidecar.html"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def assert_contains(text: str, needle: str, path: Path) -> None:
    if needle not in text:
        raise AssertionError(f"{path} missing {needle!r}")


def main() -> None:
    build = read(BUILD)
    fetch = read(FETCH)
    sidecar = read(SIDECAR)
    eligibility = read(ELIGIBILITY)
    publish = read(PUBLISH)
    install = read(INSTALL)
    ipfs = read(IPFS)
    seal = read(SEAL)
    doc = read(DOC)
    ipfs_doc = read(IPFS_DOC)

    for needle in (
        "raw_datadir_checkpoint",
        "BDAG_RAWDATADIR_SOURCE_DIR is required",
        "wait_db_lock_free",
        "BDAG_RAWDATADIR_REQUIRE_SIGNED",
        "--exclude=./network.key*",
        "--exclude=./bdageth/nodekey*",
        "--exclude=./keystore*",
        "--exclude=./bdageth/nodes*",
        "--exclude=./peerstore*",
    ):
        assert_contains(build, needle, BUILD)

    for needle in (
        "--artifact-type",
        "raw_datadir_checkpoint",
        "--dir-out",
        "--legacy-fallback=false",
        "BDAG_RAWDATADIR_IMPORT_REPLACE",
        "before-rawdatadir",
        "preserved local identity path",
    ):
        assert_contains(fetch, needle, FETCH)

    for needle in (
        "rsync",
        "--delete-excluded",
        "--one-file-system",
        "--delay-updates",
        "--exclude=/network.key*",
        "--exclude=/bdageth/nodekey*",
        "--exclude=/bdageth/nodes*",
        "--exclude=/peerstore*",
        "seal_rawdatadir_sidecar_content.py",
        "BDAG_RAWDATADIR_SIDECAR_CONTENT_MODE",
        "BDAG_NODE_SERVICES:-node",
    ):
        assert_contains(sidecar, needle, SIDECAR)

    for needle in (
        "usb_or_removable",
        "BDAG_RAWDATADIR_MIN_FREE_GIB",
        "publish_requires_finalization",
        "docker_root",
    ):
        assert_contains(eligibility, needle, ELIGIBILITY)

    for needle in (
        "BDAG_RAWDATADIR_FINALIZE=1",
        "raw datadir artifact publish requires",
        "BDAG_RAWDATADIR_SOURCE_DIR",
        "BDAG_NODE_SERVICES:-node",
        "background maintenance backoff active",
    ):
        assert_contains(publish, needle, PUBLISH)

    for needle in (
        "publish_allowed",
        "BDAG_FASTSYNC_ARTIFACT_DIRECTORY \"\"",
        "bdag-rawdatadir-source.timer",
        "Raw datadir artifact publisher is not allowed",
        "install_ipfs_content_sidecar_timer",
        "bdag-ipfs-content-sidecar.timer",
        "BDAG_RAWDATADIR_SIDECAR_CONTENT_BASE",
        "BDAG_IPFS_CONTENT_ARTIFACT_DIR",
    ):
        assert_contains(install, needle, INSTALL)

    for needle in (
        "content-addressed chunks",
        "BDAG_RAWDATADIR_SIDECAR_CONTENT_MODE",
        "artifact_root",
        "DO_NOT_PUBLISH",
        "ed25519",
        "compressed_sha256",
    ):
        assert_contains(seal, needle, SEAL)

    for needle in (
        "ipfs_is_untrusted_transport_manifest_and_consensus_are_authoritative",
        "BDAG_IPFS_CONTENT_SIDECAR_MODE",
        "BDAG_RAWDATADIR_SIDECAR_CONTENT_BASE",
        "DO_NOT_PUBLISH",
        "manifest_unsigned",
        "background_maintenance_decision",
        "ipfs add",
    ):
        assert_contains(ipfs, needle, IPFS)

    for needle in (
        "Use the existing Fast Artifact Sync V2 libp2p protocol",
        "Trust signer public keys, not peer IDs",
        "immutable SHA-256 chunks",
        "No deltas",
    ):
        assert_contains(doc, needle, DOC)

    for needle in (
        "IPFS is untrusted byte transport",
        "signed FastArtifact manifests",
        "signed manifest roots",
        "background_maintenance_decision",
    ):
        assert_contains(ipfs_doc, needle, IPFS_DOC)


if __name__ == "__main__":
    main()
