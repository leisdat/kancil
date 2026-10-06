#!/usr/bin/env bash
# Synthesize VO segments for Kancil launch video v2. Spoken-form English.
set -e
OUT=work/vo
mkdir -p $OUT
speak() { # $1=name $2=text
  /opt/hatch/bin/tts speak --voice avocado_v2:MAI_03 --language en --format mp3 \
    --output $OUT/$1.mp3 --text-stdin <<< "$2" 2>&1 | tail -1
}
speak s1a "Browsers were built for humans."
speak s1b "Agents needed one too."
speak s2  "Meet Kancil, the pocket-sized agent browser."
speak s3  "One hundred twenty-two tool actions. JSON in, JSON out."
speak s4  "Loops, sessions, stealth. Built for agents."
speak s5  "It runs where agents live: Termux, Android, Linux, macOS."
speak s6  "Small but clever. Find it on GitHub."
echo "--- durations ---"
for f in $OUT/*.mp3; do
  d=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$f")
  echo "$f $d"
done
