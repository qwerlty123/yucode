#!/bin/sh
marker=$1
shift
temp=/tmp/yucode-temp
echo "$temp" > "$marker"
"$@" || true
