# Contributing to Cruise Price Optimization

Thank you for considering contributing! This project is open source and we welcome pull requests from everyone.

## Project Structure

This is a **monorepo** containing two projects:

```
extension/   → Chrome Extension (JavaScript)
platform/    → Python backend system (Playwright + FastAPI)
```

## How to Contribute

### 1. Fork & Clone

```bash
git clone https://github.com/YOUR_USERNAME/Cruise-Price-Optimization-Extension.git
cd Cruise-Price-Optimization-Extension
```

### 2. Pick Your Area

- **Extension bugs/features** → work in `extension/`
- **Backend/scraper/API** → work in `platform/`
- **Documentation** → root files or either project's README

### 3. Set Up Your Environment

**For the Chrome extension:**
- Open `chrome://extensions` → Enable Developer Mode → Load Unpacked → select `extension/`

**For the Python platform:**
```bash
cd platform
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
python main.py api
```

### 4. Make Your Changes

- Create a feature branch: `git checkout -b feature/my-improvement`
- Write clean, readable code
- Test your changes
- Commit with clear messages

### 5. Submit a Pull Request

- Push to your fork
- Open a PR against `main`
- Describe what you changed and why

## Guidelines

- **Keep it simple** — clear code over clever code
- **Don't break existing features** — test before submitting
- **Document your changes** — update README if needed
- **One PR, one feature** — don't bundle unrelated changes

## Adding New Cruise Lines

Want to add support for a new cruise line? Here's how:

1. **Extension:** Create `adapter_newline.js` following the pattern in `adapter_espresso.js`
2. **Platform:** Create `platform/scraper/newline.py` extending `BaseScraper`
3. **Calculator:** Add a `calculate_newline()` function in `platform/core/calculator.py`
4. Update both READMEs

**Before writing a single selector, verify it against a real captured page — never ship a selector "confirmed" only via a DevTools Recorder export or a one-off manual click-through.** This project's GoCCL build shipped selectors that were confirmed exactly that way, and every one of them turned out wrong once tested against a real live booking. DevTools recordings and manual walkthroughs miss things a real automated run hits immediately: elements that only exist after an async render, data-attributes that look stable but are React/Angular-generated per-session, and text that's actually inside an iframe or shadow root. Capture a real page (`dump_page_snapshot`-style: full HTML + any embedded JSON like `window.initialData`/`window.__preloaded_data`, not just a screenshot) and grep the raw markup for the attribute you intend to depend on before writing it into a selector. The ESPRESSO adapter's own `_SEARCH_INPUT_SELECTOR` fallback chain (see [Bug History item 1](DOCUMENTATION.md#bug-history--lessons-learned) in `DOCUMENTATION.md`) is the pattern to follow: keep the old selector as a fallback alongside the new one rather than replacing it outright, so a portal redesign degrades gracefully instead of breaking outright.

## Reporting Bugs

Open an issue with:
- What you expected to happen
- What actually happened
- Steps to reproduce
- Which project (extension or platform)

## Code of Conduct

Be respectful, constructive, and collaborative. We're all here to build something useful.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
