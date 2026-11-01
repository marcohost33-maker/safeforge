#!/bin/sh
# Real kernel mounts for tests/test_r41_filesystem_class.py (CI job "mounts").
#
#   experiments/mounts/mount_matrix.sh <base>        (needs sudo; Linux)
#   SAFEFORGE_MOUNT_MATRIX=<base> python -m pytest tests/test_r41_filesystem_class.py
#
# <base>/disk/outer/bind     bind mount of <base>/disk/src: SAME filesystem, so
#                            st_dev cannot tell it from outer/ (R15); mountinfo can
# <base>/tmpfs               tmpfs: every fsync "ok", nothing survives a power cut
# <base>/ovl-disk/merged     overlayfs, upper directory on the disk filesystem
# <base>/ovl-tmpfs/merged    overlayfs, upper directory on tmpfs
# <base>/ovl-volatile/merged overlayfs mounted "volatile" (drops every sync)
# <base>/ext4-barrier        loop-mounted ext4, default options (barriers on):
#                            positive control of SF-R42-03
# <base>/ext4-nobarrier      the same image type mounted "nobarrier": flushes off,
#                            so never local (SF-R42-01 negative control)
#
# Only the mount roots are handed to the invoking user (the tests run
# unprivileged); overlayfs work directories are never touched.
set -eu
base=${1:?usage: mount_matrix.sh <base>}
me="$(id -u):$(id -g)"

mkdir -p "$base/disk/src" "$base/disk/outer/bind" "$base/tmpfs"
sudo mount --bind "$base/disk/src" "$base/disk/outer/bind"
sudo mount -t tmpfs -o size=64m tmpfs "$base/tmpfs"
sudo chown "$me" "$base/tmpfs" # before the overlay below creates its upper directory in it

overlay() { # name upper-parent [extra options]
    mkdir -p "$base/$1/lower" "$base/$1/merged" "$2/$1-upper" "$2/$1-work"
    sudo mount -t overlay overlay \
        -o "lowerdir=$base/$1/lower,upperdir=$2/$1-upper,workdir=$2/$1-work${3:+,$3}" "$base/$1/merged"
}
overlay ovl-disk "$base/disk"
overlay ovl-tmpfs "$base/tmpfs"
overlay ovl-volatile "$base/disk" volatile

loop_ext4() { # name [mount options]
    truncate -s 64M "$base/$1.img"
    mkfs.ext4 -q -F "$base/$1.img"
    mkdir -p "$base/$1"
    sudo mount -o "loop${2:+,$2}" "$base/$1.img" "$base/$1"
}
loop_ext4 ext4-barrier
loop_ext4 ext4-nobarrier nobarrier

sudo chown "$me" "$base/disk/outer/bind" "$base/ovl-disk/merged" "$base/ovl-tmpfs/merged" "$base/ovl-volatile/merged" \
    "$base/ext4-barrier" "$base/ext4-nobarrier"
