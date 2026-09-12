title: Name of the set you are building
set: 42176
status: building
progress: 10
started: September 2026
instagram: https://www.instagram.com/p/PASTE-THE-POST-LINK/
photos:
  photo-one.jpg
  photo-two.jpg
---
Write whatever you want here. Blank lines separate paragraphs.

This file is ignored because its name starts with an underscore. To make a real
post, copy it to something like `2026-09-g-wagon.md` — a date at the front keeps
the build log in order.

Notes on the settings above:

status: must be `building`, `planning` or `complete`. Anything else is treated
as `building` and the site still builds.

progress: a number from 0 to 100. Delete the line if you don't want a bar.

photos: indent each one. A bare filename like `photo-one.jpg` means a file you
uploaded to `content/media/`. A full https:// link is used as-is.

set: the LEGO set number. If it is one we track, the post automatically links to
its price page.
