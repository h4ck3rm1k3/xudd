import socket
import select
import logging

from xudd.actor import Actor

_log = logging.getLogger(__name__)

class Server(Actor):
    def __init__(self, hive, id, request_handler=None):
        super(Server, self).__init__(hive, id)
        self.message_routing.update({
            'respond': self.respond,
            'listen': self.listen
        })
        self.requests = {}
        self.request_handler = request_handler


    def listen(self, message):
        body = message.body

        port = body.get('port', 8000)
        host = body.get('host', '127.0.0.1')

        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.setblocking(0)  # XXX: Don't know if this helps much
        self.socket.bind((host, port))
        self.socket.listen(5)  # Max 5 connections in queue

        while True:
            readable, writable, errored = select.select(
                [self.socket],
                [],
                [],
                .0000001)  # XXX: This will surely make it fast! (?)

            if readable:
                _log.info('Got new request ({0} in local index)'.format(len(self.requests)))
                req = self.socket.accept()

                # Use the message id as the internal id for the request
                message_id = self.send_message(
                    to=self.request_handler,
                    directive='handle_request',
                    body={
                        'request': req
                    }
                )

                _log.debug('Sent request to worker')

                self.requests.update({
                    message_id: req
                })

            yield self.wait_on_self()

    def send(self, message):
        sock, bind = self.requests.get(message.in_reply_to)
        sock.sendall(message.body['response'])

    def close(self, message):
        sock, bind = self.requests.get(message.in_reply_to)
        sock.close()

    def respond(self, message):
        _log.debug('Responding')

        sock, bind = self.requests.get(message.in_reply_to)
        sock.sendall(message.body['response'])
        sock.close()
        del self.requests[message.in_reply_to]
        _log.info('Responded')


class Client(Actor):
    """TCP client

    Client can't do any processing on its own, it relies on another actor to
    take the data it receives.

    Once connected it will gather data and send chunks it receives to the
    other actor like this:

        self.send_message(
            to=self.chunk_handler,
            directive='handle_chunk',
            body={'chunk': b'some bytes'})

    """
    def __init__(self, hive, id, chunk_handler=None, poll_timeout=10):
        """Initialize the client

        - *chunk_handler*: ID of the actor that will be given data as we
        receive it through the socket. *Must* have a directive called
        'handle_chunk'
        - *poll_timeout*: number of milliseconds to wait for data before
        returning collected (if any) data
        """
        super(Client, self).__init__(hive, id)

        self.message_routing.update({
            'connect': self.connect,
            'send': self.send,
        })

        self.poll_timout = poll_timeout
        self.chunk_handler = chunk_handler

    def connect(self, message):
        """Connect to a server

        The body of the message should be dict that contains the following keys:
        - *host*: domain name or IP address of server to connect to
        - *port*: port number to connect to

        It may also contain the following options:
        - *timeout*: the integer amount of time in seconds a connection will
        wait with no data before closing itself. Defaults to no timeout or the
        last value give to socket.setdefaulttimeout() (see Python docs)
        - *chunk_size*: size of the receive buffer, idealy a smaller power of 2.
        Defaults to 1024
        """
        try:
            host = message.body['host']
            port = message.body['port']
        except KeyError:
            raise ValueError('{klass}.connect must be called with `host` and\
                             `port` arguments in the message body'.format(
                             self.__class__.__name__))

        # Options
        timeout = message.body.get('timeout', socket.getdefaulttimeout())
        chunk_size = message.body.get('chunk_size', 1024)

        self.socket = socket.create_connection((host, port), timeout)
        self.socket.setblocking(0)  # XXX: Don't know if this helps much
        # M says: docs aren't clear on a non-zero timeout + the above

        READ_ONLY = select.POLLIN | select.POLLPRI | select.POLLHUP \
            | select.POLLERR

        poller = select.poll()
        poller.register(self.socket, READ_ONLY)

        socket_from_fd = {
            self.socket.fileno(): self.socket
        }

        _log.info('Connected to {host}:{port}'.format(host=host, port=port))

        while True:
            events = poller.poll(self.poll_timout)

            for fd, flag in events:
                sock = socket_from_fd[fd]

                if flag & (select.POLLIN | select.POLLPRI):
                    if sock is self.socket:
                        chunk = self.socket.recv(chunk_size)

                        if chunk in ['', b'']:
                            raise RuntimeError('socket connection broken')

                        self.send_message(
                            to=self.chunk_handler,
                            directive='handle_chunk',
                            body={'chunk': chunk})

            yield self.wait_on_self()

    def send(self, message):
        """Send

        Does what it says on the tin.
        """
        out = message.body['message']
        length = len(out)
        total_sent = 0
        while total_sent < length:
            sent = self.socket.send(out[total_sent:])

            if sent == 0:
                raise RuntimeError('socket connection broken')

            total_sent += sent
