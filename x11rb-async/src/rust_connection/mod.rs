//! An implementation of a pure-Rust async connection to an X11 server.

use async_lock::{Mutex, MutexGuard, RwLock};
use futures_lite::future;

use std::convert::Infallible;
use std::future::Future;
use std::io;
use std::pin::Pin;
use std::sync::Arc;

use crate::connection::{Connection, Fut, RequestConnection};
use crate::{Cookie, CookieWithFds, VoidCookie};

use x11rb_protocol::connection::{Connection as ProtoConnection, PollReply, ReplyFdKind};
use x11rb_protocol::id_allocator::IdAllocator;
use x11rb_protocol::protocol::bigreq::EnableReply;
use x11rb_protocol::protocol::xproto::Setup;
use x11rb_protocol::x11_utils::{ExtensionInformation, TryParse, TryParseFd, X11Error};
use x11rb_protocol::xauth::get_auth;
use x11rb_protocol::{DiscardMode, RawFdContainer, SequenceNumber};

use x11rb::connection::{BufWithFds, ReplyOrError};
use x11rb::errors::{ConnectError, ConnectionError, ParseError, ReplyOrIdError};

mod extensions;
mod nb_connect;
mod shared_state;
mod stream;

pub use stream::{DefaultStream, Stream, StreamAdaptor, StreamBase};

/// A pure-Rust async connection to an X11 server.
#[derive(Debug)]
pub struct RustConnection<S = DefaultStream> {
    /// Shared state between the conenction and the packet reader.
    shared: Arc<shared_state::SharedState<S>>,

    /// The write buffer.
    ///
    /// Holding this lock implies the exclusive right to write to the stream.
    write_buffer: WriteBuffer,

    /// The setup information.
    setup: Setup,

    /// The maximum number of bytes we can send in a single request.
    max_request_bytes: Mutex<MaxRequestBytes>,

    /// The allocator for resource IDs.
    id_allocator: Mutex<IdAllocator>,

    /// The extension information.
    extensions: RwLock<extensions::Extensions>,
}

#[derive(Debug)]
struct WriteBuffer(Mutex<WriteBufferInner>);

#[derive(Debug)]
struct WriteBufferGuard<'a>(MutexGuard<'a, WriteBufferInner>);

#[derive(Debug)]
struct WriteBufferInner {
    /// The buffer that is used for writing.
    buffer: Vec<u8>,

    /// The file descriptors that we are sending over.
    fds: Vec<RawFdContainer>,

    /// Whether the buffer has been corrupted.
    ///
    /// A lock has to be explicitly unlock()d, otherwise the buffer is marked as corrupted.
    /// This exists to detect futures that were not polled to completion and might have
    /// written only a part of their data.
    corrupted: bool,
}

/// The maximum bytes we can send in a single request.
#[derive(Debug, PartialEq, Eq)]
enum MaxRequestBytes {
    /// Don't know.
    Unknown,

    /// This many bytes.
    Known(usize),

    /// We are waiting for the server to tell us.
    Requested(Option<SequenceNumber>),
}

impl RustConnection {
    /// Connect to the X11 server.
    ///
    /// This function returns a future that drives the packet reader for the connection.
    /// It should be spawned on a task executor to be polled while the connection is in
    /// use.
    pub async fn connect(
        display_name: Option<&str>,
    ) -> Result<
        (
            Self,
            usize,
            impl Future<Output = Result<Infallible, ConnectionError>> + Send,
        ),
        ConnectError,
    > {
        // Parse the display name.
        let addrs = x11rb_protocol::parse_display::parse_display(display_name)
            .ok_or(ConnectError::DisplayParsingError)?;

        // Connect to the stream.
        let (stream, screen) = nb_connect::connect(&addrs).await?;

        // Wrap the stream in a connection.
        let stream = StreamAdaptor::new(stream)?;

        // Get the peer address of the socket.
        let (family, address) = stream.get_ref().peer_addr()?;

        // Use this to get authority information.
        let (auth_name, auth_data) = blocking::unblock(move || {
            get_auth(family, &address, addrs.display)
                .unwrap_or(None)
                .unwrap_or_else(|| (Vec::new(), Vec::new()))
        })
        .await;

        let (conn, drive) =
            RustConnection::connect_to_stream_with_auth_info(stream, screen, auth_name, auth_data)
                .await?;
        Ok((conn, screen, drive))
    }
}

