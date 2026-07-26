# Bundled font assets

Comic Sol bundles Comic Neue Regular and Bold for comic display lettering and
retains Noto Sans Regular as the Unicode fallback face.

## Comic Neue Regular

- Family and face: Comic Neue Regular, static TrueType font, internal version 2.003
- Upstream project: `google/fonts`
- Exact immutable upstream raw URL: https://raw.githubusercontent.com/google/fonts/389b770410cc0b7c21c85673bfa2077420fe7f65/ofl/comicneue/ComicNeue-Regular.ttf
- Upstream revision/version: `google/fonts` repository commit `389b770410cc0b7c21c85673bfa2077420fe7f65`, committed 2026-07-16; font version 2.003
- Upstream file Git object: `88e9417f0e247c141adbcaa2464107d8d5b55aa1`
- Local SHA-256: `a0ee5a37c8b27c4db0700137d928598b1e23b0089e1546a8961909176b779360`
- License notice: SIL Open Font License 1.1
- Exact immutable upstream license: https://raw.githubusercontent.com/google/fonts/389b770410cc0b7c21c85673bfa2077420fe7f65/ofl/comicneue/OFL.txt

## Comic Neue Bold

- Family and face: Comic Neue Bold, static TrueType font, internal version 2.003
- Upstream project: `google/fonts`
- Exact immutable upstream raw URL: https://raw.githubusercontent.com/google/fonts/389b770410cc0b7c21c85673bfa2077420fe7f65/ofl/comicneue/ComicNeue-Bold.ttf
- Upstream revision/version: `google/fonts` repository commit `389b770410cc0b7c21c85673bfa2077420fe7f65`, committed 2026-07-16; font version 2.003
- Upstream file Git object: `378eb2004a5bdb5ff06e97fc709ec4f1fc205d80`
- Local SHA-256: `3e7e5fccfd7e0788f317b43312151c1bd5cf058c9697a8d83eac3939050bd61e`
- License notice: SIL Open Font License 1.1
- Exact immutable upstream license: https://raw.githubusercontent.com/google/fonts/389b770410cc0b7c21c85673bfa2077420fe7f65/ofl/comicneue/OFL.txt

## Noto Sans Regular fallback

Comic Sol continues to bundle `fonts/NotoSans-Regular.ttf` for deterministic
fallback lettering.

- Family and face: Noto Sans Regular, static hinted TrueType font
- Upstream project: `notofonts/noto-fonts`
- Upstream source: https://github.com/notofonts/noto-fonts/blob/ffebf8c1ee449e544955a7e813c54f9b73848eac/hinted/ttf/NotoSans/NotoSans-Regular.ttf
- Upstream revision/version: repository commit `ffebf8c1ee449e544955a7e813c54f9b73848eac`, committed 2023-01-25
- Upstream file Git object: `d55220958aa51eeeb85048d746eabe43d2cd9f14`
- Local SHA-256: `b85c38ecea8a7cfb39c24e395a4007474fa5a4fc864f6ee33309eb4948d232d5`
- License: SIL Open Font License 1.1
- Upstream license: https://github.com/notofonts/noto-fonts/blob/ffebf8c1ee449e544955a7e813c54f9b73848eac/LICENSE

The Noto Sans face covers the must-have Latin, Greek, and Cyrillic fallback scope.
Comic Sol does not claim CJK coverage from this file. All bundled font faces remain
subject to the SIL Open Font License 1.1. The repository's project license does not replace the font license
for any bundled face.

OFL 1.1 requires the license to travel with the fonts, so its full text is vendored
beside them: [`OFL-ComicNeue.txt`](fonts/OFL-ComicNeue.txt) covers Comic Neue Regular and
Bold, and [`OFL-NotoSans.txt`](fonts/OFL-NotoSans.txt) covers Noto Sans Regular. Each
file is the verbatim upstream license, including that project's copyright notice.

Comic Sol's MIT License applies to the project's original code and documentation
only. It does not relicense, supersede, or replace the font license or its upstream
copyright notices.
