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
    BitmapUpdate, DesktopSize, DisplayUpdate, KeyboardEvent, MouseEvent, PixelFormat, RdpServer,
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
const WHITE: (u8, u8, u8) = (0xe6, 0xed, 0xf3);
const DIM: (u8, u8, u8) = (0x94, 0xa3, 0xb8);
const ACCENT: (u8, u8, u8) = (0x60, 0xa5, 0xfa);
const WARN: (u8, u8, u8) = (0xfb, 0xbf, 0x24);

fn http_json(method: &str, path: &str, body: Option<&str>, token: Option<&str>) -> Result<Value, String> {
    let addr: SocketAddr = format!("{API_HOST}:{API_PORT}").parse().unwrap();
    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_secs(5))
        .map_err(|e| format!("连不上后端 {API_HOST}:{API_PORT}：{e}"))?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(10)));
    let payload = body.unwrap_or("");
    let mut req = format!(
        "{method} {path} HTTP/1.1\r\nHost: {API_HOST}:{API_PORT}\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: {}\r\n",
        payload.len()
    );
    if let Some(t) = token {
        req.push_str(&format!("Authorization: Bearer {t}\r\n"));
    }
    req.push_str("\r\n");
    req.push_str(payload);
    stream.write_all(req.as_bytes()).map_err(|e| format!("写请求失败：{e}"))?;
    let mut raw = Vec::new();
    stream.read_to_end(&mut raw).map_err(|e| format!("读响应失败：{e}"))?;
    let text = String::from_utf8_lossy(&raw).to_string();
    let body = text.split("\r\n\r\n").nth(1).ok_or("响应里没有正文")?;
    serde_json::from_str::<Value>(body).map_err(|e| format!("响应不是 JSON：{e}"))
}

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
                Err(err) => state.notice = format!("Open session failed: {err}"),
            }
            drop(state);
            shared.redraw();
        });
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
        tracing::info!(?event, "keyboard");
        let shared = Arc::clone(&self.shared);
        match event {
            KeyboardEvent::UnicodePressed(ch) => {
                let mut state = shared.state.lock().unwrap();
                match (state.screen, char::from_u32(ch as u32)) {
                    (Screen::Login, Some('\r')) => {
                        drop(state);
                        shared.login();
                        return;
                    }
                    (Screen::Login, Some('\t')) => {
                        state.focus_password = !state.focus_password
                    }
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
                    _ => {}
                }
                drop(state);
                shared.redraw();
            }
            KeyboardEvent::Pressed { code, .. } => {
                if code == 0x2A || code == 0x36 {
                    shared.state.lock().unwrap().shift = true;
                    return;
                }
                let shift = shared.state.lock().unwrap().shift;
                let mut state = shared.state.lock().unwrap();
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
                }
                drop(state);
                shared.redraw();
            }
            KeyboardEvent::Released { code, .. } => {
                if code == 0x2A || code == 0x36 {
                    shared.state.lock().unwrap().shift = false;
                }
            }
            _ => {}
        }
    }

    fn mouse(&mut self, event: MouseEvent) {
        tracing::debug!(?event, "mouse");
        let shared = Arc::clone(&self.shared);
        match event {
            MouseEvent::Move { x, y } => {
                shared.state.lock().unwrap().mouse = (x, y);
            }
            MouseEvent::LeftPressed => {
                let mut state = shared.state.lock().unwrap();
                if state.screen == Screen::Menu {
                    let (_, y) = state.mouse;
                    if y >= MENU_TOP {
                        let row = ((y - MENU_TOP) / LINE_H) as usize;
                        if row < state.hosts.len() {
                            state.cursor = row;
                            drop(state);
                            shared.open_session();
                        }
                    }
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
        .build();

    tracing::info!("RDP 网关已启动：mstsc /v:<本机 IP>:{port}（登录用堡垒机账号）");
    server.run().await?;
    Ok(())
}