impl<S: Stream + Send + Sync> RustConnection<S> {
    /// Connect to the X11 server using the given stream.
    ///
    /// This function returns a future that drives the packet reader for the connection.
    /// It should be spawned on a task executor to be polled while the connection is in
    /// use.
    pub async fn connect_to_stream(
        stream: S,
        screen: usize,
    ) -> Result<
        (
            Self,
            impl Future<Output = Result<Infallible, ConnectionError>> + Send,
        ),
        ConnectError,
    > {
        Self::connect_to_stream_with_auth_info(stream, screen, Vec::new(), Vec::new()).await
    }

    /// Connect to the server using the given stream and authentication information.
    ///
    /// This function returns a future that drives the packet reader for the connection.
    /// It should be spawned on a task executor to be polled while the connection is in
    /// use.
    pub async fn connect_to_stream_with_auth_info(
        stream: S,
        screen: usize,
        auth_name: Vec<u8>,
        auth_data: Vec<u8>,
    ) -> Result<
        (
            Self,
            impl Future<Output = Result<Infallible, ConnectionError>> + Send,
        ),
        ConnectError,
    > {
        // Set up the connection.
        let (mut connect, setup_request) =
            x11rb_protocol::connect::Connect::with_authorization(auth_name, auth_data);

        // Write the setup request.
        let mut fds = Vec::new();
        let mut nwritten = 0;

        while nwritten < setup_request.len() {
            nwritten += write_with(&stream, |stream| {
                match stream.write(&setup_request[nwritten..], &mut fds) {
                    Ok(0) => Err(io::ErrorKind::WriteZero.into()),
                    res => res,
                }
            })
            .await?;
        }

        // Read in the setup.
        loop {
            let adv = match stream.read(connect.buffer(), &mut fds) {
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => 0,
                Ok(0) => return Err(io::Error::from(io::ErrorKind::UnexpectedEof).into()),
                Ok(n) => n,
                Err(e) => return Err(e.into()),
            };

            // Advance the connection.
            if connect.advance(adv) {
                break;
            }

            // Wait for more data.
            stream.readable().await?;
        }

        // Resolve the setup.
        let setup = connect.into_setup()?;

        // Make sure it's valid.
        if setup.roots.len() <= screen {
            return Err(ConnectError::InvalidScreen);
        }

        Self::for_connected_stream(stream, setup)
    }

    /// Establish a connection on an already connected stream.
    ///
    /// This function returns a future that drives the packet reader for the connection.
    /// It should be spawned on a task executor to be polled while the connection is in
    /// use.
    pub fn for_connected_stream(
        stream: S,
        setup: Setup,
    ) -> Result<
        (
            Self,
            impl Future<Output = Result<Infallible, ConnectionError>> + Send,
        ),
        ConnectError,
    > {
        let id_allocator = IdAllocator::new(setup.resource_id_base, setup.resource_id_mask)?;
        let shared = Arc::new(shared_state::SharedState::new(stream));

        // Spawn a future that reads from the stream and caches the result.
        let drive = {
            let shared = shared.clone();
            async move { shared.drive().await }
        };

        Ok((
            RustConnection {
                shared,
                write_buffer: WriteBuffer(Mutex::new(WriteBufferInner {
                    buffer: Vec::with_capacity(16384),
                    fds: vec![],
                    corrupted: false,
                })),
                setup,
                max_request_bytes: Mutex::new(MaxRequestBytes::Unknown),
                id_allocator: Mutex::new(id_allocator),
                extensions: Default::default(),
            },
            drive,
        ))
    }

