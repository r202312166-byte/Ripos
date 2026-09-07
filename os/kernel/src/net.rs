//! net.rs -- smoltcp TCP/IP stack over the e1000 NIC (Phase 2).
//!
//! A single Ethernet interface (static 10.0.2.15/24 on QEMU slirp), a
//! fixed pool of TCP/UDP sockets with static buffers, and the kernel-main-
//! loop polling that drives ARP/IP/TCP.  Exposed to Python as kern.net_*
//! through the C shim; the pure-Python socket.py wraps them.

use alloc::vec;

use smoltcp::iface::{Config, Interface, SocketHandle, SocketSet};
use smoltcp::socket::tcp;
use smoltcp::socket::udp;
use smoltcp::time::Instant;
use smoltcp::wire::{EthernetAddress, HardwareAddress, IpAddress, IpCidr, Ipv4Address};

use crate::klog;
use crate::nic;

const MAX_SOCKETS: usize = 8;
const TCP_BUF: usize = 32 * 1024;
const UDP_BUF: usize = 4096;

// ---- device ------------------------------------------------------------

struct NicDevice;
static mut DEVICE: NicDevice = NicDevice;

impl smoltcp::phy::Device for NicDevice {
    type RxToken<'a> = NicRxToken;
    type TxToken<'a> = NicTxToken;

    fn capabilities(&self) -> smoltcp::phy::DeviceCapabilities {
        let mut c = smoltcp::phy::DeviceCapabilities::default();
        c.max_transmission_unit = 1500;
        c.max_burst_size = Some(32);
        c.medium = smoltcp::phy::Medium::Ethernet;
        c
    }

    fn receive(&mut self, _ts: Instant) -> Option<(Self::RxToken<'_>, Self::TxToken<'_>)> {
        if nic::rx_pending() {
            Some((NicRxToken, NicTxToken))
        } else {
            None
        }
    }

    fn transmit(&mut self, _ts: Instant) -> Option<Self::TxToken<'_>> {
        Some(NicTxToken)
    }
}

struct NicRxToken;
impl smoltcp::phy::RxToken for NicRxToken {
    fn consume<R, F>(self, f: F) -> R
    where
        F: FnOnce(&mut [u8]) -> R,
    {
        let mut buf = [0u8; 2048];
        let len = nic::poll_rx(&mut buf).unwrap_or(0);
        f(&mut buf[..len])
    }
}

static mut TX_CONSUMED: bool = false;

struct NicTxToken;
impl smoltcp::phy::TxToken for NicTxToken {
    fn consume<R, F>(self, len: usize, f: F) -> R
    where
        F: FnOnce(&mut [u8]) -> R,
    {
        if !unsafe { TX_CONSUMED } {
            unsafe { TX_CONSUMED = true };
            klog(&crate::alloc::format!("net: tx consume len={}\n", len));
        }
        let mut buf = [0u8; 2048];
        let r = f(&mut buf[..len]);
        let _ = nic::tx_send(len, |d| d[..len].copy_from_slice(&buf[..len]));
        r
    }
}

// ---- sockets -----------------------------------------------------------

#[derive(Clone, Copy)]
struct PoolSlot {
    handle: Option<SocketHandle>,
    is_tcp: bool,
    closed: bool,
}

static mut IFACE: Option<Interface> = None;
static mut SOCKETS: Option<SocketSet<'static>> = None;
static mut POOL: [PoolSlot; MAX_SOCKETS] = [PoolSlot { handle: None, is_tcp: true, closed: false }; MAX_SOCKETS];

pub fn init(phys_offset: u64) -> bool {
    if !nic::init(phys_offset) {
        return false;
    }
    unsafe {
        let mac = nic::mac();
        let config = Config::new(HardwareAddress::Ethernet(EthernetAddress(mac)));
        // Create the interface with the current time: a 0 timestamp makes the
        // first poll see a huge time delta and the TCP/ARP timers misfire.
        let now = Instant::from_micros((crate::time::monotonic_ns() / 1000) as i64);
        let mut iface = Interface::new(config, &mut DEVICE, now);
        iface.update_ip_addrs(|addrs| {
            addrs.push(IpCidr::new(IpAddress::v4(10, 0, 2, 15), 24));
        });
        iface.routes_mut().add_default_ipv4_route(Ipv4Address::new(10, 0, 2, 2));
        IFACE = Some(iface);
        SOCKETS = Some(SocketSet::new(vec![]));
        for slot in POOL.iter_mut() {
            slot.handle = None;
            slot.is_tcp = true;
            slot.closed = false;
        }
        klog("net: smoltcp up (10.0.2.15/24 gw 10.0.2.2)\n");
        true
    }
}

