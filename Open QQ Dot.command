#!/bin/sh
cd "$(CDPATH= cd "$(dirname "$0")" && pwd)"
exec /bin/sh ./start.sh "$@"
