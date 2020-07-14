#!/usr/bin/env bash

ARG1=${1:-no-print}

PACMAN_DEPS="python-systemd python-humanize python-paho-mqtt python-gobject webkit2gtk"

if test "${ARG1}" = "print"
then
	echo "${PACMAN_DEPS}"
else
	sudo pacman -Syu --needed ${PACMAN_DEPS}
fi
