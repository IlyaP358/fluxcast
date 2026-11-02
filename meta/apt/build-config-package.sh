#!/bin/bash
# Build fluxcast-repo.deb, the one file a user downloads. It carries the public
# key and a postinst that writes the sources file for the matching suite.
set -euo pipefail

KEYRING=meta/apt/fluxcast-archive-keyring.gpg
if [ ! -s "$KEYRING" ]; then
    echo "$KEYRING is missing or empty; export the public key into it first" >&2
    exit 1
fi

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT

cp -r meta/apt/fluxcast-repo/. "$stage/"
mkdir -p "$stage/usr/share/keyrings"
cp "$KEYRING" "$stage/usr/share/keyrings/fluxcast-archive-keyring.gpg"
chmod 755 "$stage/DEBIAN/postinst" "$stage/DEBIAN/postrm"

mkdir -p repo
dpkg-deb --build --root-owner-group "$stage" repo/fluxcast-repo.deb

# Also publish the bare key, for anyone who would rather add it by hand.
cp "$KEYRING" repo/fluxcast-archive-keyring.gpg

dpkg-deb -I repo/fluxcast-repo.deb | sed -n '1,12p'
