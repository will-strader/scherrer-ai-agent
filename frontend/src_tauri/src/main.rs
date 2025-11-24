// frontend/src_tauri/src/main.rs
#![cfg_attr(all(not(debug_assertions), target_os = "windows"), windows_subsystem = "windows")]

use std::{
    net::{TcpListener},
    path::{PathBuf},
    process::{Child, Command, Stdio},
    sync::{Arc, Mutex},
    time::Duration,
};

use tauri::{Manager, AppHandle, State, Wry};
use tokio::time;

#[derive(Default)]
struct BackendState {
    child: Mutex<Option<Child>>,
    port:  Mutex<Option<u16>>,
}

/// Cheap way to grab an available local port.
fn find_free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .expect("failed to bind ephemeral port")
        .local_addr()
        .unwrap()
        .port()
}

/// Resolve the app's resource directory and return the path to the backend exe and resource dir.
fn resolve_backend_paths(app: &AppHandle<Wry>) -> anyhow::Result<(PathBuf, PathBuf)> {
    // Tauri bundles files listed in tauri.conf.json "bundle.resources".
    // `tauri::api::path::resource_dir` points to that directory at runtime.
    let res_dir = tauri::api::path::resource_dir(app.package_info(), &app.config())
        .ok_or_else(|| anyhow::anyhow!("resource_dir not found"))?;

    #[cfg(target_os = "windows")]
    let exe_name = "scherrer-bid-backend.exe";
    #[cfg(not(target_os = "windows"))]
    let exe_name = "scherrer-bid-backend"; // in case you test on macOS

    let exe_path = res_dir.join("resources").join(exe_name);
    if !exe_path.exists() {
        return Err(anyhow::anyhow!("backend executable not found at {:?}", exe_path));
    }
    Ok((exe_path, res_dir.join("resources")))
}

/// Try to load a .env file from resources if present (no-op if missing).
fn try_load_env(dotenv_dir: &PathBuf) {
    let dotenv_path = dotenv_dir.join(".env");
    if dotenv_path.exists() {
        println!("🔧 Loading .env from {:?}", dotenv_path);
        let _ = dotenvy::from_path(&dotenv_path);
    } else {
        println!("No .env found at {:?}", dotenv_path);
    }
}

/// Spawn the backend; returns the chosen port and child handle.
fn spawn_backend(exe: &PathBuf, resources_dir: &PathBuf, port: u16) -> anyhow::Result<Child> {
    // Ensure .env (if any) is loaded into the environment first.
    try_load_env(resources_dir);

    // Inherit current environment, override PORT, and set working dir to resources (so relative paths work).
    let mut cmd = Command::new(exe);
    cmd.current_dir(resources_dir)
        .env("PORT", port.to_string());
    // In debug builds, inherit stdout/stderr so we can see backend logs in the Tauri console.
    // In release builds, silence the console.
    #[cfg(debug_assertions)]
    {
        cmd.stdout(Stdio::inherit()).stderr(Stdio::inherit());
    }
    #[cfg(not(debug_assertions))]
    {
        cmd.stdout(Stdio::null()).stderr(Stdio::null());
    }

    #[cfg(target_os = "windows")]
    {
        // Avoid popping a console window.
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x08000000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }

    let child = cmd.spawn()?;
    Ok(child)
}

#[tauri::command]
fn backend_info(state: State<BackendState>) -> Option<u16> {
    match state.port.lock() {
        Ok(guard) => *guard,
        Err(_) => None,
    }
}

fn main() {
    tauri::Builder::default()
        .manage(BackendState::default())
        .setup(|app| {
            let app_handle = app.handle();
            let state = app_handle.state::<BackendState>().clone();

            // Start backend on a background task.
            tauri::async_runtime::spawn(async move {
                // Small delay to let the Tauri window come up.
                time::sleep(Duration::from_millis(100)).await;

                println!("Resolving backend paths...");
                let (exe_path, resources_dir) = match resolve_backend_paths(&app_handle) {
                    Ok(p) => {
                        println!("Resolved backend: exe = {:?}, resources = {:?}", p.0, p.1);
                        p
                    },
                    Err(e) => {
                        println!("❌ Resolve error: {e}");
                        let _ = app_handle.emit_all("backend:error", format!("Resolve error: {e}"));
                        return;
                    }
                };

                let port = find_free_port();
                println!("Selected free port: {}", port);

                match spawn_backend(&exe_path, &resources_dir, port) {
                    Ok(child) => {
                        // Save child + port in state
                        if let Ok(mut p) = state.port.lock() {
                            *p = Some(port);
                        }
                        if let Ok(mut c) = state.child.lock() {
                            *c = Some(child);
                        }

                        println!("Backend launched on port {}", port);

                        // Give the server a brief moment to bind the port, then notify UI
                        time::sleep(Duration::from_millis(600)).await;
                        let _ = app_handle.emit_all("backend:ready", serde_json::json!({ "port": port }));
                    }
                    Err(e) => {
                        println!("Spawn error: {e}");
                        let _ = app_handle.emit_all("backend:error", format!("Spawn error: {e}"));
                    }
                }
            });

            Ok(())
        })
        .on_window_event(|event| {
            // If the user closes the last window, gracefully kill backend
            if let tauri::WindowEvent::CloseRequested { .. } = event.event() {
                let app = event.window().app_handle();
                println!("Window closing: stopping backend...");
                stop_backend(&app);
            }
        })
        .on_page_load(|window, _| {
            // If the page reloads, frontend can ask for backend_info() or wait for 'backend:ready'
            let _ = window.emit("backend:starting", ());
        })
        .invoke_handler(tauri::generate_handler![backend_info])
        .build(tauri::generate_context!())
        .expect("error while running tauri application")
        .run(|app_handle, e| {
            // Ensure backend gets killed on any global exit path.
            if let tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit = e {
                println!("App exiting: stopping backend...");
                stop_backend(&app_handle);
            }
        });
}

fn stop_backend(app: &AppHandle<Wry>) {
    if let Some(state) = app.try_state::<BackendState>() {
        if let Ok(mut lock) = state.child.lock() {
            if let Some(child) = lock.as_mut() {
                println!("🔻 Killing backend process (pid: {})", child.id());
                #[allow(unused_must_use)]
                { child.kill(); }
            } else {
                println!("No backend child to kill.");
            }
            *lock = None;
        } else {
            println!("⚠️ Could not lock BackendState.child to stop backend.");
        }
    } else {
        println!("BackendState not available; nothing to stop.");
    }
}