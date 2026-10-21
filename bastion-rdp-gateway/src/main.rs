//! AutoOps 堡垒机 —— RDP 网关（实验分支）
//!
//! mstsc 连 4489 之后本进程就是 RDP 服务端：登录页（堡垒机账号口令）→ 选单（自己有权
//! 远程桌面的主机，↑↓/Enter/鼠标）→ 开会话记录（后端写 sessions + 审计）。
//! 画面全部自己画（font8x8 → 位图）。把目标机画面接进来是下一步（ironrdp-client）。

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::num::{NonZeroU16, NonZeroUsize};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use anyhow::{Context, Result};
use async_trait::async_trait;
use bytes::Bytes;
use font8x8::{UnicodeFonts, BASIC_FONTS};
use ironrdp_server::{
    BitmapUpdate, ConnectionHandler, DesktopSize, DisplayUpdate, KeyboardEvent, MouseEvent, PixelFormat,
    PostConnectionAction, RdpServer,
    RdpServerDisplay, RdpServerDisplayUpdates, RdpServerInputHandler, TlsIdentityCtx,
};
use serde_json::Value;

const WIDTH: u16 = 1280;
const HEIGHT: u16 = 800;
const SCALE: usize = 2;
const LINE_H: u16 = 30;
const LEFT: u16 = 60;
const MENU_TOP: u16 = 260;
const API_HOST: &str = "127.0.0.1";
const API_PORT: u16 = 5000;

/// 后端地址：`BASTION_API_BASE=http://host:port` 优先，其次 `BASTION_API_PORT`，最后默认 127.0.0.1:5000。
/// 网关和 Flask 不一定同机同端口，所以必须可配，不能写死。
fn api_addr() -> SocketAddr {
    if let Ok(base) = std::env::var("BASTION_API_BASE") {
        let hostport = base
            .trim()
            .trim_start_matches("http://")
            .trim_start_matches("https://")
            .trim_end_matches('/');
        if let Ok(addr) = hostport.parse::<SocketAddr>() {
            return addr;
        }
    }
    let port = std::env::var("BASTION_API_PORT")
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(API_PORT);
    (std::net::Ipv4Addr::LOCALHOST, port).into()
}

fn http_raw(
    method: &str,
    path: &str,
    body: Option<&str>,
    token: Option<&str>,
) -> Result<(u16, Vec<u8>), String> {
    let addr = api_addr();
    let started = std::time::Instant::now();
    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_secs(5))
        .map_err(|e| format!("连不上后端 {addr}：{e}"))?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(30)));
    let payload = body.unwrap_or("");
    let mut req = format!(
        "{method} {path} HTTP/1.1\r\nHost: {addr}\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: {}\r\n",
        payload.len()
    );
    if let Some(t) = token {
        req.push_str(&format!("Authorization: Bearer {t}\r\n"));
    }
    req.push_str("\r\n");
    req.push_str(payload);
    stream
        .write_all(req.as_bytes())
        .map_err(|e| format!("写请求失败：{e}"))?;
    let mut raw = Vec::new();
    stream
        .read_to_end(&mut raw)
        .map_err(|e| format!("读响应失败：{e}"))?;
    let split = raw
        .windows(4)
        .position(|w| w == b"\r\n\r\n")
        .ok_or_else(|| "响应里没有正文".to_string())?;
    let head = String::from_utf8_lossy(&raw[..split]).to_string();
    let status: u16 = head
        .split_whitespace()
        .nth(1)
        .and_then(|code| code.parse().ok())
        .unwrap_or(0);
    let body = raw[split + 4..].to_vec();
    tracing::debug!(
        method,
        path,
        status,
        ms = started.elapsed().as_millis() as u64,
        bytes = body.len(),
        "调用堡垒机后端"
    );
    Ok((status, body))
}

fn http_json(
    method: &str,
    path: &str,
    body: Option<&str>,
    token: Option<&str>,
) -> Result<Value, String> {
    let (status, bytes) = http_raw(method, path, body, token)?;
    let text = String::from_utf8_lossy(&bytes).to_string();
    if status != 200 {
        return Err(format!(
            "后端返回 {status}：{}",
            text.chars().take(200).collect::<String>()
        ));
    }
    serde_json::from_str::<Value>(&text).map_err(|e| format!("响应不是 JSON：{e}"))
}

