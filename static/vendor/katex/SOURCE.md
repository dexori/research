# KaTeX browser assets

Vendored from the official npm `katex` package, version **0.19.0**.

- Source: https://registry.npmjs.org/katex/-/katex-0.19.0.tgz
- Upstream: https://github.com/KaTeX/KaTeX
- License: MIT, included as `LICENSE`.
- npm integrity: `sha512-v6Tznz3zJ7u3niRCoDTsumM2+HA2XXcCu+WAacCeHD2z3p9A9Ks987o5FzfTGBN0e8A0vjEgIDvLbICrXpdw/Q==`

Only `dist/katex.min.js`, `dist/katex.min.css`, and the referenced font files are included.
All are served locally. Research content is not sent to a CDN to render formulas.
Math rendering disables trusted commands, bounds macro expansion and sizing, and falls back to original TeX on errors.
