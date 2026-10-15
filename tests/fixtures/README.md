`voice-aac.m4a` is a synthetic 100 ms silent recording, with AAC audio in an
MPEG-4 container (16 kHz, mono). It contains no recorded speech. Generate with:

```sh
ffmpeg -f lavfi -i anullsrc=r=16000:cl=mono -t 0.1 -c:a aac -b:a 32k -map_metadata -1 -movflags +faststart voice-aac.m4a
```

The native transport regression uploads these exact bytes as `audio/mp4` and
checks that Hermes receives an AUDIO event with the same `.m4a` file contents.
