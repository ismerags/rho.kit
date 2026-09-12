# Build photos go here

Drag image files into this folder on github.com ("Add file" → "Upload files")
and reference them by bare filename in a build post:

    photos:
      chassis.jpg
      finished-front.jpg

They are copied to the published site as-is, so keep them reasonably small —
under about 500 KB each. A phone photo straight out of the camera is usually
4 MB; resize before uploading or the site gets slow on mobile data.

The whole published site has to stay under GitHub Pages' 1 GB limit, which is
thousands of photos, so this is about load time rather than space.

Set thumbnails are *not* stored here. Those are loaded directly from the
retailers' own image servers, which costs nothing and keeps ~900 product
photos out of this repository.
