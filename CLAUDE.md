# Project guidance for Claude

## Workflow

- After completing a set of changes, **always commit and open a pull request** —
  don't leave work only committed/pushed without a PR. This is the default for
  every task unless the user says otherwise.
- Develop on the feature branch assigned for the session; never push directly to
  `main`.
- This is a static HTML site (GitHub Pages, served from `main`). There is no
  build step — pages are plain `.html` files with inline `<style>`/`<script>`.

## Shared header

Most content pages share the same top navigation (`.topnav` / `.topnav-links`).
When changing nav links or header behavior, update all pages that contain
`<div class="topnav-links">` so the header stays consistent. `index.html` (the
map/home page) uses a different banner design and does not share this header.