fn http_bytes(method: &str, path: &str, token: Option<&str>) -> Result<(u16, Vec<u8>), String> {
    http_raw(method, path, None, token)
}
/// 字符 → Windows 虚拟键码（转发给目标机用）
fn char_to_vk(ch: char) -> u32 {
    let upper = ch.to_ascii_uppercase();
    match upper {
        'A'..='Z' => upper as u32,
        '0'..='9' => upper as u32,
        ' ' => 0x20,
        _ => 0,
    }
}

/// 扫描码 → 虚拟键码（回车/Tab/退格/Esc/方向键/修饰键这些）
fn scan_to_vk(code: u8, extended: bool) -> u32 {
    match code {
        0x01 => 0x1B, // Esc
        0x0E => 0x08, // Backspace
        0x0F => 0x09, // Tab
        0x1C => 0x0D, // Enter
        0x1D => 0x11, // Ctrl
        0x2A | 0x36 => 0x10, // Shift
        0x38 => 0x12, // Alt
        0x39 => 0x20, // Space
        0x47 => 0x24, // Home
        0x49 => 0x21, // PageUp
        0x4B => 0x25, // Left
        0x4D => 0x27, // Right
        0x4F => 0x23, // End
        0x51 => 0x22, // PageDown
        0x48 => {
            if extended {
                0x26
            } else {
                0x68
            }
        }
        0x50 => {
            if extended {
                0x28
            } else {
                0x62
            }
        }
        0x52 => 0x2D, // Insert
        0x53 => 0x2E, // Delete
        0x3B..=0x44 => 0x70 + (code - 0x3B) as u32, // F1..F10
        0x57 => 0x87, // F11
        0x58 => 0x88, // F12
        _ => 0,
    }
}

const WHITE: (u8, u8, u8) = (0xe6, 0xed, 0xf3);const DIM: (u8, u8, u8) = (0x94, 0xa3, 0xb8);
const ACCENT: (u8, u8, u8) = (0x60, 0xa5, 0xfa);
const WARN: (u8, u8, u8) = (0xfb, 0xbf, 0x24);