/// Drive the stack: must be called regularly from the kernel main loop.
pub fn poll() {
    unsafe {
        if let (Some(iface), Some(sockets)) = (IFACE.as_mut(), SOCKETS.as_mut()) {
            let ts = Instant::from_micros((crate::time::monotonic_ns() / 1000) as i64);
            let _ = iface.poll(ts, &mut DEVICE, sockets);
        }
    }
}

fn with_socket<F: FnOnce(&mut SocketSet<'static>, usize) -> i32>(fd: usize, f: F) -> i32 {
    unsafe {
        if fd >= MAX_SOCKETS {
            return -1;
        }
        let Some(sockets) = SOCKETS.as_mut() else { return -1 };
        f(sockets, fd)
    }
}

#[no_mangle]
pub extern "C" fn kern_net_ready() -> i32 {
    if unsafe { IFACE.is_none() } { 0 } else { 1 }
}

#[no_mangle]
pub extern "C" fn kern_net_ip() -> u32 {
    // 10.0.2.15 as a u32 in little-endian (host) order.
    (15u32) | (2 << 8) | (10 << 24)
}

#[no_mangle]
pub extern "C" fn kern_net_mac(out: *mut u8) -> i32 {
    let mac = crate::nic::mac();
    unsafe {
        for i in 0..6 {
            core::ptr::write_volatile(out.add(i), mac[i]);
        }
    }
    0
}

/// Open a socket: 1 = TCP, 0 = UDP.  Returns a fd (0..7) or -1.
#[no_mangle]
pub extern "C" fn kern_net_socket(is_tcp: i32) -> i32 {
    unsafe {
        let Some(sockets) = SOCKETS.as_mut() else { return -1 };
        for fd in 0..MAX_SOCKETS {
            if POOL[fd].handle.is_none() {
                let slot = &mut POOL[fd];
                let handle = if is_tcp != 0 {
                    let rx = tcp::SocketBuffer::new(vec![0u8; TCP_BUF]);
                    let tx = tcp::SocketBuffer::new(vec![0u8; TCP_BUF]);
                    sockets.add(tcp::Socket::new(rx, tx))
                } else {
                    let rx = udp::PacketBuffer::new(vec![udp::PacketMetadata::EMPTY; 4], vec![0u8; UDP_BUF]);
                    let tx = udp::PacketBuffer::new(vec![udp::PacketMetadata::EMPTY; 4], vec![0u8; UDP_BUF]);
                    sockets.add(udp::Socket::new(rx, tx))
                };
                slot.handle = Some(handle);
                slot.is_tcp = is_tcp != 0;
                slot.closed = false;
                return fd as i32;
            }
        }
    }
    -1
}

/// TCP connect (non-blocking; poll for completion).  ip is host-order u32.
#[no_mangle]
pub extern "C" fn kern_net_connect(fd: i32, ip: u32, port: u16) -> i32 {
    with_socket(fd as usize, |sockets, fd| {
        let slot = unsafe { &mut POOL[fd] };
        if !slot.is_tcp {
            return -1;
        }
        let Some(h) = slot.handle else { return -1 };
        let ipaddr = Ipv4Address::from_bytes(&[
            (ip & 0xFF) as u8, ((ip >> 8) & 0xFF) as u8,
            ((ip >> 16) & 0xFF) as u8, ((ip >> 24) & 0xFF) as u8,
        ]);
        // smoltcp 0.11 rejects a local port of 0; use an ephemeral port
        // derived from the monotonic clock.
        let local_port = 49152 + (crate::time::monotonic_ns() % 16384) as u16;
        let r = unsafe {
            let Some(iface) = IFACE.as_mut() else { return -1 };
            let cx = iface.context();
            sockets.get_mut::<tcp::Socket>(h).connect(
                cx, (IpAddress::Ipv4(ipaddr), port), local_port,
            )
        };
        if let Err(e) = r {
            klog(&crate::alloc::format!("net: connect err {:?}\n", e));
            return -1;
        }
        0
    })
}

