//! 目标机 RDP 客户端（真正的实现，不再依赖 mstsc 窗口桥）。
//!
//! 用 `ironrdp-client`（IronRDP 自己的客户端引擎）连目标机的 3389：
//! - 出：`RdpClient::input_sender()` 把键盘/鼠标的 FastPath 事件发给目标机；
//! - 入：`RdpOutputEvent::Image` 拿到整屏像素，贴进网关自己的 1280×800 帧缓冲。
//!
//! 相比之前的「起 mstsc 再截屏」：不需要交互桌面、不抢前台、不受单会话影响。

use std::num::NonZeroU16;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use anyhow::{Context, Result};
use ironrdp_client::config::{ConfigBuilder, Destination};
use ironrdp_client::rdp::{RdpClient, RdpInputEvent, RdpOutputEvent};
use ironrdp_pdu::input::fast_path::{FastPathInputEvent, KeyboardFlags};
use ironrdp_pdu::input::mouse::{MousePdu, PointerFlags};
use tokio::sync::mpsc;

/// 一次目标机连接需要的东西（口令来自堡垒机数据库，已经过审计）
pub struct TargetSpec {
    pub host: String,
    pub port: u16,
    pub username: String,
    pub password: String,
    pub width: u16,
    pub height: u16,
}

pub struct TargetHandle {
    pub input: mpsc::UnboundedSender<RdpInputEvent>,
}

impl TargetHandle {
    /// 把网关收到的输入转成 FastPath 事件发给目标机
    pub fn send_key(&self, scan_code: u8, extended: bool, down: bool) {
        let mut flags = KeyboardFlags::empty();
        if !down {
            flags |= KeyboardFlags::RELEASE;
        }
        if extended {
            flags |= KeyboardFlags::EXTENDED;
        }
        let event = FastPathInputEvent::KeyboardEvent(flags, scan_code);
        let _ = self.input.send(RdpInputEvent::FastPath(smallvec::smallvec![event]));
    }

    pub fn send_unicode(&self, ch: u16, down: bool) {
        let mut flags = KeyboardFlags::empty();
        if !down {
            flags |= KeyboardFlags::RELEASE;
        }
        let event = FastPathInputEvent::UnicodeKeyboardEvent(flags, ch);
        let _ = self.input.send(RdpInputEvent::FastPath(smallvec::smallvec![event]));
    }

    pub fn send_mouse(&self, x: u16, y: u16, button: MouseButton, down: bool) {
        let mut bits = 0x0800u16; // PTRFLAGS_MOVE
        match button {
            MouseButton::Left => {
                bits |= 0x1000; // BUTTON1
                if down {
                    bits |= 0x8000; // DOWN
                }
            }
            MouseButton::Right => {
                bits |= 0x2000; // BUTTON2
                if down {
                    bits |= 0x8000;
                }
            }
            MouseButton::None => {}
        }
        let flags = PointerFlags::from_bits_truncate(bits);
        let pdu = MousePdu {
            flags,
            number_of_wheel_rotation_units: 0,
            x_position: x,
            y_position: y,
        };
        let event = FastPathInputEvent::MouseEvent(pdu);
        let _ = self.input.send(RdpInputEvent::FastPath(smallvec::smallvec![event]));
    }

    pub fn close(&self) {
        let _ = self.input.send(RdpInputEvent::Close);
    }
}

#[derive(Debug, Clone, Copy)]
pub enum MouseButton {
    None,
    Left,
    Right,
}

