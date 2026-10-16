# Demo footage source

The demo GIF and stills in this folder are frames from clips that Clip Machine rendered from one
public NASA video. NASA material is not copyrighted in the United States
(https://www.nasa.gov/nasa-brand-center/images-and-media/); NASA does not endorse this project.

- **Video:** "Astronaut Matt Dominick Talks with KUSA-TV Denver" (in-flight interview from the
  International Space Station, 2024-05-31), NASA Johnson Space Center.
- **Page:** https://images.nasa.gov/details/iss071m261521523-Astronaut_Matt_Dominick_Talks_with_KUSATV_Denver_240531_2004973
- **File used:** the `~large.mp4` rendition,
  https://images-assets.nasa.gov/video/iss071m261521523-Astronaut_Matt_Dominick_Talks_with_KUSATV_Denver_240531_2004973/iss071m261521523-Astronaut_Matt_Dominick_Talks_with_KUSATV_Denver_240531_2004973~large.mp4
  (1280x720, H.264 59.94 fps, AAC, 12:41.7, 608,330,091 bytes, SHA-256
  `f6d4fdb83e40a6f1eea72cbd9c83f9df02e235462188e78761536ab73f5cc0e6`), downloaded 2026-10-15.
- **The run:** `uv run python -m clipmachine run demo-nasa.mp4 --client general` on a fresh clone,
  CPU transcription, `budget.max_usd_per_run: 0.50`. 4 candidates, 3 kept (scores 9, 8, 8), 2 API
  calls, $0.0181, 6 min 56 s wall clock.
- **Files:** `demo.gif` (clip c03, first 7 s, 12 fps), `still-before-after.jpg` (the source
  frame at 9:50 and the same moment in clip c03), `still-three-clips.jpg` (the three kept clips).
