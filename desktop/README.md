# CURI Desktop

The desktop app packages a React dashboard in a Tauri 2 window and starts the existing Python scanner as a local child process. It targets Windows and macOS; the dashboard and scanner remain separate from the optional request relay.

## Development

Install Node.js, Python 3.10+, Rust, and the native build tools for your operating system. On Windows, Tauri needs the Microsoft C++ Build Tools and WebView2. On macOS, it needs Xcode or the Xcode Command Line Tools.

```bash
npm install
npm run desktop:dev
```

Tauri starts `../curi.py serve --port 0` in development and stops it when the app exits. The backend binds only to `127.0.0.1`; its selected port is passed to the frontend through a Tauri command. The API only permits the Vite and Tauri app origins.

To run the web UI in a browser instead, start `python ../curi.py serve` in another terminal, then run `npm run dev`.

## Package

Install the build-only Python dependency and create a platform-specific Python executable:

```bash
python -m pip install -r requirements-build.txt
npm run desktop:build
```

The sidecar build script uses the current OS and CPU architecture. Build Windows packages on Windows and macOS packages on macOS; macOS distribution to users may require signing and notarization. The generated sidecar and build output are ignored by Git.