/// 连目标机并把画面推给网关。
///
/// `RdpClient::run` 的 future 不是 `Send`（上游实现里带局部可变借用），所以不能扔进
/// 多线程运行时的 `tokio::spawn`；这里给客户端单独开一个**单线程运行时**的线程，
/// 画面泵再开一个，两个线程之间用 tokio 的 mpsc 通道通信。
pub fn start(
    spec: TargetSpec,
    frame: Arc<Mutex<Vec<u8>>>,
    version: Arc<AtomicU64>,
    out_w: u16,
    out_h: u16,
) -> Result<TargetHandle> {
    let destination = Destination::new(format!("{}:{}", spec.host, spec.port))
        .with_context(|| format!("目标机地址不合法：{}:{}", spec.host, spec.port))?;
    let config = ConfigBuilder::new()
        .with_destination(destination)

        .with_client_build(18363)
        .with_client_dir("C:\\Windows\\System32")
        .with_client_name("bastion-rdp-gateway")
        .with_platform(ironrdp_pdu::rdp::capability_sets::MajorPlatformType::WINDOWS)
        .with_keyboard_type(ironrdp_pdu::gcc::KeyboardType::IbmEnhanced)
        .with_keyboard_subtype(0)
        .with_keyboard_functional_keys_count(12)
        .with_ime_file_name("")
        .with_dig_product_id("")
        .with_username(spec.username.clone())
        .with_password(spec.password.clone())
        .with_desktop_width(spec.width)
        .with_desktop_height(spec.height)
        .with_desktop_scale_factor(100)
        .with_color_depth(32)
        .with_credssp(true)
        .with_tls(true)
        .with_autologon(true)
        .with_server_pointer(true)
        .with_compression(true)
        .build()
        .context("构造 RDP 客户端配置失败")?;

    let (out_tx, mut out_rx) = mpsc::channel::<RdpOutputEvent>(64);
    let client = RdpClient::new(config, out_tx);
    let input = client.input_sender();

    // 客户端：自己的单线程运行时
    std::thread::Builder::new()
        .name("rdp-target-client".into())
        .spawn(move || {
            let rt = match tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
            {
                Ok(rt) => rt,
                Err(err) => {
                    tracing::warn!(%err, "目标机客户端运行时创建失败");
                    return;
                }
            };
            rt.block_on(async move { client.run().await });
            tracing::info!("目标机客户端已退出");
        })
        .context("起目标机客户端线程失败")?;

    // 画面泵：自己的单线程运行时
    std::thread::Builder::new()
        .name("rdp-target-pump".into())
        .spawn(move || {
            let rt = match tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
            {
                Ok(rt) => rt,
                Err(err) => {
                    tracing::warn!(%err, "画面泵运行时创建失败");
                    return;
                }
            };
            rt.block_on(async move {
                let mut frames = 0u64;
                while let Some(event) = out_rx.recv().await {
                    match event {
                        RdpOutputEvent::Image {
                            buffer,
                            width,
                            height,
                        } => {
                            blit(&frame, &version, &buffer, width, height, out_w, out_h);
                            frames += 1;
                            if frames % 60 == 0 {
                                tracing::debug!(frames, "目标机画面帧数");
                            }
                        }
                        RdpOutputEvent::ConnectionFailure(err) => {
                            tracing::warn!(%err, "目标机连接失败");
                            break;
                        }
                        RdpOutputEvent::Terminated(result) => {
                            tracing::info!(?result, "目标机会话结束");
                            break;
                        }
                        _ => {}
                    }
                }
                tracing::info!(frames, "目标机画面泵已退出");
            });
        })
        .context("起画面泵线程失败")?;

    Ok(TargetHandle { input })
}

/// 把目标机送来的整屏像素贴进网关的帧缓冲（尺寸不同就按左上角摆放 + 最近邻缩放）。
fn blit(
    frame: &Arc<Mutex<Vec<u8>>>,
    version: &Arc<AtomicU64>,
    buffer: &[u32],
    width: NonZeroU16,
    height: NonZeroU16,
    out_w: u16,
    out_h: u16,
) {
    let sw = width.get() as usize;
    let sh = height.get() as usize;
    if sw == 0 || sh == 0 || buffer.len() < sw * sh {
        return;
    }
    let mut guard = frame.lock().unwrap();
    if guard.len() != out_w as usize * out_h as usize * 4 {
        guard.resize(out_w as usize * out_h as usize * 4, 0xFF);
    }
    // 目标机分辨率与网关画面一致时直拷，否则最近邻缩放
    for y in 0..out_h as usize {
        let sy = if sh == out_h as usize { y } else { y * sh / out_h as usize };
        let dst_row = y * out_w as usize * 4;
        for x in 0..out_w as usize {
            let sx = if sw == out_w as usize { x } else { x * sw / out_w as usize };
            let pixel = buffer[sy * sw + sx];
            let off = dst_row + x * 4;
            guard[off] = (pixel & 0xFF) as u8;
            guard[off + 1] = ((pixel >> 8) & 0xFF) as u8;
            guard[off + 2] = ((pixel >> 16) & 0xFF) as u8;
            guard[off + 3] = 0;
        }
    }
    drop(guard);
    version.fetch_add(1, Ordering::SeqCst);
}