#[derive(Debug, Clone)]
struct HostRow {
    host_id: i64,
    name: String,
    address: String,
    port: u16,
    account_id: i64,
    account: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Screen {
    Login,
    Menu,
    /// 已经连上目标机，画面由桥不断刷进来
    Target,
}

struct State {
    screen: Screen,
    username: String,
    password: String,
    focus_password: bool,
    shift: bool,
    error: String,
    notice: String,
    token: String,
    hosts: Vec<HostRow>,
    cursor: usize,
    mouse: (u16, u16),
    busy: bool,
    /// 目标机会话
    sid: String,
    target_name: String,
    /// 待转发给目标机的输入事件
    pending_input: Vec<Value>,
    /// 键盘/鼠标事件计数（日志里折叠成汇总行）
    key_events: u64,
    mouse_events: u64,
}

impl Default for State {
    fn default() -> Self {
        Self {
            screen: Screen::Login,
            username: String::new(),
            password: String::new(),
            focus_password: false,
            shift: false,
            error: String::new(),
            notice: String::new(),
            token: String::new(),
            hosts: Vec::new(),
            cursor: 0,
            mouse: (0, 0),
            busy: false,
            sid: String::new(),
            target_name: String::new(),
            pending_input: Vec::new(),
            key_events: 0,
            mouse_events: 0,
        }
    }
}

/// 扫描码 set 1 → 字符（登录框够用）
fn scan_to_char(code: u8, shift: bool) -> Option<char> {
    let digits = "1234567890-=[];'`,./\\";
    let alpha = "qwertyuiopasdfghjklzxcvbnm";
    if code == 0x39 {
        return Some(' ');
    }
    let lo = match code {
        0x02..=0x0D => digits.chars().nth((code - 0x02) as usize),
        0x1A => Some('['),
        0x1B => Some(']'),
        0x27 => Some(';'),
        0x28 => Some('\''),
        0x29 => Some('`'),
        0x2B => Some('\\'),
        0x33 => Some(','),
        0x34 => Some('.'),
        0x35 => Some('/'),
        0x10..=0x19 => alpha.chars().nth((code - 0x10) as usize), // q..p
        0x1E..=0x26 => alpha.chars().nth(10 + (code - 0x1E) as usize), // a..l
        0x2C..=0x32 => alpha.chars().nth(19 + (code - 0x2C) as usize), // z..m
        _ => None,
    };
    lo.map(|c| {
        if shift {
            match c {
                '1' => '!', '2' => '@', '3' => '#', '4' => '$', '5' => '%', '6' => '^',
                '7' => '&', '8' => '*', '9' => '(', '0' => ')', '-' => '_', '=' => '+',
                '[' => '{', ']' => '}', ';' => ':', '\'' => '"', '`' => '~', '\\' => '|',
                ',' => '<', '.' => '>', '/' => '?',
                other => other.to_ascii_uppercase(),
            }
        } else {
            c
        }
    })
}

fn draw_text(buf: &mut [u8], x0: u16, y0: u16, text: &str, color: (u8, u8, u8)) {
    let mut cx = x0 as usize;
    for ch in text.chars() {
        let Some(glyph) = BASIC_FONTS.get(ch) else {
            cx += 9 * SCALE;
            continue;
        };
        for (row, bits) in glyph.iter().enumerate() {
            for col in 0..8usize {
                if bits & (1 << col) == 0 {
                    continue;
                }
                for dy in 0..SCALE {
                    for dx in 0..SCALE {
                        let px = cx + col * SCALE + dx;
                        let py = y0 as usize + row * SCALE + dy;
                        if px >= WIDTH as usize || py >= HEIGHT as usize {
                            continue;
                        }
                        let off = (py * WIDTH as usize + px) * 4;
                        buf[off] = color.0;
                        buf[off + 1] = color.1;
                        buf[off + 2] = color.2;
                        buf[off + 3] = 0;
                    }
                }
            }
        }
        cx += 9 * SCALE;
    }
}

fn render(state: &State) -> Vec<u8> {
    let mut buf = vec![0u8; WIDTH as usize * HEIGHT as usize * 4];
    for chunk in buf.chunks_exact_mut(4) {
        chunk[0] = 0x0f;
        chunk[1] = 0x17;
        chunk[2] = 0x2a;
        chunk[3] = 0;
    }
    draw_text(&mut buf, LEFT, 80, "AutoOps Bastion - RDP Gateway", WHITE);
    draw_text(&mut buf, LEFT, 120, "Sign in with your bastion account, then pick a host.", DIM);
    match state.screen {
        Screen::Login => {
            let mut y = 200;
            draw_text(&mut buf, LEFT, y, "Username:", if state.focus_password { DIM } else { ACCENT });
            draw_text(&mut buf, LEFT + 240, y, &state.username, WHITE);
            y += LINE_H;
            draw_text(&mut buf, LEFT, y, "Password:", if state.focus_password { ACCENT } else { DIM });
            draw_text(&mut buf, LEFT + 240, y, &"*".repeat(state.password.chars().count()), WHITE);
            y += LINE_H * 2;
            draw_text(&mut buf, LEFT, y, "Tab = switch field    Enter = sign in", DIM);
            if state.busy {
                draw_text(&mut buf, LEFT, y + LINE_H, "Signing in...", WARN);
            }
            if !state.error.is_empty() {
                draw_text(&mut buf, LEFT, y + LINE_H * 2, &state.error, WARN);
            }
        }
        Screen::Menu => {
            draw_text(
                &mut buf,
                LEFT,
                180,
                &format!("Signed in as {} — hosts you may reach ({}):", state.username, state.hosts.len()),
                DIM,
            );
            if state.hosts.is_empty() {
                draw_text(&mut buf, LEFT, 230, "No RDP hosts are authorized for this account.", WARN);
            }
            for (index, host) in state.hosts.iter().enumerate() {
                let y = MENU_TOP + index as u16 * LINE_H;
                let selected = index == state.cursor;
                draw_text(
                    &mut buf,
                    LEFT,
                    y,
                    &format!(
                        "{} {}. {} ({}:{})  account {}",
                        if selected { ">" } else { " " },
                        index + 1,
                        host.name,
                        host.address,
                        host.port,
                        host.account
                    ),
                    if selected { ACCENT } else { WHITE },
                );
            }
            draw_text(&mut buf, LEFT, 700, "Up/Down or mouse to select, Enter to connect.", DIM);
            if state.busy {
                draw_text(&mut buf, LEFT, 730, "Opening session...", WARN);
            }
            if !state.notice.is_empty() {
                draw_text(&mut buf, LEFT, 760, &state.notice, WARN);
            }
        }
        Screen::Target => {
            draw_text(
                &mut buf,
                LEFT,
                180,
                &format!("Streaming {} — press Esc to go back", state.target_name),
                DIM,
            );
            draw_text(
                &mut buf,
                LEFT,
                220,
                "Waiting for the target desktop (the bridge refreshes ~10 times a second)...",
                DIM,
            );
            if !state.notice.is_empty() {
                draw_text(&mut buf, LEFT, 260, &state.notice, WARN);
            }
        }
    }
    buf
}

struct Shared {
    state: Mutex<State>,
    frame: Mutex<Vec<u8>>,
    version: AtomicU64,
}

impl Shared {
    fn new() -> Self {
        let shared = Self {
            state: Mutex::new(State::default()),
            frame: Mutex::new(Vec::new()),
            version: AtomicU64::new(0),
        };
        shared.redraw();
        shared
    }

