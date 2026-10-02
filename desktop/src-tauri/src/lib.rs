#[cfg(debug_assertions)]
use std::path::PathBuf;
use std::sync::Mutex;

use tauri::{Manager, RunEvent, State};
use tauri_plugin_shell::process::{Command, CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

#[derive(Default)]
struct BackendState {
    port: Mutex<Option<u16>>,
    child: Mutex<Option<CommandChild>>,
}

#[tauri::command]
fn backend_port(state: State<'_, BackendState>) -> Option<u16> {
    *state.port.lock().expect("backend port lock poisoned")
}

fn backend_command(app: &tauri::AppHandle) -> Result<Command, Box<dyn std::error::Error>> {
    #[cfg(debug_assertions)]
    {
        let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let repo_dir = manifest_dir
            .ancestors()
            .nth(2)
            .ok_or("could not locate repository root")?
            .to_path_buf();
        let script = repo_dir.join("curi.py").to_string_lossy().into_owned();
        let python = if cfg!(windows) { "python" } else { "python3" };
        Ok(app
            .shell()
            .command(python)
            .args([script, "serve".into(), "--port".into(), "0".into()]))
    }

    #[cfg(not(debug_assertions))]
    {
        Ok(app
            .shell()
            .sidecar("curi-backend")?
            .args(["serve", "--port", "0"]))
    }
}

fn announced_port(line: &str) -> Option<u16> {
    line.split_once("http://127.0.0.1:")?
        .1
        .split_whitespace()
        .next()?
        .parse()
        .ok()
}

pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(BackendState::default())
        .setup(|app| {
            let (mut events, child) = backend_command(app.handle())?.spawn()?;
            app.state::<BackendState>()
                .child
                .lock()
                .expect("backend child lock poisoned")
                .replace(child);

            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                while let Some(event) = events.recv().await {
                    match event {
                        CommandEvent::Stdout(line) => {
                            let line = String::from_utf8_lossy(&line);
                            if let Some(port) = announced_port(&line) {
                                if let Some(state) = handle.try_state::<BackendState>() {
                                    *state.port.lock().expect("backend port lock poisoned") =
                                        Some(port);
                                }
                            }
                        }
                        CommandEvent::Stderr(line) => {
                            eprintln!("CURI backend: {}", String::from_utf8_lossy(&line));
                        }
                        CommandEvent::Terminated(_) => {
                            if let Some(state) = handle.try_state::<BackendState>() {
                                *state.port.lock().expect("backend port lock poisoned") = None;
                            }
                        }
                        _ => {}
                    }
                }
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![backend_port])
        .build(tauri::generate_context!())
        .expect("failed to build CURI desktop app");

    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            if let Some(state) = handle.try_state::<BackendState>() {
                if let Some(mut child) = state
                    .child
                    .lock()
                    .expect("backend child lock poisoned")
                    .take()
                {
                    let _ = child.kill();
                }
            }
        }
    });
}
