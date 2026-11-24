# Native raster notices

This software is based in part on the work of the Independent JPEG Group.

This software is based in part on the work of the FreeType Team.

The native raster backend links libjpeg-turbo and FreeType and includes
polygon and ellipse routines from Pillow. The Windows build links zlib-ng.
The Linux build uses system zlib and includes HarfBuzz, Raqm, and Pillow's
FriBiDi loader. The dependency lock records the source releases and hashes.
Handoff ZIP export includes prefixed zlib 1.3.1 compression routines. See
zlib's LICENSE for its copyright and redistribution terms.