    fn redraw(&self) {
        let frame = render(&self.state.lock().unwrap());
        *self.frame.lock().unwrap() = frame;
        self.version.fetch_add(1, Ordering::SeqCst);
    }

    /// 新连接进来 / 旧连接断开时把所有会话状态清干净。
    ///
    /// 本进程的 display/input handler 是**全进程一份**（ironrdp-server 的 builder 只收一次），
    /// 所以上一轮登录的 username/token/hosts 会留在内存里；新连接如果不清就会直接看到
    /// 上一个用户的选单、还能点进去连他有权限的机器 —— 这是一个必须堵住的越权。
    fn reset_for_new_connection(&self) {
        *self.state.lock().unwrap() = State::default();
        self.redraw();
        tracing::info!("新连接：会话状态已清空（未登录）");
    }

    fn login(self: &Arc<Self>) {
        {
            let mut state = self.state.lock().unwrap();
            if state.busy {
                return;
            }
            state.busy = true;
            state.error.clear();
        }
        self.redraw();
        let (user, pass) = {
            let state = self.state.lock().unwrap();
            (state.username.clone(), state.password.clone())
        };
        let shared = Arc::clone(self);
        std::thread::spawn(move || {
            let payload = serde_json::json!({ "username": user, "password": pass }).to_string();
            let result = http_json("POST", "/api/auth/login", Some(&payload), None);
            let mut state = shared.state.lock().unwrap();
            state.busy = false;
            match result {
                Ok(body) => {
                    let token = body["data"]["token"].as_str().unwrap_or("").to_string();
                    if token.is_empty() {
                        state.error = format!(
                            "登录失败：{}",
                            body["message"].as_str().unwrap_or("没有拿到 token")
                        );
                        drop(state);
                        shared.redraw();
                    } else {
                        state.token = token;
                        drop(state);
                        shared.load_hosts();
                    }
                }
                Err(err) => {
                    state.error = format!("登录失败：{err}");
                    drop(state);
                    shared.redraw();
                }
            }
        });
    }

    fn load_hosts(self: &Arc<Self>) {
        let shared = Arc::clone(self);
        let token = self.state.lock().unwrap().token.clone();
        std::thread::spawn(move || {
            let result = http_json("GET", "/api/rdp-gateway/hosts", None, Some(&token));
            let mut state = shared.state.lock().unwrap();
            match result {
                Ok(body) => {
                    let mut hosts = Vec::new();
                    if let Some(rows) = body["data"]["hosts"].as_array() {
                        for row in rows {
                            let accounts = row["accounts"].as_array().cloned().unwrap_or_default();
                            let first = accounts.first();
                            hosts.push(HostRow {
                                host_id: row["hostId"].as_i64().unwrap_or(0),
                                name: row["hostName"].as_str().unwrap_or("").to_string(),
                                address: row["address"].as_str().unwrap_or("").to_string(),
                                port: row["port"].as_u64().unwrap_or(3389) as u16,
                                account_id: first.and_then(|a| a["accountId"].as_i64()).unwrap_or(0),
                                account: first
                                    .and_then(|a| a["username"].as_str())
                                    .unwrap_or("")
                                    .to_string(),
                            });
                        }
                    }
                    state.hosts = hosts;
                    state.screen = Screen::Menu;
                    state.cursor = 0;
                    state.error.clear();
                }
                Err(err) => state.error = format!("取主机列表失败：{err}"),
            }
            drop(state);
            shared.redraw();
        });
    }