/// Non-blocking send.  Returns bytes sent or -1 (would block).
#[no_mangle]
pub extern "C" fn kern_net_send(fd: i32, buf: *const u8, len: i64) -> i64 {
    with_socket(fd as usize, |sockets, fd| {
        let h = unsafe { POOL[fd].handle };
        let Some(h) = h else { return -1 };
        if len <= 0 || buf.is_null() {
            return 0;
        }
        let src = unsafe { core::slice::from_raw_parts(buf, len as usize) };
        let sock = sockets.get_mut::<tcp::Socket>(h);
        if !sock.can_send() {
            return -1;
        }
        sock.send_slice(src).unwrap_or(0) as i32
    }) as i64
}

/// Non-blocking recv.  Returns bytes copied, 0 = closed, -1 = would block.
#[no_mangle]
pub extern "C" fn kern_net_recv(fd: i32, buf: *mut u8, cap: i64) -> i64 {
    with_socket(fd as usize, |sockets, fd| {
        let h = unsafe { POOL[fd].handle };
        let Some(h) = h else { return -1 };
        if cap <= 0 || buf.is_null() {
            return 0;
        }
        let sock = sockets.get_mut::<tcp::Socket>(h);
        // Established AND CloseWait can both have buffered data (the peer
        // sends its FIN after the response body; we must keep draining the
        // buffer or the HTTP body reads as empty).
        match sock.state() {
            tcp::State::Established | tcp::State::CloseWait => {
                if sock.can_recv() {
                    let dst = unsafe { core::slice::from_raw_parts_mut(buf, cap as usize) };
                    sock.recv_slice(dst).unwrap_or(0) as i32
                } else {
                    -1
                }
            }
            tcp::State::Closed | tcp::State::TimeWait => 0,
            _ => -1,
        }
    }) as i64
}

/// 1 = TCP connection established (or already closed/finished), 0 = pending.
/// Drives the stack (like kern_net_poll) so the handshake can progress,
/// and returns as soon as it completes -- independent of whether response
/// data has arrived.
#[no_mangle]
pub extern "C" fn kern_net_established(fd: i32) -> i32 {
    with_socket(fd as usize, |sockets, _fd| {
        let h = unsafe { POOL[fd as usize].handle };
        let Some(h) = h else { return -1 };
        crate::net::poll();
        let sock = sockets.get::<tcp::Socket>(h);
        match sock.state() {
            tcp::State::Established => 1,
            tcp::State::Closed | tcp::State::TimeWait => 1,
            _ => 0,
        }
    })
}

/// 1 = socket ready (established + readable, or closed), 0 = pending.
#[no_mangle]
pub extern "C" fn kern_net_poll(fd: i32, timeout_ms: i64) -> i32 {
    with_socket(fd as usize, |sockets, fd| {
        let h = unsafe { POOL[fd].handle };
        let Some(h) = h else { return -1 };
        let deadline = crate::time::monotonic_ns() + (timeout_ms.max(0) as u64) * 1_000_000;
        loop {
            crate::net::poll();
            let sock = sockets.get::<tcp::Socket>(h);
            match sock.state() {
                // CloseWait = peer sent FIN after the response body; data
                // may still be buffered and must be drained before EOF.
                tcp::State::Established | tcp::State::CloseWait => {
                    if sock.can_recv() || !sock.may_recv() {
                        return 1;
                    }
                }
                tcp::State::Closed | tcp::State::TimeWait => return 1,
                _ => {}
            }
            if crate::time::monotonic_ns() >= deadline {
                return 0;
            }
        }
    })
}

#[no_mangle]
pub extern "C" fn kern_net_close(fd: i32) -> i32 {
    with_socket(fd as usize, |sockets, fd| {
        let slot = unsafe { &mut POOL[fd] };
        let Some(h) = slot.handle else { return -1 };
        if slot.is_tcp {
            sockets.get_mut::<tcp::Socket>(h).close();
        } else {
            sockets.get_mut::<udp::Socket>(h).close();
        }
        sockets.remove(h);
        slot.handle = None;
        slot.closed = true;
        0
    })
}
