#!/bin/bash
# Build and sign the apt repository tree in ./repo from the .deb files in
# ./incoming. Whatever is already under repo/pool is kept, so older versions
# stay installable.
#
# The suite comes from the Version field, not the filename: GitHub rewrites the
# tilde in fluxcast_0.2.9~bookworm into a dot when it stores the asset.
set -euo pipefail

: "${KEY_ID:?APT_GPG_KEY_ID is not set}"

SUITES="bookworm trixie noble resolute"

shopt -s nullglob
debs=(incoming/*.deb)
if [ ${#debs[@]} -eq 0 ]; then
    echo "no .deb files in incoming/" >&2
    exit 1
fi

for f in "${debs[@]}"; do
    suite=$(dpkg-deb -f "$f" Version | sed -E 's/^.*~//')
    case " $SUITES " in
        *" $suite "*) ;;
        *) echo "$f carries unknown suite '$suite'" >&2; exit 1 ;;
    esac
    mkdir -p "repo/pool/$suite/main/f/fluxcast"
    cp -f "$f" "repo/pool/$suite/main/f/fluxcast/"
done

cd repo

for suite in $SUITES; do
    if [ ! -d "pool/$suite" ]; then
        echo "no packages for $suite" >&2
        exit 1
    fi
    mkdir -p "dists/$suite/main/binary-amd64"

    # Remove the old signatures first: apt-ftparchive hashes every file under
    # the dist directory, and a leftover Release.gpg would end up inside the
    # Release it is supposed to sign.
    rm -f "dists/$suite/Release" "dists/$suite/InRelease" "dists/$suite/Release.gpg"

    apt-ftparchive --arch amd64 packages "pool/$suite" \
        > "dists/$suite/main/binary-amd64/Packages"
    gzip -9kf "dists/$suite/main/binary-amd64/Packages"

    apt-ftparchive \
        -o APT::FTPArchive::Release::Origin=FluxCast \
        -o APT::FTPArchive::Release::Label=FluxCast \
        -o APT::FTPArchive::Release::Suite="$suite" \
        -o APT::FTPArchive::Release::Codename="$suite" \
        -o APT::FTPArchive::Release::Architectures=amd64 \
        -o APT::FTPArchive::Release::Components=main \
        release "dists/$suite" > "dists/$suite/Release"

    gpg --batch --yes --pinentry-mode loopback --local-user "$KEY_ID" \
        --armor --detach-sign -o "dists/$suite/Release.gpg" "dists/$suite/Release"
    gpg --batch --yes --pinentry-mode loopback --local-user "$KEY_ID" \
        --clearsign -o "dists/$suite/InRelease" "dists/$suite/Release"

    echo "$suite: $(grep -c '^Package:' "dists/$suite/main/binary-amd64/Packages") package(s)"
done