    fn open_session(self: &Arc<Self>) {
        let (host, token) = {
            let state = self.state.lock().unwrap();
            match state.hosts.get(state.cursor) {
                Some(host) => (host.clone(), state.token.clone()),
                None => return,
            }
        };
        {
            let mut state = self.state.lock().unwrap();
            if state.busy {
                return;
            }
            state.busy = true;
            state.notice.clear();
        }
        self.redraw();
        let shared = Arc::clone(self);
        std::thread::spawn(move || {
            let payload = serde_json::json!({
                "hostId": host.host_id,
                "accountId": host.account_id,
            })
            .to_string();
            let result = http_json("POST", "/api/rdp-gateway/sessions", Some(&payload), Some(&token));
            let mut state = shared.state.lock().unwrap();
            state.busy = false;
            match result {
                Ok(body) => {
                    let sid = body["data"]["sid"].as_str().unwrap_or("");
                    state.notice = format!(
                        "Session recorded (sid {sid}) — next: stream {} here",
                        host.name
                    );
                }
                Ok(body) => {
                    let sid = body["data"]["sid"].as_str().unwrap_or("").to_string();
                    state.sid = sid.clone();
                    drop(state);
                    shared.redraw();
                    shared.start_target(&host.name, &sid);
                    return;
                }
                Err(err) => state.notice = format!("Open session failed: {err}"),
            }
            drop(state);
            shared.redraw();
        });
    }

    /// 选中主机之后：让后端用 mstsc 连目标机，然后起一个「取帧 + 转发输入」的泵。
    fn start_target(self: &Arc<Self>, name: &str, sid: &str) {
        {
            let mut st = self.state.lock().unwrap();
            if st.busy {
                return;
            }
            st.busy = true;
            st.target_name = name.to_string();
            st.notice = format!("Connecting to {name} ...");
        }
        self.redraw();
        let shared = Arc::clone(self);
        let name = name.to_string();
        let sid = sid.to_string();
        std::thread::spawn(move || {
            let token = shared.state.lock().unwrap().token.clone();
            let payload = serde_json::json!({ "sid": sid }).to_string();
            let result = http_json("POST", "/api/rdp-gateway/connect", Some(&payload), Some(&token));
            let mut st = shared.state.lock().unwrap();
            st.busy = false;
            match result {
                Ok(_) => {
                    st.screen = Screen::Target;
                    st.notice.clear();
                    tracing::info!(target = %name, sid = %sid, "目标机已连上，开始推画面");
                    drop(st);
                    shared.redraw();
                    shared.spawn_pump(sid.clone(), token);
                }
                Err(err) => {
                    st.notice = format!("连接目标机失败：{err}");
                    tracing::warn!(target = %name, %err, "连接目标机失败");
                    drop(st);
                    shared.redraw();
                }
            }
        });
    }

    /// 取帧 + 把输入队列发出去，60ms 一轮；离开 Target 屏自动退出。
    fn spawn_pump(self: &Arc<Self>, sid: String, token: String) {
        let shared = Arc::clone(self);
        std::thread::spawn(move || {
            let mut last_summary = std::time::Instant::now();
            let mut frames = 0u64;
            loop {
                let (events, active, keys, mouses) = {
                    let mut st = shared.state.lock().unwrap();
                    let events = std::mem::take(&mut st.pending_input);
                    (events, st.screen == Screen::Target, st.key_events, st.mouse_events)
                };
                if !active {
                    break;
                }
                if !events.is_empty() {
                    let payload = serde_json::json!({ "sid": sid, "events": events }).to_string();
                    if let Err(err) =
                        http_json("POST", "/api/rdp-gateway/input", Some(&payload), Some(&token))
                    {
                        tracing::debug!(%err, "转发输入失败");
                    }
                }
                match http_bytes("GET", &format!("/api/rdp-gateway/frame?sid={sid}"), Some(&token)) {
                    Ok((200, body)) => {
                        shared.apply_tiles(&body);
                        frames += 1;
                    }
                    Ok((204, _)) => {}
                    Ok((code, _)) => tracing::warn!(code, "帧接口返回异常"),
                    Err(err) => tracing::debug!(%err, "取帧失败"),
                }
                if last_summary.elapsed() >= Duration::from_secs(30) {
                    tracing::info!(keys, mouses, frames, "输入与画面汇总（累计）");
                    last_summary = std::time::Instant::now();
                }
                std::thread::sleep(Duration::from_millis(60));
            }
            tracing::info!(frames, "画面泵已退出");
        });
    }

