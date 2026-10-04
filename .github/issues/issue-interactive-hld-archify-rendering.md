# [BUG] Archify SVG Export Produces Crescent/Moon Clipping Mask Artifacts & Markdown Preview Limitations

## Issue Title
`[BUG] Archify static SVG export renders crescent clipping mask cutouts in Markdown viewers`

## Labels
`documentation`, `bug`, `visuals`

---

## Description
When attempting to use standalone HTML / SVG exports from **Archify** (`tt-a1i/archify`) inside the repository documentation (`README.md`):

1. **Standalone SVG Mask Artifacts**:
   - The exported static SVG uses dynamic clipping masks (`<clipPath>`, `c-mask`) intended for the interactive JavaScript canvas runtime.
   - When viewed in GitHub's markdown renderer or VS Code Markdown preview, these clipping paths fail to calculate dynamic bounds and render as prominent crescent/moon-shaped cutouts that occlude diagram nodes and connections.

2. **Interactive HTML Markdown Incompatibility**:
   - Markdown sanitizers (GitHub Flavored Markdown) strip out `<script>` and `<iframe>` elements for security reasons.
   - As a result, linking to standalone `.html` files in `README.md` redirects users to raw code or a file download rather than rendering in-place.

## Resolution
- Reverted the primary System Architecture and Request Lifecycle Sequence in `README.md` to standard, native **Mermaid diagrams** (`graph TB` and `sequenceDiagram`).
- Native Mermaid diagrams render cleanly and consistently across both GitHub and VS Code with zero external dependencies and responsive styling.

## Action Items / Future Investigation
- [ ] Monitor upstream Archify repository for static vector flattening or pure headless raster PNG generation that eliminates interactive mask clipping paths.
- [ ] If high-fidelity static assets are needed in the future, evaluate pre-rendered PNG assets using a headless browser screenshot pipeline with fixed dark mode styling.
