#!/usr/bin/env bash

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "${DIR}/python"

broadwayd --port=8080 --address=0.0.0.0 > /dev/null 2>&1 &
GTK_THEME=Adwaita:light GDK_BACKEND=broadway python control-ui.py
