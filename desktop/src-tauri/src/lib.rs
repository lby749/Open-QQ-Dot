use std::process::{Child, Command, Stdio};
use std::sync::Mutex;


struct BridgeProcess(Mutex<Option<Child>>);

fn python_executable(project_root: &std::path::Path) -> std::path::PathBuf {
    let venv = project_root.join(".venv").join("Scripts").join("python.exe");
    if venv.is_file() {
        venv
    } else {
        std::path::PathBuf::from("python")
    }
}

#[tauri::command]
fn project_root() -> String {
    // src-tauri lives in desktop/src-tauri; the Python core is two levels up.
    let cwd = std::env::current_dir().unwrap_or_default();
    let mut candidate = cwd.clone();
    for _ in 0..4 {
        if candidate.join("vnext_bridge.py").is_file() {
            return candidate.to_string_lossy().to_string();
        }
        if !candidate.pop() {
            break;
        }
    }
    cwd.to_string_lossy().to_string()
}

#[tauri::command]
fn start_bridge(state: tauri::State<'_, BridgeProcess>) -> Result<String, String> {
    let mut slot = state.0.lock().map_err(|_| "bridge state poisoned")?;
    if let Some(child) = slot.as_mut() {
        if child.try_wait().map_err(|e| e.to_string())?.is_none() {
            return Ok("already-running".into());
        }
    }
    let root = project_root();
    let mut command = Command::new(python_executable(std::path::Path::new(&root)));
    command
        .arg("-X")
        .arg("utf8")
        .arg("vnext_bridge.py")
        .arg("--port")
        .arg("8791")
        .current_dir(&root)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    let child = command.spawn().map_err(|e| format!("无法启动 Python Core: {e}"))?;
    *slot = Some(child);
    Ok("started".into())
}

#[tauri::command]
fn open_legacy(app: tauri::AppHandle) -> Result<String, String> {
    use tauri::Manager;
    let mut candidates: Vec<std::path::PathBuf> = Vec::new();
    if let Ok(dir) = app.path().resource_dir() {
        candidates.push(dir.join("resources").join("legacy").join("Open QQ Dot.exe"));
        candidates.push(dir.join("legacy").join("Open QQ Dot.exe"));
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            candidates.push(dir.join("resources").join("legacy").join("Open QQ Dot.exe"));
        }
    }
    let root = project_root();
    candidates.push(std::path::Path::new(&root).join("Open QQ Dot.exe"));
    candidates.push(std::path::Path::new(&root).join("OpenQQDots.exe"));
    for candidate in candidates {
        if candidate.is_file() {
            Command::new(&candidate)
                .current_dir(candidate.parent().unwrap_or(std::path::Path::new(".")))
                .spawn()
                .map_err(|e| format!("无法打开旧版控制台: {e}"))?;
            return Ok(candidate.to_string_lossy().to_string());
        }
    }
    Err("未找到旧版控制台（resources/legacy/Open QQ Dot.exe）".into())
}

#[tauri::command]
fn stop_bridge(state: tauri::State<'_, BridgeProcess>) -> Result<(), String> {
    let mut slot = state.0.lock().map_err(|_| "bridge state poisoned")?;
    if let Some(mut child) = slot.take() {
        let _ = child.kill();
        let _ = child.wait();
    }
    Ok(())
}

pub fn run() {
    tauri::Builder::default()
        .manage(BridgeProcess(Mutex::new(None)))
        .invoke_handler(tauri::generate_handler![project_root, start_bridge, stop_bridge, open_legacy])
        .run(tauri::generate_context!())
        .expect("error while running Open QQ Dot desktop");
}