    /// Send a request.
    async fn send_request(
        &self,
        bufs: &[io::IoSlice<'_>],
        mut fds: Vec<RawFdContainer>,
        kind: ReplyFdKind,
    ) -> Result<SequenceNumber, ConnectionError>
    where
        S: Send + Sync,
    {
        // Compute the request.
        let mut storage = Default::default();
        let bufs = compute_length_field(self, bufs, &mut storage).await?;

        // Lock the buffer.
        let mut buffer = self.write_buffer.lock().await?;

        loop {
            let seq = {
                let mut inner = self.shared.lock_connection();
                inner.send_request(kind)
            };

            // Logically send the request.
            match seq {
                Some(seq) => {
                    // Write the request to the buffer.
                    buffer = self.write_all_vectored(buffer, bufs, &mut fds).await?;
                    buffer.unlock();
                    return Ok(seq);
                }

                None => {
                    // Synchronize and try agan.
                    buffer = self.send_sync(buffer).await?;
                }
            }
        }
    }

    /// Send a request that catches us up to the current sequence number.
    async fn send_sync<'a>(
        &'a self,
        buffer: WriteBufferGuard<'a>,
    ) -> Result<WriteBufferGuard<'a>, ConnectionError> {
        let length = 1u16.to_ne_bytes();
        let request = [
            x11rb_protocol::protocol::xproto::GET_INPUT_FOCUS_REQUEST,
            0,
            length[0],
            length[1],
        ];

        // Send this request.
        {
            let mut inner = self.shared.lock_connection();
            let seq = inner
                .send_request(ReplyFdKind::ReplyWithoutFDs)
                .expect("This request should not be blocked by syncs");
            inner.discard_reply(seq, DiscardMode::DiscardReplyAndError);

            seq
        };

