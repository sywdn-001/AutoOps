//! AutoOps 堡垒机 —— RDP 网关（实验）
//!
//! `mstsc /v:<堡垒机>:4489` 连进来后，这里就是那个 **RDP 服务端**：先把选单画成一张位图
//! 推给客户端，再把鼠标/键盘事件收上来做命中测试；选中后再把目标机的画面接到这条会话里。
//!
//! 当前里程碑：能收 mstsc 的连接、稳定推出画面、收得到输入事件。
//! - TLS 身份：首次启动用 `rcgen` 自签一张（落在 `instance/` 下），之后复用；
//! - 画面：`font8x8` 画 ASCII 文本（选单行以后由 Python 侧的 `rdp_menu.py` 提供）；
//! - 输入：键盘/鼠标事件先打成日志，确认 PDU 解出来了，再接菜单命中与转发。

use std::net::SocketAddr;
use std::num::{NonZeroU16, NonZeroUsize};
use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::{Context, Result};
use async_trait::async_trait;
use bytes::Bytes;
use font8x8::{UnicodeFonts, BASIC_FONTS};
use ironrdp_server::{
    BitmapUpdate, DesktopSize, DisplayUpdate, KeyboardEvent, MouseEvent, PixelFormat, RdpServer,
    RdpServerDisplay, RdpServerDisplayUpdates, RdpServerInputHandler, TlsIdentityCtx,
};

const WIDTH: u16 = 1280;
const HEIGHT: u16 = 800;
const FONT_SCALE: usize = 2;
/// 每行文字的行高（像素）
const LINE_HEIGHT: u16 = 40;

/// 选单内容：先用写死的几行，确认「画得出、点得动」；之后由 Python 的 `rdp_menu.py` 生成。
fn menu_lines() -> Vec<String> {
    vec![
        "AutoOps Bastion - RDP Gateway (experimental)".to_string(),
        String::new(),
        "This screen is rendered by the gateway itself, not by the target host.".to_string(),
        "Next step: list the hosts your account may reach, pick one, then stream it.".to_string(),
        String::new(),
        "Keyboard and mouse events are already received and logged.".to_string(),
    ]
}

fn draw_text(buf: &mut [u8], x0: u16, y0: u16, text: &str) {
    let mut cursor_x = x0 as usize;
    for ch in text.chars() {
        let Some(glyph) = BASIC_FONTS.get(ch) else {
            cursor_x += 9 * FONT_SCALE;
            continue;
        };
        for (row, bits) in glyph.iter().enumerate() {
            for col in 0..8usize {
                if bits & (1 << col) == 0 {
                    continue;
                }
                for dy in 0..FONT_SCALE {
                    for dx in 0..FONT_SCALE {
                        let px = cursor_x + col * FONT_SCALE + dx;
                        let py = y0 as usize + row * FONT_SCALE + dy;
                        if px >= WIDTH as usize || py >= HEIGHT as usize {
                            continue;
                        }
                        let off = (py * WIDTH as usize + px) * 4;
                        buf[off] = 0xe6;
                        buf[off + 1] = 0xed;
                        buf[off + 2] = 0xf3;
                        buf[off + 3] = 0;
                    }
                }
            }
        }
        cursor_x += 9 * FONT_SCALE;
    }
}

fn render_menu() -> Vec<u8> {
    let mut buf = vec![0u8; WIDTH as usize * HEIGHT as usize * 4];
    for chunk in buf.chunks_exact_mut(4) {
        chunk[0] = 0x0f;
        chunk[1] = 0x17;
        chunk[2] = 0x2a;
        chunk[3] = 0;
    }
    let mut y = 80u16;
    for line in menu_lines() {
        draw_text(&mut buf, 80, y, &line);
        y += LINE_HEIGHT;
    }
    buf
}

struct MenuDisplay {
    frame: Vec<u8>,
    size: DesktopSize,
    sent: bool,
}

#[async_trait]
impl RdpServerDisplay for MenuDisplay {
    async fn size(&mut self) -> DesktopSize {
        self.size
    }

    async fn updates(&mut self) -> Result<Box<dyn RdpServerDisplayUpdates>> {
        let frame = if self.sent {
            None
        } else {
            self.sent = true;
            Some(std::mem::take(&mut self.frame))
        };
        Ok(Box::new(MenuUpdates { frame }))
    }
}

struct MenuUpdates {
    frame: Option<Vec<u8>>,
}

#[async_trait]
impl RdpServerDisplayUpdates for MenuUpdates {
    async fn next_update(&mut self) -> Result<Option<DisplayUpdate>> {
        if let Some(frame) = self.frame.take() {
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
        // 选单是静态的：没有新画面就不推，免得白占带宽
        tokio::time::sleep(Duration::from_secs(30)).await;
        Ok(None)
    }
}

struct LogInput;

impl RdpServerInputHandler for LogInput {
    fn keyboard(&mut self, event: KeyboardEvent) {
        tracing::info!(?event, "keyboard");
    }

    fn mouse(&mut self, event: MouseEvent) {
        tracing::info!(?event, "mouse");
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

    let display = MenuDisplay {
        frame: render_menu(),
        size: DesktopSize {
            width: WIDTH,
            height: HEIGHT,
        },
        sent: false,
    };

    let mut server = RdpServer::builder()
        .with_addr(addr)
        .with_tls(acceptor)
        .with_input_handler(LogInput)
        .with_display_handler(display)
        .build();

    tracing::info!("RDP 网关已启动：mstsc /v:<本机 IP>:{port}");
    server.run().await?;
    Ok(())
}