    /// 把「变化块」贴进帧缓冲：`[u32 n][n × (u16 x,y,w,h + RGBA)]`
    fn apply_tiles(&self, body: &[u8]) {
        if body.len() < 4 {
            return;
        }
        let count = u32::from_le_bytes([body[0], body[1], body[2], body[3]]) as usize;
        let mut offset = 4usize;
        let mut frame = self.frame.lock().unwrap();
        if frame.len() != WIDTH as usize * HEIGHT as usize * 4 {
            frame.resize(WIDTH as usize * HEIGHT as usize * 4, 0);
        }
        for _ in 0..count {
            if offset + 8 > body.len() {
                break;
            }
            let x = u16::from_le_bytes([body[offset], body[offset + 1]]) as usize;
            let y = u16::from_le_bytes([body[offset + 2], body[offset + 3]]) as usize;
            let w = u16::from_le_bytes([body[offset + 4], body[offset + 5]]) as usize;
            let h = u16::from_le_bytes([body[offset + 6], body[offset + 7]]) as usize;
            offset += 8;
            let size = w * h * 4;
            if offset + size > body.len() || w == 0 || h == 0 {
                break;
            }
            for row in 0..h {
                let dst_y = y + row;
                if dst_y >= HEIGHT as usize || x + w > WIDTH as usize {
                    break;
                }
                let dst = (dst_y * WIDTH as usize + x) * 4;
                if dst + w * 4 > frame.len() {
                    break;
                }
                frame[dst..dst + w * 4]
                    .copy_from_slice(&body[offset + row * w * 4..offset + (row + 1) * w * 4]);
            }
            offset += size;
        }
        drop(frame);
        self.version.fetch_add(1, Ordering::SeqCst);
    }

    fn stop_target(self: &Arc<Self>) {
        let (sid, token) = {
            let mut st = self.state.lock().unwrap();
            if st.screen != Screen::Target {
                return;
            }
            st.screen = Screen::Menu;
            st.notice = "已断开目标机".to_string();
            (st.sid.clone(), st.token.clone())
        };
        tracing::info!(sid = %sid, "离开目标机画面");
        self.redraw();
        std::thread::spawn(move || {
            let payload = serde_json::json!({ "sid": sid }).to_string();
            let _ = http_json("POST", "/api/rdp-gateway/disconnect", Some(&payload), Some(&token));
        });
    }
}

struct GatewayConnectionHandler {
    shared: Arc<Shared>,
}

impl ConnectionHandler for GatewayConnectionHandler {
    fn on_accept(&mut self, peer: SocketAddr) -> bool {
        tracing::info!(%peer, "RDP 客户端接入");
        self.shared.reset_for_new_connection();
        true
    }

    fn on_disconnected(
        &mut self,
        peer: SocketAddr,
        duration: Duration,
        error: Option<&anyhow::Error>,
    ) -> PostConnectionAction {
        tracing::info!(%peer, elapsed = ?duration, error = ?error.map(|e| e.to_string()), "RDP 客户端断开");
        // 断开也清一次：下次连进来绝不继承上一个用户的登录态
        self.shared.reset_for_new_connection();
        PostConnectionAction::Continue
    }
}

struct GatewayDisplay {
    shared: Arc<Shared>,
}

#[async_trait]
impl RdpServerDisplay for GatewayDisplay {
    async fn size(&mut self) -> DesktopSize {
        DesktopSize { width: WIDTH, height: HEIGHT }
    }