        // Write the entire packet.
        let iov = &[io::IoSlice::new(&request)];
        let mut fds = Vec::new();
        self.write_all_vectored(buffer, iov, &mut fds).await
    }

    /// Write a set of buffers to the stream.
    async fn write_all_vectored<'a>(
        &'a self,
        mut write_buffer: WriteBufferGuard<'a>,
        mut bufs: &[io::IoSlice<'_>],
        fds: &mut Vec<RawFdContainer>,
    ) -> Result<WriteBufferGuard<'a>, ConnectionError> {
        // Get the total length of the buffers.
        let mut total_len = bufs
            .iter()
            .fold(0usize, |sum, buf| sum.saturating_add(buf.len()));

        // If our data doesn't fit, flush the buffer first.
        if write_buffer.0.buffer.len() + total_len > write_buffer.0.buffer.capacity() {
            write_buffer = self.flush_impl(write_buffer).await?;
        }

        // If our data fits now, write all of it.
        if total_len < write_buffer.0.buffer.capacity() {
            for buf in bufs {
                write_buffer.0.buffer.extend_from_slice(buf);
            }

            write_buffer.0.fds.append(fds);

            return Ok(write_buffer);
        }

        debug_assert!(write_buffer.0.buffer.is_empty());

        // Otherwise, write directly to the stream.
        let mut partial: &[u8] = &[];
        write_with(&self.shared.stream, |stream| {
            while total_len > 0 && !partial.is_empty() {
                // If the partial buffer is non-empty, write it.
                if !partial.is_empty() {
                    let n = stream.write(partial, fds)?;
                    if n == 0 {
                        return Err(io::Error::from(io::ErrorKind::WriteZero));
                    }

                    partial = &partial[n..];
                    total_len -= n;
                } else {
                    // Write the iov.
                    let mut n = stream.write_vectored(bufs, fds)?;
                    if n == 0 {
                        return Err(io::Error::from(io::ErrorKind::WriteZero));
                    }

                    // Calculate how much we have left to go.
                    total_len -= n;
                    while n > 0 {
                        if n >= bufs[0].len() {
                            n -= bufs[0].len();
                            bufs = &bufs[1..];
                        } else {
                            partial = &bufs[0][n..];
                            n = 0;
                        }
                    }
                }
            }

            Ok(())
        })
        .await?;

        Ok(write_buffer)
    }

    /// Flush the write buffer.
    async fn flush_impl<'a>(
        &'a self,
        mut buffer: WriteBufferGuard<'a>,
    ) -> Result<WriteBufferGuard<'a>, ConnectionError> {
        // If we don't have any data to write, we are done.
        if buffer.0.buffer.is_empty() && buffer.0.fds.is_empty() {
            return Ok(buffer);
        }

        // Write the entire buffer.
        let mut position = 0;
        write_with(&self.shared.stream, {
            let buffer = &mut *buffer.0;
            move |stream| {
                while position < buffer.buffer.len() {
                    let n = stream.write(&buffer.buffer[position..], &mut buffer.fds)?;
                    if n == 0 {
                        return Err(io::Error::new(
                            io::ErrorKind::WriteZero,
                            "failed to write whole buffer",
                        ));
                    }

                    position += n;
                }

                Ok(())
            }
        })
        .await?;

        if !buffer.0.fds.is_empty() {
            return Err(ConnectionError::IoError(io::Error::new(
                io::ErrorKind::Other,
                "failed to write all fds",
            )));
        }

        // Reset the buffer.
        buffer.0.buffer.clear();

        Ok(buffer)
    }

    /// Prefetch the maximum request length.
    async fn prefetch_len_impl(&self) -> Result<MutexGuard<'_, MaxRequestBytes>, ConnectionError>
    where
        S: Send + Sync,
    {
        let mut mrl = self.max_request_bytes.lock().await;

        // Start prefetching if necessary.
        if *mrl == MaxRequestBytes::Unknown {
            // Wait for the reply.
            let cookie = crate::protocol::bigreq::enable(self)
                .await
                .map(|cookie| {
                    let seq = cookie.sequence_number();
                    std::mem::forget(cookie);
                    seq
                })
                .ok();

            // Update the max request length.
            *mrl = MaxRequestBytes::Requested(cookie);
        }

        Ok(mrl)
    }

    /// Wait for a reply with file descriptors.
    async fn wait_for_reply_with_fds_impl(
        &self,
        sequence: SequenceNumber,
    ) -> Result<ReplyOrError<BufWithFds<Vec<u8>>, Vec<u8>>, ConnectionError> {
        // Ensure that the request is sent.
        self.flush_impl(self.write_buffer.lock().await?)
            .await?
            .unlock();

        let get_reply = |inner: &mut ProtoConnection| {
            if let Some(reply) = inner.poll_for_reply_or_error(sequence) {
                if reply.0[0] == 0 {
                    // This is a reply
                    Some(Ok(ReplyOrError::Error(reply.0)))
                } else {
                    // This is an error
                    Some(Ok(ReplyOrError::Reply(reply)))
                }
            } else {
                None
            }
        };

        self.shared.wait_for_incoming(get_reply).await
    }
}

impl WriteBuffer {
    async fn lock(&self) -> Result<WriteBufferGuard<'_>, ConnectionError> {
        let mut lock = self.0.lock().await;
        if std::mem::replace(&mut lock.corrupted, true) {
            return Err(ConnectionError::IoError(io::Error::new(
                io::ErrorKind::Other,
                "The write buffer was corrupted",
            )));
        }

        Ok(WriteBufferGuard(lock))
    }
}

impl<'a> WriteBufferGuard<'a> {
    fn unlock(mut self) {
        self.0.corrupted = false;
    }
}

impl<S: Stream + Send + Sync> RequestConnection for RustConnection<S> {
    type Buf = Vec<u8>;

