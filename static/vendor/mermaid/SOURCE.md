# Mermaid Tiny

Vendored from the official npm package `@mermaid-js/tiny@12.1.0`, fetched with
`npm pack --ignore-scripts`. License: MIT (see LICENSE).

`mermaid.tiny.js` SHA-256:
`143c1b9345c45963ff429da4bdcd9051316a43522ea9edd7ccb7a87ff947a804`

Official project: https://github.com/mermaid-js/mermaid
Integration and Tiny limitations: https://mermaid.js.org/config/usage.html

The standalone IIFE includes its rendering dependencies; no runtime CDN requests.
Tiny excludes native mindmap/architecture diagram syntax, KaTeX, lazy loading and
ELK. Flowcharts can express research architectures; unsupported diagrams retain
their source. Rendering runs in a separate opaque-origin, network-disabled iframe.
The parent only displays sanitized SVG as an image, never as inline executable SVG.
