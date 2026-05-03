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