    fn send_request_with_reply<'this, 'bufs, 'sl, 're, 'future, R>(
        &'this self,
        bufs: &'bufs [io::IoSlice<'sl>],
        fds: Vec<RawFdContainer>,
    ) -> Fut<'future, crate::Cookie<'this, Self, R>, ConnectionError>
    where
        'this: 'future,
        'bufs: 'future,
        'sl: 'future,
        're: 'future,
        R: TryParse + Send + 're,
    {
        Box::pin(async move {
            let seq = self
                .send_request(bufs, fds, ReplyFdKind::ReplyWithoutFDs)
                .await?;

            Ok(Cookie::new(self, seq))
        })
    }

    fn send_request_with_reply_with_fds<'this, 'bufs, 'sl, 're, 'future, R>(
        &'this self,
        bufs: &'bufs [io::IoSlice<'sl>],
        fds: Vec<RawFdContainer>,
    ) -> Fut<'future, crate::CookieWithFds<'this, Self, R>, ConnectionError>
    where
        'this: 'future,
        'bufs: 'future,
        'sl: 'future,
        're: 'future,
        R: TryParseFd + Send + 're,
    {
        Box::pin(async move {
            let seq = self
                .send_request(bufs, fds, ReplyFdKind::ReplyWithFDs)
                .await?;

            Ok(CookieWithFds::new(self, seq))
        })
    }

    fn send_request_without_reply<'this, 'bufs, 'sl, 'future>(
        &'this self,
        bufs: &'bufs [io::IoSlice<'sl>],
        fds: Vec<RawFdContainer>,
    ) -> Fut<'future, crate::VoidCookie<'this, Self>, ConnectionError>
    where
        'this: 'future,
        'bufs: 'future,
        'sl: 'future,
    {
        Box::pin(async move {
            let seq = self.send_request(bufs, fds, ReplyFdKind::NoReply).await?;

            Ok(VoidCookie::new(self, seq))
        })
    }

    fn discard_reply(
        &self,
        sequence: SequenceNumber,
        _kind: x11rb::connection::RequestKind,
        mode: x11rb_protocol::DiscardMode,
    ) {
        self.shared.lock_connection().discard_reply(sequence, mode)
    }

    fn prefetch_extension_information(&self, name: &'static str) -> Fut<'_, (), ConnectionError> {
        Box::pin(async move {
            let mut cache = self.extensions.write().await;
            cache.prefetch(self, name).await
        })
    }

    fn extension_information(
        &self,
        name: &'static str,
    ) -> Fut<'_, Option<ExtensionInformation>, ConnectionError> {
        Box::pin(async move {
            let mut cache = self.extensions.write().await;
            cache.information(self, name).await
        })
    }

    fn wait_for_reply_or_raw_error(
        &self,
        sequence: SequenceNumber,
    ) -> Fut<'_, ReplyOrError<Self::Buf>, ConnectionError> {
        Box::pin(async move {
            match self.wait_for_reply_with_fds_impl(sequence).await? {
                ReplyOrError::Reply((buf, _)) => Ok(ReplyOrError::Reply(buf)),
                ReplyOrError::Error(buf) => Ok(ReplyOrError::Error(buf)),
            }
        })
    }

    fn wait_for_reply(
        &self,
        sequence: SequenceNumber,
    ) -> Fut<'_, Option<Self::Buf>, ConnectionError> {
        Box::pin(async move {
            // Flush the request.
            self.flush_impl(self.write_buffer.lock().await?)
                .await?
                .unlock();

            let get_reply = |inner: &mut ProtoConnection| match inner.poll_for_reply(sequence) {
                PollReply::TryAgain => None,
                PollReply::Reply(reply) => Some(Ok(Some(reply))),
                PollReply::NoReply => Some(Ok(None)),
            };

            // Wait for the reply.
            self.shared.wait_for_incoming(get_reply).await
        })
    }

    fn wait_for_reply_with_fds_raw(
        &self,
        sequence: SequenceNumber,
    ) -> Fut<'_, ReplyOrError<x11rb::connection::BufWithFds<Self::Buf>, Self::Buf>, ConnectionError>
    {
        Box::pin(self.wait_for_reply_with_fds_impl(sequence))
    }

    fn check_for_raw_error(
        &self,
        sequence: SequenceNumber,
    ) -> Fut<'_, Option<Self::Buf>, ConnectionError> {
        Box::pin(async move {
            let mut write_buffer = None;
            if self
                .shared
                .lock_connection()
                .prepare_check_for_reply_or_error(sequence)
            {
                write_buffer = Some(self.send_sync(self.write_buffer.lock().await?).await?);

                assert!(!self
                    .shared
                    .lock_connection()
                    .prepare_check_for_reply_or_error(sequence),);
            }

            // Ensure that the request is sent.
            let write_buffer = match write_buffer {
                Some(write_buffer) => write_buffer,
                None => self.write_buffer.lock().await?,
            };
            self.flush_impl(write_buffer).await?.unlock();

            let get_result =
                |inner: &mut ProtoConnection| match inner.poll_check_for_reply_or_error(sequence) {
                    PollReply::TryAgain => None,
                    PollReply::NoReply => Some(Ok(None)),
                    PollReply::Reply(buffer) => Some(Ok(Some(buffer))),
                };

            self.shared.wait_for_incoming(get_result).await
        })
    }

    fn prefetch_maximum_request_bytes(
        &self,
    ) -> Pin<Box<dyn futures_lite::Future<Output = ()> + Send + '_>> {
        Box::pin(async move {
            self.prefetch_len_impl()
                .await
                .expect("Failed to prefetch maximum request bytes");
        })
    }

    fn maximum_request_bytes(
        &self,
    ) -> Pin<Box<dyn futures_lite::Future<Output = usize> + Send + '_>> {
        Box::pin(async move {
            let mut mrl = self
                .prefetch_len_impl()
                .await
                .expect("Failed to prefetch maximum request bytes");

            // Complete the prefetching.
            match *mrl {
                MaxRequestBytes::Known(len) => len,
                MaxRequestBytes::Unknown => unreachable!("We are in the Some branch"),
                MaxRequestBytes::Requested(cookie) => {
                    let cookie = match cookie {
                        Some(cookie) => cookie,
                        None => {
                            // Not available.
                            return self
                                .setup()
                                .maximum_request_length
                                .try_into()
                                .ok()
                                .and_then(|x: usize| x.checked_mul(4))
                                .unwrap_or(std::usize::MAX);
                        }
                    };

                    // Wait for the reply.
                    let cookie = Cookie::<'_, _, EnableReply>::new(self, cookie);

                    let reply = cookie.reply().await.expect("Failed to get reply");

                    // Mark the request as done.
                    let total = reply
                        .maximum_request_length
                        .try_into()
                        .ok()
                        .and_then(|x: usize| x.checked_mul(4))
                        .unwrap_or(std::usize::MAX);

                    *mrl = MaxRequestBytes::Known(total);
                    total
                }
            }
        })
    }

    fn parse_error(&self, error: &[u8]) -> Result<x11rb::x11_utils::X11Error, ParseError> {
        let extensions = future::block_on(self.extensions.read());
        X11Error::try_parse(error, &*extensions)
    }

    fn parse_event(&self, event: &[u8]) -> Result<x11rb::protocol::Event, ParseError> {
        let extensions = future::block_on(self.extensions.read());
        x11rb::protocol::Event::parse(event, &*extensions)
    }
}

