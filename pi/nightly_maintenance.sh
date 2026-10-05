#!/bin/bash
# BirdThing nightly maintenance: clear broken/empty cached bird images so thumbnails re-fetch fresh,
# trim old stream recordings, and free RAM — so each morning starts clean and quick.
CACHE=/opt/birdthing/imgcache
LOG=/tmp/birdthing-maint.log
# delete failed/corrupt cached images (tiny or zero-byte) so the API re-fetches them clean
n=$(find "$CACHE" -type f -name '*.jpg' -size -2k 2>/dev/null | wc -l)
find "$CACHE" -type f -name '*.jpg' -size -2k -delete 2>/dev/null
# cap StreamData on the small SD card (BirdNET only needs the most recent clips)
find /home/birdpi/BirdSongs/StreamData -name '*.wav' -mmin +180 -delete 2>/dev/null
# free page cache / reclaim RAM on the small Pi
sync; echo 3 > /proc/sys/vm/drop_caches 2>/dev/null
echo "$(date '+%F %T') maintenance done (removed $n broken images)" >> "$LOG"
tail -n 100 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG" 2>/dev/null