    async fn updates(&mut self) -> Result<Box<dyn RdpServerDisplayUpdates>> {
        Ok(Box::new(GatewayUpdates { shared: Arc::clone(&self.shared), version: 0 }))
    }
}

struct GatewayUpdates {
    shared: Arc<Shared>,
    version: u64,
}

#[async_trait]
impl RdpServerDisplayUpdates for GatewayUpdates {
    async fn next_update(&mut self) -> Result<Option<DisplayUpdate>> {
        loop {
            let current = self.shared.version.load(Ordering::SeqCst);
            if current != self.version {
                self.version = current;
                let frame = self.shared.frame.lock().unwrap().clone();
                let update = BitmapUpdate {
                    x: 0,
                    y: 0,
                    width: NonZeroU16::new(WIDTH).unwrap(),
                    height: NonZeroU16::new(HEIGHT).unwrap(),
                    format: PixelFormat::RgbX32,
                    data: Bytes::from(frame),
                    stride: NonZeroUsize::new(WIDTH as usize * 4).unwrap(),
                };
                return Ok(Some(DisplayUpdate::Bitmap(update)));
            }
            tokio::time::sleep(Duration::from_millis(80)).await;
        }
    }
}

struct GatewayInput {
    shared: Arc<Shared>,
}

impl RdpServerInputHandler for GatewayInput {
    fn keyboard(&mut self, event: KeyboardEvent) {
        let shared = Arc::clone(&self.shared);
        // 逐条事件只进 trace（真要排查时把 RUST_LOG 里加 trace）；
        // info 级每 30 条给一行汇总，选单/登录这些「有意义的动作」才单独打 info。
        tracing::trace!(?event, "keyboard");
        match event {
            KeyboardEvent::UnicodePressed(ch) => {
                let mut state = shared.state.lock().unwrap();
                state.key_events += 1;
                match (state.screen, char::from_u32(ch as u32)) {
                    (Screen::Login, Some('\r')) => {
                        drop(state);
                        shared.login();
                        return;
                    }
                    (Screen::Login, Some('\t')) => state.focus_password = !state.focus_password,
                    (Screen::Login, Some('\u{8}')) => {
                        if state.focus_password {
                            state.password.pop();
                        } else {
                            state.username.pop();
                        }
                    }
                    (Screen::Login, Some(c)) if !c.is_control() => {
                        if state.focus_password {
                            state.password.push(c);
                        } else {
                            state.username.push(c);
                        }
                    }
                    (Screen::Target, Some(c)) if !c.is_control() => {
                        let vk = char_to_vk(c);
                        if vk != 0 {
                            state
                                .pending_input
                                .push(serde_json::json!({"kind": "key", "vk": vk, "down": true}));
                            state
                                .pending_input
                                .push(serde_json::json!({"kind": "key", "vk": vk, "down": false}));
                        }
                    }
                    _ => {}
                }
                let total = state.key_events;
                drop(state);
                if total % 30 == 0 {
                    tracing::info!(keys = total, "键盘事件累计（每 30 条汇总一次）");
                }
                shared.redraw();
            }
            KeyboardEvent::Pressed { code, extended } => {
                if code == 0x2A || code == 0x36 {
                    shared.state.lock().unwrap().shift = true;
                    return;
                }
                let shift = shared.state.lock().unwrap().shift;
                let mut state = shared.state.lock().unwrap();
                state.key_events += 1;
                match state.screen {
                    Screen::Login => match code {
                        0x1C => {
                            drop(state);
                            shared.login();
                            return;
                        }
                        0x0F => state.focus_password = !state.focus_password,
                        0x0E => {
                            if state.focus_password {
                                state.password.pop();
                            } else {
                                state.username.pop();
                            }
                        }
                        _ => {
                            if let Some(ch) = scan_to_char(code, shift) {
                                if state.focus_password {
                                    state.password.push(ch);
                                } else {
                                    state.username.push(ch);
                                }
                            }
                        }
                    },
                    Screen::Menu => match code {
                        0x48 | 0x19 => {
                            let len = state.hosts.len();
                            if len > 0 {
                                state.cursor = (state.cursor + len - 1) % len;
                            }
                        }
                        0x50 | 0x18 => {
                            let len = state.hosts.len();
                            if len > 0 {
                                state.cursor = (state.cursor + 1) % len;
                            }
                        }
                        0x1C => {
                            drop(state);
                            shared.open_session();
                            return;
                        }
                        _ => {}
                    },
                    Screen::Target => {
                        if code == 0x01 {
                            drop(state);
                            shared.stop_target();
                            return;
                        }
                        let vk = scan_to_vk(code, extended);
                        if vk != 0 {
                            state
                                .pending_input
                                .push(serde_json::json!({"kind": "key", "vk": vk, "down": true}));
                        }
                    }
                }
                let total = state.key_events;
                drop(state);
                if total % 30 == 0 {
                    tracing::info!(keys = total, "键盘事件累计（每 30 条汇总一次）");
                }
                shared.redraw();
            }
            KeyboardEvent::Released { code, extended } => {
                if code == 0x2A || code == 0x36 {
                    shared.state.lock().unwrap().shift = false;
                    return;
                }
                let mut state = shared.state.lock().unwrap();
                if state.screen == Screen::Target {
                    let vk = scan_to_vk(code, extended);
                    if vk != 0 {
                        state
                            .pending_input
                            .push(serde_json::json!({"kind": "key", "vk": vk, "down": false}));
                    }
                }
            }
            _ => {}
        }
    }