impl<S: Stream + Send + Sync> Connection for RustConnection<S> {
    fn wait_for_raw_event_with_sequence(
        &self,
    ) -> Fut<'_, x11rb_protocol::RawEventAndSeqNumber<Self::Buf>, ConnectionError> {
        Box::pin(async move {
            let get_event = |inner: &mut ProtoConnection| inner.poll_for_event_with_sequence();

            Ok(self.shared.wait_for_incoming(get_event).await)
        })
    }

    fn poll_for_raw_event_with_sequence(
        &self,
    ) -> Result<Option<x11rb_protocol::RawEventAndSeqNumber<Self::Buf>>, ConnectionError> {
        Ok(self.shared.lock_connection().poll_for_event_with_sequence())
    }

    fn flush(&self) -> Fut<'_, (), ConnectionError> {
        Box::pin(async move {
            self.flush_impl(self.write_buffer.lock().await?)
                .await?
                .unlock();

            Ok(())
        })
    }

    fn setup(&self) -> &Setup {
        &self.setup
    }

    fn generate_id(&self) -> Fut<'_, u32, ReplyOrIdError> {
        Box::pin(async move {
            use crate::protocol::xc_misc;

            let mut id_allocator = self.id_allocator.lock().await;

            // Try to get an ID from the allocator.
            if let Some(id) = id_allocator.generate_id() {
                return Ok(id);
            }

            // We may need to allocate more IDs.
            if self
                .extension_information(xc_misc::X11_EXTENSION_NAME)
                .await?
                .is_some()
            {
                // Update the ID range.
                id_allocator
                    .update_xid_range(&xc_misc::get_xid_range(self).await?.reply().await?)?;

                // Generate a new ID.
                return id_allocator
                    .generate_id()
                    .ok_or(ReplyOrIdError::IdsExhausted);
            }

            // If we are here, we do not have the XCMisc extension.
            Err(ReplyOrIdError::IdsExhausted)
        })
    }
}

