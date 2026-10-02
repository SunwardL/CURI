import { spawnSync } from "node:child_process";
import { mkdirSync, copyFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const desktopDir = resolve(scriptDir, "..");
const repoDir = resolve(desktopDir, "..");
const tripleByPlatform = {
  "win32-x64": "x86_64-pc-windows-msvc",
  "win32-arm64": "aarch64-pc-windows-msvc",
  "darwin-x64": "x86_64-apple-darwin",
  "darwin-arm64": "aarch64-apple-darwin",
};
const target = tripleByPlatform[`${process.platform}-${process.arch}`];

if (!target) {
  throw new Error(`Unsupported sidecar target: ${process.platform}-${process.arch}`);
}

const extension = process.platform === "win32" ? ".exe" : "";
const buildDir = join(desktopDir, ".build", target);
const distDir = join(buildDir, "dist");
const binaryDir = join(desktopDir, "src-tauri", "binaries");
const binaryName = `curi-backend-${target}${extension}`;
const python = process.env.CURI_PYTHON || (process.platform === "win32" ? "python" : "python3");
const result = spawnSync(
  python,
  [
    "-m",
    "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onefile",
    "--name",
    "curi-backend",
    "--paths",
    repoDir,
    "--distpath",
    distDir,
    "--workpath",
    join(buildDir, "work"),
    "--specpath",
    buildDir,
    join(repoDir, "curi.py"),
  ],
  { cwd: repoDir, stdio: "inherit" },
);

if (result.error) throw result.error;
if (result.status !== 0) process.exit(result.status ?? 1);

mkdirSync(binaryDir, { recursive: true });
copyFileSync(join(distDir, `curi-backend${extension}`), join(binaryDir, binaryName));
process.stdout.write(`Built ${join(binaryDir, binaryName)}\n`);
