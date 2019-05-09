#!/bin/bash

# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved

set -e

# Integration test for round-tripping a file.
#
# Note:
# - On MacOS X, you need to install GNU coreutils (package `coreutils`).
# - On Ubuntu, you need to install realpath (package `realpath`).
# - On all platforms, you need to install brotli (`pip install brotli`).

prog_dir=$(dirname $(realpath $0))

tmp_dir=$(mktemp -d)
pushd $tmp_dir > /dev/null
$prog_dir/bpy.py make-dict $prog_dir/test-data/y5R7cnYctJv.js.dump my.dict
$prog_dir/bpy.py optimize-ast $prog_dir/test-data/three.min.js.dump three.dump
$prog_dir/bpy.py encode-ast my.dict three.dump three.bin
$prog_dir/bpy.py decode-ast my.dict three.bin three.out
if [[ $(diff three.dump three.out) ]]; then
    echo 'test fails, decoded files differ'
    exit 1
else
    echo 'test passed'
fi
popd > /dev/null
rm -rf $tmp_dir