/// Copied from x11rb
async fn compute_length_field<'b>(
    conn: &impl RequestConnection,
    request_buffers: &'b [io::IoSlice<'b>],
    storage: &'b mut (Vec<io::IoSlice<'b>>, [u8; 8]),
) -> Result<&'b [io::IoSlice<'b>], ConnectionError> {
    // Compute the total length of the request
    let length: usize = request_buffers.iter().map(|buf| buf.len()).sum();
    assert_eq!(
        length % 4,
        0,
        "The length of X11 requests must be a multiple of 4, got {}",
        length
    );
    let wire_length = length / 4;

    let first_buf = &request_buffers[0];

    // If the length fits into an u16, just return the request as-is
    if let Ok(wire_length) = u16::try_from(wire_length) {
        // Check that the request contains the correct length field
        let length_field = u16::from_ne_bytes([first_buf[2], first_buf[3]]);
        assert_eq!(
            wire_length, length_field,
            "Length field contains incorrect value"
        );
        return Ok(request_buffers);
    }

    // Check that the total length is not too large
    if length > conn.maximum_request_bytes().await {
        return Err(ConnectionError::MaximumRequestLengthExceeded);
    }

    // Okay, we need to use big requests (thus four extra bytes, "+1" below)
    let wire_length: u32 = wire_length
        .checked_add(1)
        .ok_or(ConnectionError::MaximumRequestLengthExceeded)?
        .try_into()
        .expect("X11 request larger than 2^34 bytes?!?");
    let wire_length = wire_length.to_ne_bytes();

    // Now construct the new IoSlices

    // Replacement for the first four bytes of the request
    storage.1.copy_from_slice(&[
        // First part of the request
        first_buf[0],
        first_buf[1],
        // length field zero indicates big requests
        0,
        0,
        // New bytes: extended length
        wire_length[0],
        wire_length[1],
        wire_length[2],
        wire_length[3],
    ]);
    storage.0.push(io::IoSlice::new(&storage.1));

    // The remaining part of the first buffer of the request
    storage.0.push(io::IoSlice::new(&first_buf[4..]));

    // and the rest of the request
    storage.0.extend(
        request_buffers[1..]
            .iter()
            .map(std::ops::Deref::deref)
            .map(io::IoSlice::new),
    );

    Ok(&storage.0[..])
}

async fn write_with<'a, S: StreamBase<'a>, R, F>(stream: &'a S, mut f: F) -> Result<R, io::Error>
where
    F: FnMut(&'a S) -> Result<R, io::Error>,
{
    loop {
        match f(stream) {
            Err(ref e) if e.kind() == io::ErrorKind::WouldBlock => {
                // This operation would block; wait for the stream to become writable.
                stream.writable().await?;
            }

            res => return res,
        }
    }
}