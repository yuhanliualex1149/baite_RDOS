"""Model stream provenance without remote connections or a disabled audit guard."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


CODE = Path(__file__).resolve().parents[1]


def isolated_socket_guard(body):
    with tempfile.TemporaryDirectory(prefix='rdos-socket-guard-') as temporary:
        root = Path(temporary).resolve()
        (root / 'runtime').mkdir()
        (root / 'code/lab').mkdir(parents=True)
        (root / 'code/lab/__init__.py').write_text('')
        shutil.copy2(CODE / 'lab/guard.py', root / 'code/lab/guard.py')
        setup = '''
import asyncio, contextvars, gc, json, os, socket, sys, weakref
from pathlib import Path
from lab import guard
root = Path(os.environ['RDOS_LAB_ROOT'])
token = object()
guard.register_model_token(token)
class Agent:
    _lab_model_token = token
agent = Agent()
guard.install(root,18761)

class PeerSocket(socket.socket):
    # Controlled peer facts test the real audit hook without contacting any
    # host. Neither sys.audit below nor getpeername performs network I/O.
    peer = ('198.51.100.23',443)
    def getpeername(self):
        if self.fileno() < 0: raise OSError('closed')
        return self.peer

def event(name,sock,address=None):
    sys.audit('socket.'+name,sock,address)

def denied(name,sock,address=None):
    try:event(name,sock,address)
    except PermissionError as exc:
        assert 'network.'+name in str(exc),str(exc)
    else:raise AssertionError('unguarded '+name)

def register(sock,address=('198.51.100.23',443)):
    with guard.model_permit(agent):event('connect',sock,address)
'''
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'PYTHONPATH': str(root / 'code'),
               'PYTHONDONTWRITEBYTECODE': '1', 'RDOS_LAB_MODE': '1', 'RDOS_LAB_ROOT': str(root),
               'RDOS_LAB_RUNTIME': str(root / 'runtime'), 'RDOS_LAB_ENV_ID': 'rdos_socket_guard_only',
               'RDOS_LAB_GENERATION': '1' * 32}
        return subprocess.run([sys.executable, '-c', setup + '\n' + body], cwd=root / 'code',
                              env=env, capture_output=True, text=True, timeout=20)


class ModelSocketGuardTest(unittest.TestCase):
    def check(self, body):
        result = isolated_socket_guard(body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_registered_model_stream_can_flush_after_permit_context_exits(self):
        self.check('''
sock=PeerSocket(socket.AF_INET,socket.SOCK_STREAM)
denied('sendmsg',sock)
register(sock)
assert not guard._model_permit.get()
async def check():
    loop=asyncio.get_running_loop()
    flushed=loop.create_future()
    def flush():
        assert not guard._model_permit.get()
        event('sendmsg',sock)
        flushed.set_result(True)
    # A real asyncio callback with an empty ContextVar context models selector
    # transport buffering; the exact registered socket remains the authority.
    loop.call_soon(flush,context=contextvars.Context())
    assert await asyncio.wait_for(flushed,2)
asyncio.run(check())
sock.close()
ipv6=PeerSocket(socket.AF_INET6,socket.SOCK_STREAM)
ipv6.peer=('2001:db8::23',443,0,0)
register(ipv6,ipv6.peer)
event('sendmsg',ipv6)
ipv6.close()
''')

    def test_unregistered_addressed_datagram_unix_and_sendto_stay_blocked(self):
        self.check('''
sock=PeerSocket(socket.AF_INET,socket.SOCK_STREAM)
other=PeerSocket(socket.AF_INET,socket.SOCK_STREAM)
denied('connect',sock,('198.51.100.23',443))
with guard.model_permit(agent):
    denied('connect',sock,('198.51.100.23',8443))
register(sock)
denied('sendmsg',other)
denied('sendmsg',sock,('198.51.100.23',443))
denied('sendmsg',sock,())
denied('sendto',sock)
udp=PeerSocket(socket.AF_INET,socket.SOCK_DGRAM)
with guard.model_permit(agent):
    denied('connect',udp,('198.51.100.23',443))
    denied('sendmsg',udp)
unix=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
with guard.model_permit(agent):
    denied('connect',unix,'/tmp/not-a-real-service')
    denied('sendmsg',unix)
for value in (sock,other,udp,unix):value.close()
''')

    def test_closed_changed_peer_and_reused_descriptor_do_not_inherit_trust(self):
        self.check('''
sock=PeerSocket(socket.AF_INET,socket.SOCK_STREAM)
register(sock)
sock.peer=('198.51.100.23',8443)
denied('sendmsg',sock)
sock.peer=('198.51.100.23',443)
fd=sock.fileno()
sock.close()
denied('sendmsg',sock)
replacement=PeerSocket(socket.AF_INET,socket.SOCK_STREAM)
# Even if the OS immediately reuses fd, object provenance is not transferred.
denied('sendmsg',replacement)
replacement.close()
reference=weakref.ref(sock)
del sock
gc.collect()
assert reference() is None
''')


if __name__ == '__main__':
    unittest.main()