    fn mouse(&mut self, event: MouseEvent) {
        tracing::trace!(?event, "mouse");
        let shared = Arc::clone(&self.shared);
        match event {
            MouseEvent::Move { x, y } => {
                shared.state.lock().unwrap().mouse = (x, y);
            }
            MouseEvent::LeftPressed => {
                let mut state = shared.state.lock().unwrap();
                state.mouse_events += 1;
                let (x, y) = state.mouse;
                match state.screen {
                    Screen::Menu if y >= MENU_TOP => {
                        let row = ((y - MENU_TOP) / LINE_H) as usize;
                        if row < state.hosts.len() {
                            state.cursor = row;
                            drop(state);
                            shared.open_session();
                        }
                    }
                    Screen::Target => {
                        state
                            .pending_input
                            .push(serde_json::json!({"kind": "mouse", "x": x, "y": y, "action": "down"}));
                        drop(state);
                    }
                    _ => {}
                }
            }
            MouseEvent::LeftReleased => {
                let mut state = shared.state.lock().unwrap();
                state.mouse_events += 1;
                let (x, y) = state.mouse;
                if state.screen == Screen::Target {
                    state
                        .pending_input
                        .push(serde_json::json!({"kind": "mouse", "x": x, "y": y, "action": "up"}));
                }
                let total = state.mouse_events;
                drop(state);
                if total % 30 == 0 {
                    tracing::info!(clicks = total, "鼠标事件累计（每 30 条汇总一次）");
                }
            }
            MouseEvent::RightPressed => {
                let mut state = shared.state.lock().unwrap();
                let (x, y) = state.mouse;
                if state.screen == Screen::Target {
                    state
                        .pending_input
                        .push(serde_json::json!({"kind": "mouse", "x": x, "y": y, "action": "right"}));
                }
            }
            _ => {}
        }
    }
}

fn ensure_tls_identity(dir: &Path) -> Result<TlsIdentityCtx> {
    std::fs::create_dir_all(dir).ok();
    let cert = dir.join("rdp-gateway-cert.pem");
    let key = dir.join("rdp-gateway-key.pem");
    if !cert.exists() || !key.exists() {
        let certified = rcgen::generate_simple_self_signed(vec![
            "localhost".to_string(),
            "127.0.0.1".to_string(),
        ])
        .context("生成自签证书失败")?;
        std::fs::write(&cert, certified.cert.pem()).context("写证书失败")?;
        std::fs::write(&key, certified.signing_key.serialize_pem()).context("写私钥失败")?;
        tracing::info!(cert = %cert.display(), "已生成 RDP 网关自签证书");
    }
    TlsIdentityCtx::init_from_paths(&cert, &key).context("加载 TLS 身份失败")
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info,bastion_rdp_gateway=debug".into()),
        )
        .init();

    let port: u16 = std::env::var("RDP_GATEWAY_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(4489);
    let addr: SocketAddr = ([0, 0, 0, 0], port).into();
    let instance_dir = std::env::var("BASTION_INSTANCE_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("../bastion-backend/instance"));

    let tls = ensure_tls_identity(&instance_dir)?;
    let acceptor = tls.make_acceptor().context("构造 TLS acceptor 失败")?;

    let shared = Arc::new(Shared::new());
    let mut server = RdpServer::builder()
        .with_addr(addr)
        .with_tls(acceptor)
        .with_input_handler(GatewayInput { shared: Arc::clone(&shared) })
        .with_display_handler(GatewayDisplay { shared: Arc::clone(&shared) })
        .with_connection_handler(Some(Box::new(GatewayConnectionHandler {
            shared: Arc::clone(&shared),
        })))
        .build();

    tracing::info!("RDP 网关已启动：mstsc /v:<本机 IP>:{port}（登录用堡垒机账号）");
    server.run().await?;
    Ok(())
}
